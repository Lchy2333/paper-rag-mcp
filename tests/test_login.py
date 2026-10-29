"""login.py 与凭据文件兜底链的测试：不依赖真实 Agent 与浏览器。

覆盖：
- 凭据文件读写 roundtrip（config.save_credentials / load_credentials）；
- _token_from 的三级兜底顺序（请求头 > 环境变量 > 凭据文件）；
- stdio 模式启动校验放行/拒绝；
- serve_login_once 端到端：假 Agent + 本地登录页，覆盖错误密码、
  state 校验、成功登录拿到 token（不写文件，写文件是 main 的职责）。
"""
from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import tools
from config import credentials_path, load_credentials, save_credentials, settings
from login import serve_login_once


@pytest.fixture
def cred_file(tmp_path, monkeypatch):
    """把凭据文件指到测试临时目录，避免读写真实 ~/.paper-rag-mcp/credentials.json。"""
    path = tmp_path / "credentials.json"
    monkeypatch.setattr(settings, "credentials_file", str(path))
    return path


# ── 凭据文件 ────────────────────────────────────────────────────────────


def test_credentials_roundtrip(cred_file):
    assert load_credentials() == ""
    assert not credentials_path().exists()
    save_credentials("tok-1")
    assert load_credentials() == "tok-1"
    payload = json.loads(cred_file.read_text(encoding="utf-8"))
    assert payload["access_token"] == "tok-1"


def test_load_credentials_tolerates_corrupt_file(cred_file):
    cred_file.parent.mkdir(parents=True, exist_ok=True)
    cred_file.write_text("not-json", encoding="utf-8")
    assert load_credentials() == ""


def test_save_credentials_overwrites(cred_file):
    save_credentials("old")
    save_credentials("new")
    assert load_credentials() == "new"


# ── _token_from 三级兜底 ────────────────────────────────────────────────


def test_token_from_env_beats_credentials(cred_file, monkeypatch):
    save_credentials("file-token")
    monkeypatch.setattr(settings, "paper_rag_token", "env-token")
    assert tools._token_from(None) == "env-token"


def test_token_from_falls_back_to_credentials(cred_file, monkeypatch):
    monkeypatch.setattr(settings, "paper_rag_token", "")
    save_credentials("file-token")
    assert tools._token_from(None) == "file-token"


def test_token_from_empty_when_nothing_configured(cred_file, monkeypatch):
    monkeypatch.setattr(settings, "paper_rag_token", "")
    assert tools._token_from(None) == ""


# ── stdio 启动校验 ──────────────────────────────────────────────────────


def test_validate_stdio_requires_something(cred_file, monkeypatch):
    monkeypatch.setattr(settings, "mcp_transport", "stdio")
    monkeypatch.setattr(settings, "paper_rag_token", "")
    with pytest.raises(RuntimeError):
        settings.validate()
    save_credentials("file-token")
    settings.validate()  # 凭据文件兜底后放行


def test_validate_stdio_env_still_works(cred_file, monkeypatch):
    monkeypatch.setattr(settings, "mcp_transport", "stdio")
    monkeypatch.setattr(settings, "paper_rag_token", "env-token")
    settings.validate()


# ── serve_login_once 端到端（假 Agent）──────────────────────────────────


class FakeAgentHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass

    def _json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if body.get("username") == "alice" and body.get("password") == "right-pw":
            return self._json(200, {"access_token": "login-jwt", "user_id": "u-1"})
        return self._json(401, {"detail": "用户名或密码错误"})


@pytest.fixture
def fake_agent_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAgentHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _fetch_page(url: str) -> str:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.read().decode("utf-8")


def _submit(url: str, state: str, username: str, password: str) -> tuple[int, str]:
    data = urllib.parse.urlencode(
        {"state": state, "username": username, "password": password}
    ).encode("utf-8")
    req = urllib.request.Request(url.rstrip("/") + "/submit", data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def test_serve_login_once_end_to_end(cred_file, monkeypatch, fake_agent_url):
    monkeypatch.setattr(settings, "agent_base_url", fake_agent_url)

    ready = threading.Event()
    holder: dict[str, str] = {}

    def announce(url: str) -> None:
        holder["url"] = url
        ready.set()

    result: dict[str, str] = {}

    def run() -> None:
        result["token"] = serve_login_once(timeout=20, announce=announce)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(10), "登录服务未在预期时间内就绪"

    # 1) 登录页可访问且携带 state
    page = _fetch_page(holder["url"])
    match = re.search(r'name="state" value="([^"]+)"', page)
    assert match, "登录页缺少 state 隐藏字段"
    state = match.group(1)

    # 2) state 不匹配 → 403（防本机端口被其他页面 CSRF）
    status, _ = _submit(holder["url"], state="bad", username="alice", password="right-pw")
    assert status == 403

    # 3) 错误密码 → 返回登录页 + Agent 的业务文案，服务继续存活
    status, page = _submit(holder["url"], state=state, username="alice", password="wrong-pw")
    assert status == 200
    assert "用户名或密码错误" in page

    # 4) 正确密码 → 成功页 + serve_login_once 返回 token
    status, page = _submit(holder["url"], state=state, username="alice", password="right-pw")
    assert status == 200
    assert "登录成功" in page

    thread.join(20)
    assert result["token"] == "login-jwt"
    # serve_login_once 不写文件（写文件是 main 的职责），此处保持空
    assert load_credentials() == ""
