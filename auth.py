"""Bearer token → 可信 user_id：Agent introspection + 进程内缓存。

为什么不在本地验签（DESIGN.md §4.3）：
1. HS256 是共享密钥，复制到第三个进程等于多开一个签发后门；
2. 本地验签看不到 users.disabled / 已注销，语义和 get_current_user 不一致；
3. Agent 换 RS256/JWKS 时 MCP 无需改动。

缓存：key = sha256(token)，TTL = min(INTROSPECT_CACHE_TTL, exp - now)。
- exp 由 Agent 的 /api/auth/introspect 返回（已验签 token 的过期时间）；
- exp 缺失或已过期时 ttl <= 0，不写缓存（宁可多一跳，不能拿旧身份）。
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time

import requests

from config import settings

logger = logging.getLogger(__name__)

# 缓存条目上限：超过后先清过期项，仍超限则整体清空，避免无界增长。
_CACHE_MAX_ENTRIES = 1024

_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()


class Unauthorized(Exception):
    """token 缺失/无效/过期/用户被禁用（对客户端统一表现为「未登录或登录已过期」）。"""


class AuthServiceUnavailable(Exception):
    """Agent introspection 不可达或异常 —— 服务故障，不是用户身份问题。"""


def token_fingerprint(token: str) -> str:
    """日志脱敏用：只暴露 sha256 前 8 位，永不打印 token 原文。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


def _cache_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def reset_cache() -> None:
    """清空进程内缓存（测试与排障用）。"""
    with _lock:
        _cache.clear()


def _store(key: str, data: dict, ttl: float) -> None:
    if ttl <= 0:
        return
    now = time.time()
    with _lock:
        if len(_cache) >= _CACHE_MAX_ENTRIES:
            for k, (deadline, _) in list(_cache.items()):
                if deadline <= now:
                    _cache.pop(k, None)
            if len(_cache) >= _CACHE_MAX_ENTRIES:
                _cache.clear()
        _cache[key] = (now + ttl, data)


def resolve_user(token: str) -> dict:
    """用 Agent introspection 换可信 user_id；失败抛 Unauthorized / AuthServiceUnavailable。

    返回 {"user_id": str, "username": str, "disabled": bool, "exp": int | None}。
    """
    token = (token or "").strip()
    if not token:
        raise Unauthorized("missing token")

    key = _cache_key(token)
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit is not None:
            if hit[0] > now:
                return hit[1]
            _cache.pop(key, None)

    url = f"{settings.agent_base_url.rstrip('/')}/api/auth/introspect"
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=settings.introspect_timeout,
        )
    except requests.RequestException as exc:
        logger.warning(
            "introspect_unreachable token=%s err=%s", token_fingerprint(token), type(exc).__name__
        )
        raise AuthServiceUnavailable("introspection unreachable") from exc

    if resp.status_code in (401, 403):
        logger.info("introspect_rejected token=%s status=%s", token_fingerprint(token), resp.status_code)
        raise Unauthorized("token rejected")
    if resp.status_code != 200:
        logger.warning("introspect_failed token=%s status=%s", token_fingerprint(token), resp.status_code)
        raise AuthServiceUnavailable(f"introspection returned {resp.status_code}")

    try:
        data = resp.json()
    except ValueError as exc:
        logger.warning("introspect_bad_body token=%s", token_fingerprint(token))
        raise AuthServiceUnavailable("introspection body is not JSON") from exc
    if not isinstance(data, dict):
        raise AuthServiceUnavailable("introspection body is not an object")

    user_id = str(data.get("user_id") or "").strip()
    if not user_id:
        logger.warning("introspect_no_user_id token=%s", token_fingerprint(token))
        raise Unauthorized("introspection returned no user_id")
    data["user_id"] = user_id

    try:
        exp = float(data.get("exp") or 0)
    except (TypeError, ValueError):
        exp = 0.0
    ttl = min(float(settings.introspect_cache_ttl), exp - now) if exp > 0 else 0.0
    _store(key, data, ttl)

    logger.info(
        "introspect_ok user_id=%s token=%s cached=%s", user_id, token_fingerprint(token), ttl > 0
    )
    return data
