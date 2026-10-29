"""三个只读 tool 的注册、身份解析与出参裁剪（DESIGN.md §5）。

铁律（§4.1）：这里的函数签名就是发给 LLM 的 schema，**不得出现**
user_id / api_key / Authorization。身份只从请求头、PAPER_RAG_TOKEN 或
login.py 写入的凭据文件取，由 auth.resolve_user() 换出可信 user_id。
"""
from __future__ import annotations

import json
import logging
from typing import Annotated

from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, TextContent
from pydantic import Field

import rag_client
from auth import AuthServiceUnavailable, Unauthorized, resolve_user
from config import load_credentials, settings

logger = logging.getLogger(__name__)

_SEARCH_DESC = (
    "在当前用户的论文库中检索与问题相关的原文片段，返回片段内容、文件名与页码。"
    "只读操作，不会修改任何数据。"
)
_LIST_DESC = "列出当前用户可见的论文文档（文件名、页数、分块数、处理状态）。"
_GET_DESC = "按 document_id 获取单个论文文档的元信息与处理状态。"


# ── 身份 ────────────────────────────────────────────────────────────────


def _token_from(ctx: Context | None) -> str:
    """优先取本次请求的 Authorization: Bearer；再退到环境变量；最后退到凭据文件。

    HTTP 传输才有 headers；stdio 下 ctx.request_context 会抛 ValueError，
    测试里也可能没有请求上下文，因此一律兜底到本地身份（env / 凭据文件）。
    """
    if ctx is not None:
        try:
            headers = ctx.headers
        except Exception:  # 无请求上下文（stdio / 单测）
            headers = None
        if headers:
            for name, value in headers.items():
                if name.lower() != "authorization" or not value:
                    continue
                scheme, _, token = value.partition(" ")
                if scheme.lower() == "bearer" and token.strip():
                    return token.strip()
    if settings.paper_rag_token.strip():
        return settings.paper_rag_token.strip()
    return load_credentials()


# ── 返回构造 ────────────────────────────────────────────────────────────


def _ok(structured: dict, text: str) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=structured,
        is_error=False,
    )


def _error(message: str) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=message)],
        is_error=True,
    )


def _handle(ctx: Context, tool_name: str, fn) -> CallToolResult:
    """统一解析身份 + 错误语义（§5.4）。fn 接收可信 user_id，返回 CallToolResult。"""
    token = _token_from(ctx)
    try:
        user = resolve_user(token)
        return fn(user["user_id"])
    except Unauthorized:
        return _error("未登录或登录已过期")
    except AuthServiceUnavailable:
        logger.warning("tool_identity_unavailable tool=%s", tool_name)
        return _error("身份校验服务暂不可用，请稍后重试")
    except rag_client.RagError as exc:
        if exc.status is not None:
            # 4xx：透传 RAG 的业务文案（如「文档不存在」）
            return _error(exc.message)
        logger.warning("tool_rag_error tool=%s stage=%s msg=%s", tool_name, exc.stage, exc.message)
        message = "检索服务暂不可用，请稍后重试"
        if exc.stage:
            message = f"{message}（stage: {exc.stage}）"
        return _error(message)
    except rag_client.RagUnavailable:
        logger.warning("tool_rag_unavailable tool=%s", tool_name)
        return _error("检索服务暂不可用，请稍后重试")
    except Exception:
        logger.exception("tool_internal_error tool=%s", tool_name)
        return _error("服务内部错误，请稍后重试")


# ── 出参限额（§5.3）────────────────────────────────────────────────────


def _clip_content(value: object) -> tuple[str, bool]:
    text = "" if value is None else str(value)
    limit = settings.max_chunk_chars
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _json_size(payload: dict) -> int:
    return len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))


def _trim_to_budget(structured: dict) -> dict:
    """总体积超限时从尾部减条数，并附 note。"""
    limit = settings.max_response_bytes
    if _json_size(structured) <= limit:
        return structured

    results = structured.get("results")
    if not isinstance(results, list) or not results:
        return structured

    total = len(results)
    dropped = 0
    while results and _json_size(structured) > limit:
        results.pop()
        dropped += 1
    structured["note"] = f"结果过大，已省略 {dropped}/{total} 条片段"
    while results and _json_size(structured) > limit:
        results.pop()
        dropped += 1
        structured["note"] = f"结果过大，已省略 {dropped}/{total} 条片段"
    return structured


def _render_search_text(query: str, structured: dict) -> str:
    lines = [f'检索「{query}」共 {len(structured["results"])} 条片段：']
    for index, item in enumerate(structured["results"], start=1):
        filename = item.get("filename") or "unknown"
        page = item.get("page")
        score = item.get("score")
        header = f"[{index}] {filename}"
        if page is not None:
            header += f" p.{page}"
        if score is not None:
            header += f" (score {score})"
        lines.append(header)
        lines.append(item.get("content") or "")
        if item.get("truncated"):
            lines.append("（片段过长，已截断）")
    if structured.get("note"):
        lines.append(structured["note"])
    return "\n".join(lines)


# ── tools ──────────────────────────────────────────────────────────────


def search_papers(
    ctx: Context,
    query: Annotated[str, Field(description="检索问题或关键词")],
    top_k: Annotated[int, Field(ge=1, le=settings.max_top_k, description="返回片段数量上限")] = 5,
    document_ids: Annotated[
        list[str] | None, Field(description="可选，限定在这些文档内检索")
    ] = None,
) -> CallToolResult:
    """在当前用户的论文库中检索原文片段。"""

    def run(user_id: str) -> CallToolResult:
        k = max(1, min(int(top_k), settings.max_top_k))
        data = rag_client.search(query, k, document_ids, user_id)

        raw_results = data.get("results")
        if not isinstance(raw_results, list):
            raw_results = []

        results: list[dict] = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            content, truncated = _clip_content(item.get("content"))
            entry = {
                "chunk_id": item.get("chunk_id"),
                "document_id": item.get("document_id"),
                "filename": item.get("filename"),
                "page": item.get("page"),
                "score": item.get("score"),
                "content": content,
            }
            if truncated:
                entry["truncated"] = True
            results.append(entry)

        structured: dict = {
            "query": query,
            "results": results,
            "total_chunks": data.get("total_chunks", len(results)),
        }
        if data.get("latency_ms") is not None:
            structured["latency_ms"] = data.get("latency_ms")

        structured = _trim_to_budget(structured)
        return _ok(structured, _render_search_text(query, structured))

    return _handle(ctx, "search_papers", run)


def list_documents(ctx: Context) -> CallToolResult:
    """列出当前用户可见的论文文档。"""

    def run(user_id: str) -> CallToolResult:
        data = rag_client.list_documents(user_id)
        documents = data.get("documents")
        if not isinstance(documents, list):
            documents = []

        structured = {"documents": documents, "total": len(documents)}
        if _json_size(structured) > settings.max_response_bytes:
            structured = {"documents": documents[:50], "total": len(documents)}
            structured["note"] = "文档过多，已只返回前 50 条"

        if not documents:
            text = "当前用户的论文库为空。"
        else:
            lines = [f"共 {len(documents)} 篇文档："]
            for index, doc in enumerate(structured["documents"], start=1):
                if not isinstance(doc, dict):
                    continue
                name = doc.get("filename") or doc.get("title") or "unknown"
                status = doc.get("status") or "unknown"
                pages = doc.get("page_count")
                chunks = doc.get("chunk_count")
                lines.append(
                    f"{index}. {name} (id: {doc.get('id')}) — {status}"
                    f"，{pages} 页 / {chunks} 块"
                )
            if structured.get("note"):
                lines.append(structured["note"])
            text = "\n".join(lines)
        return _ok(structured, text)

    return _handle(ctx, "list_documents", run)


def get_document(
    ctx: Context,
    document_id: Annotated[str, Field(description="文档 ID（可从 list_documents 获取）")],
) -> CallToolResult:
    """按 ID 获取文档元信息与处理状态。"""

    def run(user_id: str) -> CallToolResult:
        data = rag_client.get_document(document_id, user_id)
        document = data.get("document")
        if not isinstance(document, dict):
            return _error("文档不存在")

        structured = {"document": document}
        name = document.get("filename") or document.get("title") or "unknown"
        lines = [
            f"{name} (id: {document.get('id')})",
            f"状态: {document.get('status') or 'unknown'}",
            f"页数: {document.get('page_count')}，分块数: {document.get('chunk_count')}",
        ]
        if document.get("error"):
            lines.append(f"错误: {document.get('error')}")
        return _ok(structured, "\n".join(lines))

    return _handle(ctx, "get_document", run)


def register_all(mcp) -> None:
    """把三个 tool 注册进 MCPServer 实例（structured_output=False 以原样返回 CallToolResult）。"""
    mcp.tool(name="search_papers", description=_SEARCH_DESC, structured_output=False)(search_papers)
    mcp.tool(name="list_documents", description=_LIST_DESC, structured_output=False)(list_documents)
    mcp.tool(name="get_document", description=_GET_DESC, structured_output=False)(get_document)
