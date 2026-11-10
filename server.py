"""paper-rag-mcp 入口：MCPServer 实例 + 传输选择（DESIGN.md §6.1 / §6.3）。

stdio：本地单人，身份来自 PAPER_RAG_TOKEN 环境变量。
streamable-http：多人共用，身份来自每次 HTTP 请求的 Authorization 头。
"""
from __future__ import annotations

import logging
import sys

from mcp.server.mcpserver import MCPServer

from config import settings
from request_context import request_id_var
from tools import register_all

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "PaperRAG 论文库检索服务（只读）。所有工具都以当前连接的用户身份执行，"
    "按用户自动隔离数据；不需要也不能指定 user_id。"
    "先调用 list_documents 了解可用文档，再用 search_papers 检索原文片段。"
)

mcp = MCPServer(
    "paper-rag",
    title="PaperRAG",
    description="论文库检索：检索原文片段、列出文档、查看文档元信息。",
    instructions=INSTRUCTIONS,
)
register_all(mcp)


class RequestIdFilter(logging.Filter):
    """与 agent 仓同机制：每条日志输出前现场读 contextvar，把 rid 贴进记录。

    Filter 是钩子不是快照——每条日志经过时执行一次 request_id_var.get()，
    tools/_handle 里 set 的 rid 因此出现在本次调用产生的所有日志行上。
    """

    def filter(self, record):
        record.request_id = request_id_var.get()
        return True


def _configure_logging() -> None:
    """日志必须写 stderr：stdio 传输的 stdout 是 JSON-RPC 通道，写入即破坏协议。

    注意：mcp SDK 在 import 时已给 root 装了 handler（裸 %(message)s），
    logging.basicConfig 此时是空操作（它只在 root 无 handler 时生效），
    所以必须直接改写已有 handler 的 formatter，否则格式与 rid 都不会生效。
    """
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not root.handlers:
        root.addHandler(logging.StreamHandler(sys.stderr))
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"
    )
    # 关卡挂 root 的所有 handler：MCP 全部 logger（tools/auth/rag_client）都冒泡到 root 输出
    for handler in root.handlers:
        handler.setFormatter(formatter)
        handler.addFilter(RequestIdFilter())


def main() -> None:
    _configure_logging()
    settings.validate()

    if settings.mcp_transport == "streamable-http":
        logger.info(
            "mcp_started transport=streamable-http host=%s port=%s",
            settings.mcp_host,
            settings.mcp_port,
        )
        mcp.run(
            transport="streamable-http",
            host=settings.mcp_host,
            port=settings.mcp_port,
        )
    else:
        logger.info("mcp_started transport=stdio")
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
