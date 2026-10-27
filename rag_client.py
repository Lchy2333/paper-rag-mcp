"""RAG REST 封装：注入 X-API-Key / X-User-ID，解析 SSE，映射错误语义。

约定（DESIGN.md §4.4 / §5.3 / §6.2）：
- 服务鉴权只认 X-API-Key；X-User-ID 必发（RAG 缺失即 401）。
- RAG 的 SSE 是 text/event-stream 且不带 charset，requests 会按 latin-1 解码，
  必须用 iter_lines() 拿原始字节后手动 decode("utf-8")，否则中文乱码。
- 4xx 视为业务错误（透传文案），5xx / 网络异常 / 超时归为服务不可用。
"""
from __future__ import annotations

import json
import logging
from urllib.parse import quote

import requests

from config import settings

logger = logging.getLogger(__name__)


class RagError(Exception):
    """RAG 明确拒绝：4xx 业务错误，或 SSE 下发的 error 事件。"""

    def __init__(self, message: str, status: int | None = None, stage: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.stage = stage


class RagUnavailable(Exception):
    """RAG 不可用：5xx、连接失败、超时、响应不是 JSON。"""


def _base() -> str:
    return settings.rag_base_url.rstrip("/")


def _headers(user_id: str) -> dict[str, str]:
    headers = {"X-User-ID": user_id}
    api_key = settings.rag_api_key.strip()
    if api_key:
        headers["X-API-Key"] = api_key
    return headers


def _error_message(resp: requests.Response) -> str:
    """按 RAG 的既有文案结构取错误信息：error / detail / message。"""
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        for field in ("error", "detail", "message"):
            value = body.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
    text = (resp.text or "").strip()
    if text:
        return text[:200]
    return f"RAG 返回 HTTP {resp.status_code}"


def _raise_for_status(resp: requests.Response) -> None:
    if resp.status_code < 400:
        return
    message = _error_message(resp)
    if resp.status_code >= 500:
        logger.warning("rag_server_error status=%s msg=%s", resp.status_code, message)
        raise RagUnavailable(message)
    raise RagError(message, status=resp.status_code)


def _error_from_event(data: object) -> RagError:
    message = "检索失败"
    stage = None
    if isinstance(data, dict):
        raw = data.get("error") or data.get("message") or data.get("detail")
        if isinstance(raw, str) and raw.strip():
            message = raw.strip()
        raw_stage = data.get("stage")
        if isinstance(raw_stage, str) and raw_stage.strip():
            stage = raw_stage.strip()
    return RagError(message, stage=stage)


def _parse_sse(resp: requests.Response) -> dict:
    """按 SSE 规范逐行解析；返回 event:result 的 payload。"""
    event = ""
    data_lines: list[str] = []
    result: dict | None = None

    def flush() -> dict | None:
        nonlocal event, data_lines
        evt = event
        payload = "\n".join(data_lines)
        event, data_lines = "", []
        if not payload.strip():
            return None
        try:
            data = json.loads(payload)
        except ValueError:
            logger.warning("rag_sse_bad_json event=%s", evt)
            return None
        if evt == "error":
            raise _error_from_event(data)
        if evt == "result":
            if not isinstance(data, dict):
                raise RagError("检索结果格式错误")
            return data
        return None

    for raw in resp.iter_lines():
        if raw is None:
            continue
        line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        if not line:
            got = flush()
            if got is not None:
                result = got
            continue
        if line.startswith(":"):  # 注释/心跳行
            continue
        field, sep, value = line.partition(":")
        if not sep:
            continue
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event = value.strip()
        elif field == "data":
            data_lines.append(value)

    got = flush()
    if got is not None:
        result = got
    if result is None:
        raise RagError("检索流未返回结果")
    return result


def search(query: str, top_k: int, document_ids: list[str] | None, user_id: str) -> dict:
    body: dict = {"query": query, "top_k": top_k}
    if document_ids:
        body["document_ids"] = list(document_ids)

    url = f"{_base()}/api/v1/search"
    try:
        with requests.post(
            url,
            json=body,
            headers=_headers(user_id),
            stream=True,
            timeout=(settings.connect_timeout, settings.search_timeout),
        ) as resp:
            _raise_for_status(resp)
            return _parse_sse(resp)
    except requests.RequestException as exc:
        logger.warning("rag_search_unreachable err=%s", type(exc).__name__)
        raise RagUnavailable(f"search request failed: {type(exc).__name__}") from exc


def _get_json(url: str, user_id: str) -> dict:
    try:
        resp = requests.get(
            url,
            headers=_headers(user_id),
            timeout=(settings.connect_timeout, settings.search_timeout),
        )
    except requests.RequestException as exc:
        logger.warning("rag_get_unreachable err=%s", type(exc).__name__)
        raise RagUnavailable(f"request failed: {type(exc).__name__}") from exc

    _raise_for_status(resp)
    try:
        data = resp.json()
    except ValueError as exc:
        raise RagUnavailable("RAG 返回的不是 JSON") from exc
    if not isinstance(data, dict):
        raise RagUnavailable("RAG 返回结构异常")
    return data


def list_documents(user_id: str) -> dict:
    """GET /api/v1/documents → {"documents": [...]}"""
    return _get_json(f"{_base()}/api/v1/documents", user_id)


def get_document(document_id: str, user_id: str) -> dict:
    """GET /api/v1/documents/{id} → {"document": {...}}"""
    quoted = quote(str(document_id), safe="")
    return _get_json(f"{_base()}/api/v1/documents/{quoted}", user_id)
