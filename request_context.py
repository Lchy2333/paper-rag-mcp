"""MCP 请求级上下文：request_id 的唯一权威存放处。

与 paper-rag-agent 仓同模式（两仓独立进程，各自持一份，不共享代码）：
- 生产：tools._handle 每个 tools/call 生成/透传一次并 set，finally reset；
- 消费：server.py 的 RequestIdFilter 每条日志输出前现场读取，自动贴进日志行；
- 传播：rag_client / auth 出站请求带 X-Request-ID——Agent/RAG 侧中间件
  会透传同一个 id，一次工具调用在三段日志里可 grep 同一 rid。
"""
import contextvars
import uuid

# default="-"：tools/call 之外的日志（启动、后台线程）读到 "-" 而不是报错
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)


def new_request_id() -> str:
    """生成 12 位 hex rid，与 Agent/RAG 侧同长度，跨服务 grep 友好。"""
    return uuid.uuid4().hex[:12]
