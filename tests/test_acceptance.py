"""DESIGN.md §10 验收 1-8：用假 RAG / 假 Agent 跑，不依赖真实服务与模型。"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import auth
import rag_client
from config import settings
from server import mcp

API_KEY = "test-key"
EXP_SECONDS = 1.0


class Stub:
    def __init__(self) -> None:
        self.search_headers: list[dict] = []
        self.search_bodies: list[dict] = []
        self.introspect_tokens: list[str] = []
        self.fail_search = False
        self.token_exp = time.time() + EXP_SECONDS


STUB = Stub()

_USERS = {
    "token-a": ("user-a", "alice"),
    "token-b": ("user-b", "bob"),
    "token-exp": ("user-a", "alice"),
    "token-disabled": ("user-x", "mallory"),
}

_DOCS = {
    "user-a": [
        {"id": "doc-a", "filename": "attention.pdf", "status": "ready", "page_count": 12, "chunk_count": 340},
        {"id": "doc-a2", "filename": "transformer.pdf", "status": "ready", "page_count": 8, "chunk_count": 120},
    ],
    "user-b": [
        {"id": "doc-b", "filename": "secret.pdf", "status": "ready", "page_count": 3, "chunk_count": 20},
    ],
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静音
        pass

    # ── helpers ──
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self) -> str:
        raw = self.headers.get("Authorization") or ""
        scheme, _, token = raw.partition(" ")
        return token.strip() if scheme.lower() == "bearer" else ""

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def _user_id(self) -> str:
        uid = (self.headers.get("X-User-ID") or "").strip()
        if not uid or self.headers.get("X-API-Key") != API_KEY:
            return ""
        return uid

    # ── routes ──
    def do_POST(self):
        if self.path == "/api/auth/introspect":
            token = self._bearer()
            STUB.introspect_tokens.append(token)
            if token == "token-disabled":
                return self._send_json(401, {"detail": "账号已被禁用"})
            if token not in _USERS:
                return self._send_json(401, {"detail": "无效的登录凭证"})
            user_id, username = _USERS[token]
            exp = STUB.token_exp if token == "token-exp" else time.time() + 3600
            if token == "token-exp" and time.time() >= exp:
                return self._send_json(401, {"detail": "登录已过期，请重新登录"})
            return self._send_json(
                200,
                {"user_id": user_id, "username": username, "disabled": False, "exp": exp},
            )

        if self.path == "/api/v1/search":
            STUB.search_headers.append(dict(self.headers))
            body = self._read_json()
            STUB.search_bodies.append(body)
            if STUB.fail_search:
                return self._send_json(500, {"error": "boom"})
            user_id = self._user_id()
            if not user_id:
                return self._send_json(401, {"error": "unauthorized"})

            query = body.get("query", "")
            if query == "big":
                results = [
                    {
                        "chunk_id": f"chunk-{i}",
                        "document_id": "doc-a",
                        "filename": "big.pdf",
                        "page": i + 1,
                        "score": 0.9,
                        "content": "中文片段" * 1250,  # 5000 字符
                    }
                    for i in range(20)
                ]
            elif query == "missing":
                results = []
            else:
                doc = "doc-a" if user_id == "user-a" else "doc-b"
                results = [
                    {
                        "chunk_id": f"{doc}-c1",
                        "document_id": doc,
                        "filename": "attention.pdf" if doc == "doc-a" else "secret.pdf",
                        "page": 3,
                        "score": 0.82,
                        "content": "注意力机制的核心是对上下文加权。",
                    }
                ]
            payload = {"results": results, "total_chunks": len(results), "latency_ms": 12}

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")  # 故意不带 charset
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for event, data in (
                ("progress", {"stage": "searching"}),
                ("result", payload),
            ):
                chunk = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                raw = chunk.encode("utf-8")
                self.wfile.write(f"{len(raw):X}\r\n".encode("ascii") + raw + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return

        return self._send_json(404, {"error": "not found"})

    def do_GET(self):
        if self.path == "/api/v1/documents":
            user_id = self._user_id()
            if not user_id:
                return self._send_json(401, {"error": "unauthorized"})
            return self._send_json(200, {"documents": _DOCS.get(user_id, [])})

        if self.path.startswith("/api/v1/documents/"):
            user_id = self._user_id()
            if not user_id:
                return self._send_json(401, {"error": "unauthorized"})
            doc_id = self.path.rsplit("/", 1)[-1]
            for doc in _DOCS.get(user_id, []):
                if doc["id"] == doc_id:
                    return self._send_json(200, {"document": doc})
            return self._send_json(404, {"error": "文档不存在"})

        return self._send_json(404, {"error": "not found"})


@pytest.fixture(scope="module")
def stub_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(autouse=True)
def env(stub_url, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "rag_base_url", stub_url)
    monkeypatch.setattr(settings, "agent_base_url", stub_url)
    monkeypatch.setattr(settings, "rag_api_key", API_KEY)
    monkeypatch.setattr(settings, "paper_rag_token", "")
    # 凭据文件兜底链：指向空文件路径，避免读到测试机的真实 ~/.paper-rag-mcp/credentials.json
    monkeypatch.setattr(settings, "credentials_file", str(tmp_path / "credentials.json"))
    monkeypatch.setattr(settings, "introspect_cache_ttl", 60)
    monkeypatch.setattr(settings, "introspect_timeout", 5.0)
    monkeypatch.setattr(settings, "search_timeout", 10.0)
    monkeypatch.setattr(settings, "connect_timeout", 5.0)
    STUB.fail_search = False
    STUB.search_headers.clear()
    STUB.search_bodies.clear()
    STUB.introspect_tokens.clear()
    auth.reset_cache()
    yield
    auth.reset_cache()


def call(name: str, arguments: dict):
    return asyncio.run(mcp.call_tool(name, arguments))


def text_of(result) -> str:
    return result.content[0].text


def test_1_no_token():
    result = call("search_papers", {"query": "attention"})
    assert result.is_error is True
    assert text_of(result) == "未登录或登录已过期"
    # 身份解析失败时不得向 RAG 发请求
    assert STUB.search_headers == []


def test_2_user_isolation(monkeypatch):
    monkeypatch.setattr(settings, "paper_rag_token", "token-a")
    result = call("search_papers", {"query": "attention"})
    assert result.is_error is False
    assert result.structured_content["results"][0]["document_id"] == "doc-a"
    assert STUB.search_headers[-1]["X-User-ID"] == "user-a"

    auth.reset_cache()
    monkeypatch.setattr(settings, "paper_rag_token", "token-b")
    result_b = call("search_papers", {"query": "attention"})
    assert result_b.structured_content["results"][0]["document_id"] == "doc-b"
    assert STUB.search_headers[-1]["X-User-ID"] == "user-b"


def test_3_user_id_in_args_cannot_override_identity(monkeypatch):
    monkeypatch.setattr(settings, "paper_rag_token", "token-a")
    result = call("search_papers", {"query": "attention", "user_id": "user-b"})
    assert result.is_error is False
    # 入参里的 user_id 不影响身份：RAG 收到的仍是 token-a 对应的 user-a
    assert STUB.search_headers[-1]["X-User-ID"] == "user-a"
    assert result.structured_content["results"][0]["document_id"] == "doc-a"


def test_4_normal_search(monkeypatch):
    monkeypatch.setattr(settings, "paper_rag_token", "token-a")
    result = call("search_papers", {"query": "attention", "top_k": 5})
    structured = result.structured_content
    assert structured["query"] == "attention"
    assert structured["total_chunks"] == 1
    item = structured["results"][0]
    assert set(item) >= {"chunk_id", "document_id", "filename", "page", "score", "content"}
    assert item["content"] == "注意力机制的核心是对上下文加权。"  # 中文无乱码
    assert "[1] attention.pdf p.3 (score 0.82)" in text_of(result)
    assert STUB.search_bodies[-1] == {"query": "attention", "top_k": 5}


def test_5_rag_down(monkeypatch):
    monkeypatch.setattr(settings, "paper_rag_token", "token-a")
    monkeypatch.setattr(settings, "rag_base_url", "http://127.0.0.1:1")
    result = call("search_papers", {"query": "attention"})
    assert result.is_error is True
    assert text_of(result) == "检索服务暂不可用，请稍后重试"

    # 进程仍可用：恢复后同一进程内再次调用成功
    monkeypatch.setattr(settings, "rag_base_url", settings.agent_base_url)
    again = call("search_papers", {"query": "attention"})
    assert again.is_error is False


def test_5b_rag_500(monkeypatch):
    monkeypatch.setattr(settings, "paper_rag_token", "token-a")
    STUB.fail_search = True
    result = call("search_papers", {"query": "attention"})
    assert result.is_error is True
    assert text_of(result) == "检索服务暂不可用，请稍后重试"


def test_6_expired_token_not_served_from_cache(monkeypatch):
    monkeypatch.setattr(settings, "paper_rag_token", "token-exp")
    STUB.token_exp = time.time() + EXP_SECONDS
    first = call("search_papers", {"query": "attention"})
    assert first.is_error is False
    assert STUB.introspect_tokens == ["token-exp"]

    # 缓存 TTL 不得超过 token 剩余有效期：exp 过后必须重新 introspection 并拒绝
    time.sleep(EXP_SECONDS + 0.2)
    second = call("search_papers", {"query": "attention"})
    assert second.is_error is True
    assert text_of(second) == "未登录或登录已过期"
    assert len(STUB.introspect_tokens) == 2


def test_7_disabled_user(monkeypatch):
    monkeypatch.setattr(settings, "paper_rag_token", "token-disabled")
    result = call("search_papers", {"query": "attention"})
    assert result.is_error is True
    assert text_of(result) == "未登录或登录已过期"
    assert STUB.search_headers == []


def test_8_output_limits(monkeypatch):
    monkeypatch.setattr(settings, "paper_rag_token", "token-a")
    result = call("search_papers", {"query": "big", "top_k": 20})
    assert result.is_error is False
    structured = result.structured_content

    for item in structured["results"]:
        assert len(item["content"]) == settings.max_chunk_chars
        assert item.get("truncated") is True

    raw = json.dumps(structured, ensure_ascii=False).encode("utf-8")
    assert len(raw) <= settings.max_response_bytes
    assert "note" in structured
    assert len(structured["results"]) < 20


def test_top_k_schema_limit():
    with pytest.raises(Exception):
        call("search_papers", {"query": "attention", "top_k": 21})


def test_documents_tools(monkeypatch):
    monkeypatch.setattr(settings, "paper_rag_token", "token-a")
    listed = call("list_documents", {})
    assert listed.is_error is False
    assert [d["id"] for d in listed.structured_content["documents"]] == ["doc-a", "doc-a2"]

    got = call("get_document", {"document_id": "doc-a"})
    assert got.is_error is False
    assert got.structured_content["document"]["filename"] == "attention.pdf"

    # 别人的文档：RAG 404 → 透传业务文案
    missing = call("get_document", {"document_id": "doc-b"})
    assert missing.is_error is True
    assert text_of(missing) == "文档不存在"


def test_sse_error_event_with_stage(monkeypatch):
    monkeypatch.setattr(settings, "paper_rag_token", "token-a")

    def boom(*args, **kwargs):
        raise rag_client.RagError("embedding failed", stage="embed")

    monkeypatch.setattr(rag_client, "search", boom)
    result = call("search_papers", {"query": "attention"})
    assert result.is_error is True
    assert text_of(result) == "检索服务暂不可用，请稍后重试（stage: embed）"
