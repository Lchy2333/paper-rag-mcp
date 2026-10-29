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
        ├── stdio 模式：python login.py 拉起本机登录页，JWT 自动写入凭据文件
        │   （也可手工把 JWT 填进 PAPER_RAG_TOKEN 环境变量，优先级更高）
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

1. **paper-rag-agent** 身份校验接口（✅ 已提供，agent 仓 dev 分支）：

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
| `PAPER_RAG_TOKEN` | 空 | 手工覆盖入口（优先级高于凭据文件）；禁止填 admin token。推荐留空，用 `login.py` 登录 |
| `PAPER_RAG_CREDENTIALS_FILE` | `~/.paper-rag-mcp/credentials.json` | `login.py` 写入的凭据文件路径 |
| `SEARCH_TIMEOUT` | `90` | 检索读超时（秒） |
| `CONNECT_TIMEOUT` | `15` | 连接超时（秒） |
| `INTROSPECT_TIMEOUT` | `5` | 身份校验超时（秒） |
| `INTROSPECT_CACHE_TTL` | `60` | 身份缓存 TTL（秒），实际不超过 token 剩余有效期 |
| `MAX_TOP_K` | `20` | `top_k` 上限 |
| `MAX_CHUNK_CHARS` | `2000` | 单片段最大字符数，超出截断并标记 `truncated` |
| `MAX_RESPONSE_BYTES` | `65536` | 单次返回总字节上限，超出从尾部减条数并附 `note` |

启动期会做校验：`MCP_TRANSPORT` 非法、`RAG_BASE_URL` 为空、stdio 模式下环境变量与凭据文件都拿不到用户身份，都会直接拒绝启动。

## 登录（stdio 模式首次使用）

```bash
python login.py
```

流程：自动打开浏览器上的**本机临时登录页**（127.0.0.1 随机端口）→ 输入用户名密码 →
本地服务代理转发 Agent 的 `/api/auth/login` → JWT 写入本机凭据文件 → 退出。
前端仓库无需任何改动；token 不进 URL、不打印原文（只显示指纹），且只落在运行命令的这台机器上。

- 凭据文件默认 `~/.paper-rag-mcp/credentials.json`，可用 `PAPER_RAG_CREDENTIALS_FILE` 改路径。
- `PAPER_RAG_TOKEN` 环境变量优先级更高，可随时手工覆盖。
- token 过期后重跑一次 `python login.py` 即可。
- streamable-http 模式下，命令结束会额外打印一段可直接粘贴的客户端配置片段。

## 运行

```bash
python server.py
```

- **stdio**（本地单人）：日志写 stderr，stdout 是 JSON-RPC 通道。
- **streamable-http**（多人共用）：监听 `MCP_HOST:MCP_PORT`，身份取自每次请求的 `Authorization` 头，请部署在内网 / 网关之后。

> 日志必须走 stderr。任何 print 到 stdout 都会破坏 stdio 协议。

### 两种模式：谁跑 server.py（必读）

| | `stdio`（客户端 `"type": "local"`） | `streamable-http`（客户端 `"type": "remote"`） |
| --- | --- | --- |
| server.py 谁来跑 | **客户端自动拉起子进程**，无需手动运行 | **必须手动/常驻运行**（像 Agent、RAG 一样是独立服务） |
| 身份来源 | 凭据文件（`login.py` 写入）或 `PAPER_RAG_TOKEN` | 每次请求的 `Authorization: Bearer` |
| 适用 | 本地单人、开发调试 | 多客户端/多用户、远程部署 |

**一条规则**：8765 端口只允许一个 server.py 存在。手动跑了服务端 → 客户端必须用 remote；
没有手动跑 → 客户端用 local 且传输必须是 stdio。两者混搭必然报错：
`WinError 10048` 端口冲突（local 子进程撞上手动实例），或 `Request timed out`（remote 连向不存在的服务）。

### 客户端配置示例（stdio / OpenCode）

```json
{
  "mcp": {
    "paper-rag": {
      "type": "local",
      "command": [
        "d:/Project/PaperRAG/paper-rag-mcp/.venv/Scripts/python.exe",
        "d:/Project/PaperRAG/paper-rag-mcp/server.py"
      ],
      "environment": { "MCP_TRANSPORT": "stdio" },
      "enabled": true
    }
  }
}
```

说明：

- `.env` 与 [config.py](config.py) 同目录，按**代码所在目录**查找（与客户端进程 cwd 无关），
  `RAG_BASE_URL` / `RAG_API_KEY` / `AGENT_BASE_URL` 无需在 env 里重复填写。
- `environment` 里的 `MCP_TRANSPORT=stdio` 用于覆盖 `.env` 的 streamable-http
  （环境变量优先级高于 .env 文件），保证子进程以 stdio 模式启动、不抢端口。
- 用户身份来自 `login.py` 写入的凭据文件，token 过期后**重跑 `login.py` 即可，客户端无需重启**
  （凭据文件每次工具调用现读）。
- Claude Desktop / Cursor / Trae 的 `mcpServers` 写法类似：`command`/`args` 平铺 + `env`。

### 客户端配置示例（streamable-http / OpenCode）

先手动运行 `python server.py` 并保持，然后：

```json
{
  "mcp": {
    "paper-rag": {
      "type": "remote",
      "url": "http://127.0.0.1:8765/mcp",
      "headers": { "Authorization": "Bearer <login.py 打印的 JWT>" },
      "enabled": true
    }
  }
}
```

远端部署时把 `url` 换成服务器地址（注意路径必须是 `/mcp`）；token 过期需更新 headers。

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

## 常见问题排查（2026-10 首次联调实录）

| 症状 | 根因 | 处置 |
| --- | --- | --- |
| login.py 页面提示「无法连接登录服务（Agent）」 | `AGENT_BASE_URL` 端口写错（Agent 实际不在 8000） | 确认 Agent 实际监听端口后改 `.env`；新版 login.py 会把底层异常打到终端 |
| 工具返回 `invalid or missing API key` | `.env` 的 `RAG_API_KEY` 为空，MCP 不带 `X-API-Key`，而 RAG 启用了鉴权 | 填入与 RAG `server.api_key` 相同的值（当前单 key，与 Agent 共用；多 Key 见 DESIGN.md §8.2） |
| 客户端报 `WinError 10048` / process exited | 两种模式混搭：客户端 local 又拉起子进程，与手动实例抢 8765 | 按上文「两种模式」二选一，保证只有一个 server.py |
| 客户端报 `Request timed out` / 连接被拒 | remote 模式但没有任何 server.py 在监听 | 先手动运行 `python server.py` 并保持 |
| 工具返回「未登录或登录已过期」 | JWT 过期或用户被禁用 | 重跑 `python login.py`（stdio 无需重启客户端；http 模式更新 headers） |
| stdio 启动即退出：stdio 模式缺少用户身份 | 凭据文件不存在且 `PAPER_RAG_TOKEN` 为空 | 先跑 `python login.py`，或在 `.env` 填 token |
| OpenCode `/mcps` 列表为空 | 配置顶层键写成了 `mcpServers`（Claude Desktop 风格）；**OpenCode 的键是 `mcp`** | 改键名后重启客户端 |
| 中文日志乱码 | GBK 控制台显示 UTF-8 日志 | 无害；介意的话把终端代码页切到 65001 |

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
├── config.py         # pydantic-settings 配置 + 凭据文件读写
├── login.py          # 浏览器登录 helper（本机临时登录页 → JWT → 凭据文件）
├── auth.py           # Bearer → Agent introspection → user_id（含指纹缓存）
├── rag_client.py     # RAG HTTP 客户端（SSE 解析、错误映射）
├── tools.py          # 三个 tool 的注册、身份解析、出参裁剪
├── requirements.txt
├── .env.example
├── tests/
│   ├── conftest.py
│   ├── test_acceptance.py
│   └── test_login.py
└── DESIGN.md         # 设计文档
```
