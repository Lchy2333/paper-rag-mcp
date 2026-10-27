"""paper-rag-mcp 配置：从 .env / 环境变量读取（对齐 paper-rag-agent 的 pydantic-settings 风格）。

约定（DESIGN.md §7）：
- RAG_API_KEY 只存在于本进程环境变量，不下发给任何终端用户，不进日志/响应体。
- stdio 模式必须配置 PAPER_RAG_TOKEN（用户 JWT）；streamable-http 模式从每次 HTTP
  请求的 Authorization 头取 token，不读环境变量。
- 启动校验放在 main() 里调用（不在 import 时），便于测试/CI 无 .env 也能 import。
"""
import logging
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── RAG 服务（数据所有者）──
    rag_base_url: str = "http://localhost:8080"
    # 服务级密钥；为空表示 RAG 未启用鉴权（本地开发），此时不发送 X-API-Key
    rag_api_key: str = ""

    # ── Agent（身份校验方）──
    agent_base_url: str = "http://localhost:8000"

    # ── MCP 传输 ──
    mcp_transport: str = "stdio"  # stdio | streamable-http
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8765

    # stdio 模式下的用户身份（用户 JWT）；禁止填 admin token（DESIGN.md §9）
    paper_rag_token: str = ""

    # ── 超时 ──
    search_timeout: float = 90.0  # 检索读超时（与 RAGtools.json 的 search_rag 对齐）
    connect_timeout: float = 15.0  # 连接超时
    introspect_timeout: float = 5.0
    introspect_cache_ttl: int = 60  # 秒，且不得超过 token 剩余有效期

    # ── 出参限额（DESIGN.md §5.3）──
    max_top_k: int = 20
    max_chunk_chars: int = 2000
    max_response_bytes: int = 64 * 1024

    def validate(self) -> None:
        """启动期校验：缺关键项直接拒绝启动，不猜默认值。"""
        if not self.rag_base_url.strip():
            raise RuntimeError("RAG_BASE_URL 不能为空")
        if not self.agent_base_url.strip():
            raise RuntimeError("AGENT_BASE_URL 不能为空")
        if self.mcp_transport not in ("stdio", "streamable-http"):
            raise RuntimeError(f"MCP_TRANSPORT 只能是 stdio 或 streamable-http，当前: {self.mcp_transport}")
        if not self.rag_api_key.strip():
            logger.warning("RAG_API_KEY 为空：调用 RAG 将不带 X-API-Key（仅适用于 RAG 未启用鉴权的本地开发环境）")
        if self.mcp_transport == "stdio" and not self.paper_rag_token.strip():
            raise RuntimeError(
                "stdio 模式必须配置 PAPER_RAG_TOKEN（用户在浏览器登录页拿到的 JWT）。"
                "不要把 admin 的 token 配进来。"
            )


settings = Settings()
