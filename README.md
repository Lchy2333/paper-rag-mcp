# paper-rag-mcp

PaperRAG 的 MCP Server：把论文库的检索能力以 [Model Context Protocol](https://modelcontextprotocol.io) 工具的形式暴露给 LLM 客户端（Claude Desktop、Cursor、Trae 等）。

只读、按用户隔离：每次工具调用都以**当前登录用户**的身份访问 RAG，模型既不能也不需要指定 `user_id`。

## 能力（P0）

| Tool | 说明 | 参数 |
| --- | --- | --- |
| `search_papers` | 在用户论文库中检索原文片段，返回内容、文件名、页码、相关度 | `query`(必填)、`top_k`(默认 5，1–20)、`document_ids`(可选) |
| `list_documents` | 列出当前用户可见的文档及处理状态 | 无 |
| `get_document` | 按 `document_id` 查看单个文档的元信息 | `document_id`(必填) |

## 身份链路

MCP 协议不支持在 `tools/call` 里附加 header，身份只能在建立连接 / 启动进程时绑定：

```
浏览器登录 paper-rag-agent  →  拿到用户 JWT
        │
        ├── stdio 模式：JWT 写进 PAPER_RAG_TOKEN 环境变量
        └── streamable-http 模式：JWT 放在每次请求的 Authorization: Bearer
                    │
                    ▼
        MCP 调 POST {AGENT_BASE_URL}/api/auth/introspect 换取可信 user_id
                    │
                    ▼
        MCP 调 RAG：X-API-Key: $RAG_API_KEY + X-User-ID: <user_id>
```

安全约束：

- tool schema 里**永远不出现** `user_id` / `api_key` / `Authorization`。
- MCP 不信任任何客户端传入的 `X-User-ID`，也不在本地解码 JWT，只认 Agent 的 introspection 结果。
- `RAG_API_KEY` 只存在于 MCP 进程环境变量，不下发给终端用户，不写入日志与响应体。
- **禁止使用 admin token**：RAG 侧 admin 会绕过用户过滤，等于把全库交给该用户。

## 前置条件

1. **paper-rag-agent** 需提供身份校验接口（本仓库依赖，不含该实现）：

   ```
   POST /api/auth/introspect
   Authorization: Bearer <用户 JWT>
   → 200 {"user_id": "1", "username": "alice", "disabled": false, "exp": 1730000000}
   ```

   401/403 表示 token 无效 → MCP 回「未登录或登录已过期」。

2. **paper-rag**（RAG 服务）可访问，且已启用 `X-API-Key` / `X-User-ID` 鉴权中间件。

## 安装

```bash
git clone https://github.com/Lchy2333/paper-rag-mcp.git
cd paper-rag-mcp
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
# source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env   # 然后按需填写
```

依赖：`mcp>=2.0`、`requests`、`pydantic-settings`（Python 3.10+）。

## 配置

全部配置来自环境变量或同目录 `.env`（`.env` 已在 `.gitignore` 中）。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `RAG_BASE_URL` | `http://localhost:8080` | RAG 服务地址 |
| `RAG_API_KEY` | 空 | MCP 专属服务级密钥。为空表示 RAG 未启用鉴权（仅限本地开发） |
| `AGENT_BASE_URL` | `http://localhost:8000` | Agent 地址，用于 `/api/auth/introspect` |
| `MCP_TRANSPORT` | `stdio` | `stdio` \| `streamable-http` |
| `MCP_HOST` | `127.0.0.1` | 仅 streamable-http 生效 |
| `MCP_PORT` | `8765` | 仅 streamable-http 生效 |
| `PAPER_RAG_TOKEN` | 空 | **stdio 模式必填**，用户 JWT；streamable-http 忽略 |
| `SEARCH_TIMEOUT` | `90` | 检索读超时（秒） |
| `CONNECT_TIMEOUT` | `15` | 连接超时（秒） |
| `INTROSPECT_TIMEOUT` | `5` | 身份校验超时（秒） |
| `INTROSPECT_CACHE_TTL` | `60` | 身份缓存 TTL（秒），实际不超过 token 剩余有效期 |
| `MAX_TOP_K` | `20` | `top_k` 上限 |
| `MAX_CHUNK_CHARS` | `2000` | 单片段最大字符数，超出截断并标记 `truncated` |
| `MAX_RESPONSE_BYTES` | `65536` | 单次返回总字节上限，超出从尾部减条数并附 `note` |

启动期会做校验：`MCP_TRANSPORT` 非法、`RAG_BASE_URL` 为空、stdio 模式缺 `PAPER_RAG_TOKEN` 都会直接拒绝启动。

## 运行

```bash
python server.py
```

- **stdio**（本地单人）：日志写 stderr，stdout 是 JSON-RPC 通道。
- **streamable-http**（多人共用）：监听 `MCP_HOST:MCP_PORT`，身份取自每次请求的 `Authorization` 头，请部署在内网 / 网关之后。

> 日志必须走 stderr。任何 print 到 stdout 都会破坏 stdio 协议。

### 客户端配置示例（stdio）

Claude Desktop / Cursor / Trae 的 `mcpServers`：

```json
{
  "mcpServers": {
    "paper-rag": {
      "command": "d:/Project/PaperRAG/paper-rag-mcp/.venv/Scripts/python.exe",
      "args": ["d:/Project/PaperRAG/paper-rag-mcp/server.py"],
      "env": {
        "RAG_BASE_URL": "http://localhost:8080",
        "RAG_API_KEY": "<RAG 服务级密钥>",
        "AGENT_BASE_URL": "http://localhost:8000",
        "PAPER_RAG_TOKEN": "<当前用户 JWT>"
      }
    }
  }
}
```

`PAPER_RAG_TOKEN` 会随进程常驻，token 过期后需要重启 MCP 或换用 streamable-http。

### 调用示例

`search_papers`：

```json
{"query": "attention 机制的计算复杂度", "top_k": 3}
```

返回的 `content` 文本形如：

```
检索「attention 机制的计算复杂度」共 3 条片段：
[1] attention_is_all_you_need.pdf p.3 (score 0.82)
<原文片段>
（片段过长，已截断）
```

同时返回结构化数据：`query`、`results[]`（`chunk_id` / `document_id` / `filename` / `page` / `score` / `content` / 可选 `truncated`）、`total_chunks`、`latency_ms`。

## 错误语义

| 场景 | 返回文案 |
| --- | --- |
| 无 token / token 无效或过期 / 用户被禁用 | 未登录或登录已过期 |
| Agent introspection 不可达或 5xx | 身份校验服务暂不可用，请稍后重试 |
| RAG 返回 4xx | 透传 RAG 的业务文案（如「文档不存在」） |
| RAG 5xx / 超时 / 流中断 | 检索服务暂不可用，请稍后重试 |
| SSE `event: error` | 检索服务暂不可用，请稍后重试（stage: xxx） |
| 其他未预期异常 | 服务内部错误，请稍后重试 |

实现细节：RAG 的检索接口返回 `text/event-stream` 且不带 charset，`requests` 会按 latin-1 解码，因此客户端用 `iter_lines()` 取原始字节后手动 `decode("utf-8")`。

## 测试

```bash
.venv/Scripts/python.exe -m pytest tests -q
```

验收测试用本地假 Agent / 假 RAG（SSE 故意不带 charset）覆盖：正常检索、用户隔离、token 过期/禁用、身份服务不可用、RAG 4xx/5xx、SSE error stage、超大结果截断、`top_k` 越界、文档列表与详情。

## 目录结构

```
paper-rag-mcp/
├── server.py         # MCPServer 实例 + 传输选择 + 启动校验
├── config.py         # pydantic-settings 配置
├── auth.py           # Bearer → Agent introspection → user_id（含指纹缓存）
├── rag_client.py     # RAG HTTP 客户端（SSE 解析、错误映射）
├── tools.py          # 三个 tool 的注册、身份解析、出参裁剪
├── requirements.txt
├── .env.example
├── tests/
│   ├── conftest.py
│   └── test_acceptance.py
└── DESIGN.md         # 设计文档
```
