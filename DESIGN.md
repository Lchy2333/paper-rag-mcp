# paper-rag-mcp 设计文档

> 状态：设计草案（未实现）
> 定位：把 RAG 的检索/文档能力以 **MCP Server** 形式对外暴露的独立进程，归属 RAG 侧（数据所有者）。
> 关联文档：[paper-rag-agent/architecture.md](../paper-rag-agent/architecture.md)（ADR-001 / ADR-002 / §10.6 身份贯通）

---

## 1. 结论先行

| 决策 | 内容 |
|---|---|
| 归属 | **RAG 侧**，独立进程，目录 `paper-rag-mcp/`，与 `paper-rag-backend` / `paper-rag-agent` / `paper-rag-fronted` 平级 |
| 后端 | 只调 RAG 的 REST（`/api/v1/*`），**不碰 PostgreSQL、不碰 LLM、不重写检索** |
| 身份来源 | 用户在**浏览器登录页**登录 → Agent `/api/auth/login` 签发 JWT → MCP 客户端持有 → 每次工具调用带 `Authorization: Bearer <JWT>` → MCP 调 Agent `/api/auth/introspect` 换成 `user_id` |
| 铁律 | `user_id` **绝不出现在 tool schema 里**，LLM 无法指定、无法伪造 |
| 安全边界 | 只保证「**身份来源可信**」，不保证「身份不可伪造」——持有 `RAG_API_KEY` 的进程仍可为任意 `user_id` 服务，见 §4.5 |
| v1 工具 | 只读三件：`search_papers`、`list_documents`、`get_document` |
| 传输 | 本地单人用 `stdio`；多人共用用 `streamable-http` |
| 前置改动 | Agent 新增 `POST /api/auth/introspect`；RAG 侧如需保留 Agent 直连则需支持多 API Key |

---

## 2. 为什么需要它

MCP 解决的是 **N 个客户端 × M 个服务** 的适配爆炸（N+M 收敛）。

- 只有我们自己的 Agent 一个消费者时，收益为 0 —— 项目的 [RAGtools.json](../paper-rag-agent/tools/RAGtools.json) + [HttpTool](../paper-rag-agent/tools/http_tool.py) 已经是配置驱动的工具总线，再加 MCP 是纯冗余。
- 一旦出现第二个消费者（Claude Desktop、Cursor、第三方 Agent），收益才成立：它们不必了解 RAG 的 REST 契约、SSE 格式、鉴权头，只认 MCP 工具。

因此本项目的 MCP **不放在 Agent 与 RAG 之间**（那样它只服务 Agent 一个客户端，且会额外持有一份 RAG 密钥），而是放在 **RAG 前面**，Agent 降级为它的客户端之一。

---

## 3. 架构拓扑

```
Claude Desktop   Cursor   第三方 Agent        paper-rag-agent
      │             │          │                    │
      │             │          │  (编排/流式/ HITL)  │
      └─────────────┴──────────┼────────────────────┘
                               │  MCP (stdio / streamable-http)
                               │  Authorization: Bearer <用户 JWT>
                               ▼
                    ┌──────────────────────┐
                    │    paper-rag-mcp     │   ← 本进程
                    │  · JWT → user_id     │
                    │  · 唯一持有 RAG Key  │
                    └──────────┬───────────┘
                               │  HTTP  X-API-Key + X-User-ID
                               ▼
                    ┌──────────────────────┐
                    │  paper-rag-backend   │  (Go，数据所有者)
                    │  三路检索 + 强制过滤  │
                    └──────────┬───────────┘
                               ▼
                        PostgreSQL + pgvector
```

**JWT 从哪来**：用户在浏览器登录页登录 Agent 获得（`POST /api/auth/login`）。
- `stdio`：用户把 token 手工填进 `PAPER_RAG_TOKEN` 环境变量（§6.3）。
- `streamable-http`：客户端在每次请求的 `Authorization` 头里携带。

### 3.1 两条通路并存（有意为之）

| 通路 | 用途 | 为什么不能合并 |
|---|---|---|
| Agent → RAG（`RagProxy` 直连） | Agent 编排内部用，需要 `/search` 的 SSE `progress` 事件做前端流式进度、需要 HITL 审批 | MCP 的 `tools/call` 不适合承载增量进度流；强行塞进去会让 Agent 的 UX 退化 |
| MCP Client → MCP → RAG | 外部客户端工具调用 | 对外统一入口，隔离 REST 细节 |

代价：**两处持有 RAG 密钥**。缓解手段见 §8.2（per-consumer API Key）。

---

## 4. 信任边界与身份传递

### 4.1 安全铁律

> **`user_id` 的决定权永远不在 LLM 手里。**

- ❌ 禁止：tool schema 里出现 `user_id` 参数（Prompt 注入 → 越权读别人的论文库）。
- ❌ 禁止：MCP 直接信任客户端传来的 `X-User-ID`（明文 header，任何人可伪造）。
- ✅ 正确：MCP 只信任 **验签通过的用户 JWT**，`user_id` 从 token 的 `sub` 得出，由 MCP 在调用 RAG 时注入 header。

### 4.2 身份链（本版采用）

```
① 登录（只在浏览器里发生一次）

   用户 ──▶ 前端登录页 ──POST /api/auth/login──▶ Agent
                          ◀── { access_token(JWT), user_id } ──

② 工具调用（每一次 tools/call 都重新过一遍）

MCP Client ──Bearer JWT──▶ MCP Server ──Bearer JWT──▶ Agent POST /api/auth/introspect
                                                            │ 验签 + 查 users 表
                                                            ▼
                                                     { user_id, username, disabled }
                                                            │
                                MCP 取 user_id ◀────────────┘
                                      │
                                      ▼
                          调 RAG：X-API-Key + X-User-ID
```

要点：

- ① 返回的 `user_id` **不能**直接拿来当 MCP 的调用凭证 —— 它只是登录结果的一部分，前端展示/建会话用。MCP 侧的身份一律由 ② 的 introspection 得出。
- MCP **不调** `login`（需要密码，密码不应进服务端配置或 LLM 上下文），只调 `introspect`（只吃 JWT）。
- MCP **不本地解码** JWT 的 `sub`（能读到 ≠ 可信，理由见 §4.3）。
- 到期/禁用由 ② 每跳兜住：JWT 过期 → introspection 401；用户被禁用 → `get_current_user` 查库后 403/401。
- 这套链路的边界见 §4.5：它保证 `user_id` **来源可信**，不保证 `user_id` **不可伪造**。

### 4.3 为什么走 introspection，而不是本地验签

Agent 的 JWT 是 HS256（[auth.py](../paper-rag-agent/app/auth.py#L25-L55)），理论上 MCP 拿到同一个 `JWT_SECRET` 就能本地 `decode`。不采用，原因有三：

1. **密钥扩散**：HS256 是共享密钥，谁持有谁就能**签发**任意用户的 token。把密钥复制到第三个进程，等于多开一个后门。
2. **状态校验缺失**：本地验签只看签名和过期时间，看不到 `users.disabled`、用户已注销等状态。[get_current_user](../paper-rag-agent/app/auth.py#L62-L94) 每次都会查库，MCP 必须对齐这一语义。
3. **演进**：将来 Agent 换成 RS256/JWKS 或接入统一 Auth，MCP 无需改动。

代价是每次工具调用多一跳（同机房约 1–3ms）。可对 introspection 结果做**进程内缓存**，key = `sha256(token)`，TTL = `min(60s, exp - now)`，用户登出/禁用最多滞后 60 秒。

### 4.4 RAG 侧的既有约束（必须遵守）

- 服务鉴权读 `X-API-Key`，常量时间比较；不认 `Authorization: Bearer`（[middleware.go](../paper-rag-backend/internal/httpapi/middleware.go#L16-L32)）。
- `X-User-ID` 缺失或为空 → 直接 401，不做兜底（[middleware.go](../paper-rag-backend/internal/httpapi/middleware.go#L34-L49)）。
- `user_id` 命中 `admin_users` 白名单时检索**不过滤**（`scopeUserID` 返回空 → 全库）。因此 MCP 不得把 admin 身份设为默认值；admin 用户登录后自然获得全库能力，这是既有语义。
- `/ask` 仅 admin 可调且是全局检索，**MCP v1 不暴露**。

### 4.5 残余风险与边界（必读）

本设计的身份链只解决了一件事：**`user_id` 从哪来**（由 Agent 验签得出，而非客户端自报）。
它**没有**解决另一件事：**`user_id` 有多可信**。后者由 RAG 侧既有鉴权模型决定，不在 MCP 能修的范围内。

#### 两层身份模型

| 层 | 载体 | 回答的问题 | 谁校验 | 可信度 |
|---|---|---|---|---|
| 第 1 层：能不能进门 | `X-API-Key` | 请求是不是可信服务发的 | RAG `APIKeyAuth`（常量时间比较） | 强（有密钥才行） |
| 第 2 层：以谁的名义 | `X-User-ID` | 这次操作算在谁头上 | RAG `UserContext`（**只查非空**） | **弱（只是声明）** |

[middleware.go](../paper-rag-backend/internal/httpapi/middleware.go) 的 `UserContext` 不验签、不查库，因此：

> **任何持有 `RAG_API_KEY` 的进程，都可以为任意 `user_id` 发起请求。**

RAG 的用户隔离建立在「只有一个可信代理」的假设上。MCP 的引入把 1 个代理变成 2 个（Agent + MCP），该假设被打破 —— 这是本版方案明确接受、并记录在案的残余风险。

#### 防得住 / 防不住

| 威胁 | 是否防住 | 说明 |
|---|---|---|
| LLM 通过 tool 参数指定别人的 `user_id` | ✅ | schema 里没有该字段 |
| 客户端伪造 `X-User-ID` 传给 MCP | ✅ | MCP 不读入参 header，只取 introspection 结果 |
| 客户端自己解码 JWT 拿 `sub` | ✅ | 拿到的 `sub` 只能自用；MCP 仍会验签一次 |
| 绕过校验，让 MCP 直接调 `login` 换 user_id | ✅ | 设计禁止；且需要密码 |
| token 过期 / 用户被禁用 | ✅ | 每跳 introspection 查库（缓存最多滞后 60s，见 §4.3） |
| **MCP 进程被攻陷、或运维人员手工构造请求** | ❌ | 拿到 `RAG_API_KEY` 后可直接写任意 `X-User-ID` |
| **MCP 代码 bug 把 `user_id` 传错** | ❌ | RAG 无法分辨 |
| **审计**：区分 Agent 与 MCP 的调用 | ⚠️ | 共用一把 key 时不可归因；多 Key（§8.2）后可区分调用方，但仍无法证明 `user_id` 真实 |

#### 为什么 v1 接受

- 修复它必须改 RAG 的鉴权模型（从「API Key + 声明式 user_id」改为「audience 绑定的签名 token」），属跨项目重构，见 §4.6。
- 当前部署形态下 MCP 与 Agent 同属内网可信域：攻击面主要是「MCP 自身被攻陷」，与「Agent 被攻陷」等价，风险增量有限。
- **前提条件**：MCP 的主机/容器必须与 RAG 同级可信；`RAG_API_KEY` 不得下发到任何终端用户环境。

#### 明确不做

- 不把 `X-User-ID` 当安全凭证对外宣传。
- 不让前端接触 `RAG_API_KEY`。
- 不在 MCP 内做「用户 A 只能查 A」的二次过滤 —— 那是 RAG 的职责，重复实现只会掩盖问题。

### 4.6 目标态（**本期不实现**，仅记录）

要真正堵住 §4.5 的缺口，需要三件事同时成立：

1. Agent 从 HS256 升级为 RS256/ES256 并暴露 JWKS，签发**带 `aud` 的短票**（`aud=agent` / `aud=mcp`）。
2. 中间层（Agent、MCP）用 token exchange（RFC 8693）换一张 `aud=rag`、TTL 60s、带 `act` claim 的委托票，而不是长期持有 `RAG_API_KEY`。
3. RAG 侧用「本地验签 + 校验 `aud == rag`」替换 `APIKeyAuth` + `UserContext`，`user_id := claims.sub`，`actor := claims.act.sub`。

代价：失去即时断权（只能靠 60s TTL）、RAG 需引入 JWKS 缓存与时钟偏差容忍、中间层每跳多一次换票。**P2 之前不做。**


---

## 5. 对外工具契约（v1）

### 5.1 工具清单

| MCP tool | 对应 RAG 接口 | 说明 |
|---|---|---|
| `search_papers` | `POST /api/v1/search`（SSE） | 取 `event: result` 作为最终结果 |
| `list_documents` | `GET /api/v1/documents` | 当前用户可见文档列表 |
| `get_document` | `GET /api/v1/documents/{id}` | 文档元信息 + 处理状态 |

v1 不暴露：`upload`、`rechunk`、`delete`（破坏性操作，需审批流，而 MCP 没有 HITL 通道）、`ask`（仅 admin）、`health`（运维面）。

### 5.2 `search_papers`

```json
{
  "name": "search_papers",
  "description": "在当前用户的论文库中检索与问题相关的原文片段，返回片段内容、文件名与页码。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "query":       { "type": "string", "description": "检索问题或关键词" },
      "top_k":       { "type": "integer", "minimum": 1, "maximum": 20, "default": 5 },
      "document_ids":{ "type": "array", "items": { "type": "string" },
                       "description": "可选，限定在这些文档内检索" }
    },
    "required": ["query"]
  }
}
```

对外**不暴露** `rerank` / `hybrid` 开关：这是运维调参项，暴露给 LLM 只会增加误用。由 MCP 侧配置固定（默认跟随 RAG 服务端配置）。

返回结构直接复用 ADR-001 的定义（`chunk_id / document_id / filename / page / score / content`），包成 MCP 的 `structuredContent`：

```json
{
  "content": [
    { "type": "text", "text": "[1] attention.pdf p.3 (score 0.82)\n<片段正文> ..." }
  ],
  "structuredContent": {
    "query": "attention mechanism",
    "results": [
      { "chunk_id": "…", "document_id": "…", "filename": "attention.pdf",
        "page": 3, "score": 0.82, "content": "…" }
    ],
    "total_chunks": 5,
    "latency_ms": 640
  }
}
```

`text` 供不支持 structuredContent 的客户端直接喂给模型；`structuredContent` 供可编排的客户端做引文渲染。

### 5.3 输出限额（防止打爆上下文）

| 项 | 上限 | 说明 |
|---|---|---|
| `top_k` | 20 | schema 已约束，服务端再夹一次 |
| 单片段 `content` | 2000 字符 | 超出截断并标记 `truncated: true` |
| 总返回字节 | 64 KB | 超出则减少条数并附 `note` |
| 请求超时 | 90s | 与 [RAGtools.json](../paper-rag-agent/tools/RAGtools.json) 的 `search_rag` 对齐；SSE 首字节超时 15s |

### 5.4 错误语义

| 场景 | MCP 返回 |
|---|---|
| 未带/过期 token | `isError: true`，`text: "未登录或登录已过期"`（不泄露内部细节） |
| RAG 4xx | 透传业务错误文案（如「文档不存在」），`isError: true` |
| RAG 5xx / 超时 | `"检索服务暂不可用，请稍后重试"`，`isError: true` |
| SSE 只收到 `error` 事件 | 同上，附 `stage` 便于排查 |

---

## 6. 内部实现

### 6.1 目录结构

```
paper-rag-mcp/
├── DESIGN.md              # 本文档
├── server.py              # FastMCP 实例 + 传输选择（stdio / streamable-http）
├── config.py              # 环境变量读取与校验（pydantic-settings，对齐 agent 风格）
├── auth.py                # Bearer 解析 + introspection + 进程内缓存
├── rag_client.py          # RAG REST 封装（含 SSE 解析、超时、错误映射）
├── tools.py               # 三个 tool 的注册与出参裁剪
├── requirements.txt       # mcp, requests, pydantic-settings
└── .env.example
```

**禁止**从 `../paper-rag-agent` import 任何模块（跨目录 sys.path 会把两个项目绑死）。

### 6.2 关键代码骨架

```python
# server.py
from mcp.server.fastmcp import FastMCP
from config import settings
from tools import register_all

mcp = FastMCP("paper-rag")
register_all(mcp)

if __name__ == "__main__":
    # 本地单人：stdio；多人：streamable-http（需配 host/port）
    mcp.run(transport=settings.mcp_transport)
```

```python
# auth.py
import hashlib, time, requests
from config import settings

_cache: dict[str, tuple[float, dict]] = {}

def resolve_user(token: str) -> dict:
    """用 Agent introspection 换可信 user_id；失败抛 Unauthorized。"""
    key = hashlib.sha256(token.encode()).hexdigest()
    hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]

    r = requests.post(
        f"{settings.agent_base_url}/api/auth/introspect",
        headers={"Authorization": f"Bearer {token}"},
        timeout=5,
    )
    if r.status_code != 200:
        raise Unauthorized()
    data = r.json()                       # {user_id, username, disabled}
    ttl = min(60, max(0, int(data.get("exp", 0) - time.time())))
    _cache[key] = (time.time() + ttl, data)
    return data
```

```python
# rag_client.py —— SSE 解析（必须按 UTF-8 逐行解码）
def search(query: str, top_k: int, document_ids: list[str] | None, user_id: str) -> dict:
    body = {"query": query, "top_k": top_k}
    if document_ids:
        body["document_ids"] = document_ids
    headers = {"X-API-Key": settings.rag_api_key, "X-User-ID": user_id}

    with requests.post(f"{settings.rag_base_url}/api/v1/search",
                       json=body, headers=headers, stream=True,
                       timeout=(15, settings.search_timeout)) as resp:
        resp.raise_for_status()
        evt, result = "", None
        for raw in resp.iter_lines():          # 原始字节，禁止 resp.text
            if not raw:
                continue
            line = raw.decode("utf-8")         # RAG 侧固定 UTF-8，中文不进 latin-1
            if line.startswith("event:"):
                evt = line[7:].strip()
            elif line.startswith("data:"):
                payload = json.loads(line[6:].strip())
                if evt == "result":
                    result = payload
                elif evt == "error":
                    raise RagError(payload.get("error", "search failed"))
        if result is None:
            raise RagError("检索流未返回结果")
        return result
```

> SSE 的坑与 [http_tool.py](../paper-rag-agent/tools/http_tool.py#L80-L97) 一致：`text/event-stream` 不带 charset，`requests` 会按 latin-1 解码导致中文乱码，必须走 `iter_lines()` + 手动 UTF-8。

### 6.3 传输与会话绑定

| 传输 | 身份绑定方式 | 适用 |
|---|---|---|
| `stdio` | 进程级：用户身份按「`PAPER_RAG_TOKEN` 环境变量 > 凭据文件」取（凭据文件由 `login.py` 写入，见 §6.4）；token 过期重跑 `login.py` | 本地单人、开发调试 |
| `streamable-http` | 连接级：每次 HTTP 请求的 `Authorization: Bearer`，MCP 逐请求解析 | 多人共用、远程 |

MCP 协议**没有**「每次 `tools/call` 追加自定义 header」的能力，因此身份只能在建连/进程启动时绑定，不能按调用动态塞。

### 6.4 login.py：浏览器登录 helper（v1 已实现）

把 §4.2 ①「登录只在浏览器里发生一次」落成命令：`python login.py`。

```
本机 127.0.0.1 随机端口起临时服务（内置登录页 + /submit 代理）
  → 打开浏览器，用户在**本机登录页**输入用户名密码
  → 本地服务代理转发 Agent POST /api/auth/login（服务端到服务端，不受浏览器 CORS 限制）
  → JWT 写入凭据文件（默认 ~/.paper-rag-mcp/credentials.json）→ 服务退出
```

设计取舍：登录页**内置在 login.py**，不改造前端仓库——代理转发绕开了 CORS，
前端零改动即可用；token 也不进浏览器地址栏（对比「前端回跳」方案）。

安全要点：

- 只绑 `127.0.0.1` + 随机端口；登录成功即退出，超时 5 分钟自动关闭。
- 表单携带一次性 `state`，`/submit` 校验，防本机端口被其他网页 CSRF。
- 密码/token 不落日志（token 只输出 sha256 指纹），不进 URL。
- token 全程只落在运行命令的这台机器上；凭据文件 0600 权限（Windows 尽力而为）。
- 禁止把 admin 的 token 写进凭据文件（§9）。

远端部署预留：登录页指向 `AGENT_BASE_URL`（改 `.env` 即指向远端 Agent）；
http 模式下命令结束打印可直接粘贴的客户端配置片段（提示替换为服务器地址）。

局限（对照 §4.6）：token 过期需重跑 `login.py`；完整方案是 MCP 标准 OAuth
（客户端自动完成授权码 + PKCE，Agent 需实现授权服务器端点），P2 之前不做。

---

## 7. 配置项

| 变量 | 必填 | 示例 | 说明 |
|---|---|---|---|
| `RAG_BASE_URL` | ✅ | `http://paper-rag-backend:8080` | RAG 服务地址 |
| `RAG_API_KEY` | ✅ | `sk-mcp-…` | **MCP 专属**的服务级密钥（见 §8.2） |
| `AGENT_BASE_URL` | ✅ | `http://paper-rag-agent:8000` | introspection 目标（login.py 的登录页也指向它） |
| `MCP_TRANSPORT` | ✅ | `stdio` \| `streamable-http` | 传输方式 |
| `MCP_HOST` / `MCP_PORT` | http 时必填 | `0.0.0.0` / `8765` | 监听地址 |
| `PAPER_RAG_TOKEN` | ❌（手工覆盖入口） | `<用户 JWT>` | 优先级高于凭据文件；禁止填 admin token |
| `PAPER_RAG_CREDENTIALS_FILE` | ❌ | `~/.paper-rag-mcp/credentials.json` | `login.py` 写入的凭据文件路径 |
| `SEARCH_TIMEOUT` | ❌ | `90` | 秒 |
| `INTROSPECT_CACHE_TTL` | ❌ | `60` | 秒，且不得超过 token 剩余有效期 |

`.env` 不入库；stdio 场景下 token 属敏感信息，文档中需明确提示用户不要提交到版本库。

---

## 8. 需要本目录之外的改动

### 8.1 Agent：新增 introspection 接口（✅ 已实现，agent 仓 dev 分支 8e62114）

```python
# paper-rag-agent/app/api/auth.py（已合入）
@router.post("/introspect")
def introspect(request: Request, user: Dict[str, Any] = CurrentUser) -> Dict[str, Any]:
    """服务间身份校验：Bearer JWT → 可信用户信息（不含 token / 密钥）。"""
    exp = None
    authorization = request.headers.get("authorization") or ""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        try:
            exp = decode_token(token.strip()).get("exp")
        except jwt.InvalidTokenError:
            exp = None
    return {"user_id": str(user["id"]), "username": user["username"],
            "disabled": bool(user["disabled"]), "exp": exp}
```

要点：

- 复用 [get_current_user](../paper-rag-agent/app/auth.py#L62-L94)，天然继承「验签 + 查库 + 禁用校验」。
- 该接口只返回身份，不返回任何业务数据，泄露风险低；但需要在网关层限制只允许内网/服务网段访问。
- 返回体不含 token、不含密钥。

### 8.2 RAG：多 API Key（P1，若要保留 Agent 直连）

现状是 [config.go](../paper-rag-backend/internal/config/config.go#L37-L42) 的单一静态 `server.api_key`，无法区分调用方，也无从独立限流/吊销。

建议演进为：

```yaml
server:
  api_keys:
    - name: agent
      key: "${RAG_API_KEY_AGENT}"
      rate_limit: 60      # req/s，可选
    - name: mcp
      key: "${RAG_API_KEY_MCP}"
      rate_limit: 30
```

兼容策略：未配置 `api_keys` 时回退到单 `api_key` 行为（零回归）。中间件在 context 里写入 `client_name`，日志带上，便于审计「是谁在读谁的数据」。

> 若 P1 暂不做，则必须让 Agent 与 MCP **共用一把 key**，并在文档里标注这是已知的审计盲区。

### 8.3 RAG：非流式 search（可选，P2）

给 MCP 提供一个 `POST /api/v1/search/plain`，省掉 SSE 解析。**非必需**，因为 SSE 解析只有约 20 行；优先不改 Go 侧。

---

## 9. 安全评审清单

- [ ] tool schema 中不存在 `user_id` / `api_key` / `Authorization` 字段
- [ ] MCP 不读取客户端传入的 `X-User-ID`
- [ ] `X-API-Key` 只存在于 MCP 进程环境变量，不出现在任何响应体/日志
- [ ] introspection 失败一律 401，不回显 token 片段
- [ ] 日志脱敏：token 只记 `sha256` 前 8 位；`content` 不落日志
- [ ] `top_k`、`content` 长度、总响应体积均有上限
- [ ] 默认身份不是 admin；文档明确警告不要把 admin token 配进 `PAPER_RAG_TOKEN`
- [ ] stdio 模式下 `PAPER_RAG_TOKEN` 不写入仓库、不打印
- [ ] streamable-http 部署在内网，或前面挂网关做来源限制

---

## 10. 验收标准

| # | 场景 | 期望 |
|---|---|---|
| 1 | 无 token 调 `search_papers` | `isError: true`，提示未登录 |
| 2 | 用户 A 的 token 检索用户 B 的文档内容 | 查不到（RAG 侧 `X-User-ID` 过滤生效） |
| 3 | 工具参数里塞 `user_id` | schema 校验失败，请求不发出 |
| 4 | 正常检索 | 返回 `structuredContent.results`，字段与 ADR-001 一致，中文无乱码 |
| 5 | RAG 停机 | 返回「检索服务暂不可用」，MCP 进程不崩 |
| 6 | token 过期 | 401，且缓存不返回旧身份 |
| 7 | 用户被禁用 | introspection 返回 401，MCP 立即拒绝 |
| 8 | 大文档检索 | `content` 截断生效，总体积 < 64KB |

建议用假 RAG（本地 stub 返回固定 SSE）跑 1–8，不依赖真实模型调用。

---

## 11. 分期

| 阶段 | 内容 | 触发条件 |
|---|---|---|
| **P0** | 只读三件 + introspection + stdio | 现在就能做，本地可验证 |
| **P1** | `streamable-http` + RAG 多 Key + 审计日志 | 出现第二个客户端 / 需要多人共用 |
| **P2** | OAuth 2.1 resource server（MCP 规范链路）、资源类 `resources/list`（论文目录树） | 对外产品化 |
| **不做** | 写操作（upload/rechunk/delete）、`ask`、跨用户检索、在 MCP 内重写检索 | —— |

---

## 12. 未决问题

1. **stdio 的 token 过期体验**：当前方案要用户手动换 token 重启。是否加一个 `login` tool（用户名密码经工具参数传递）？**倾向不做** —— 密码会进入 LLM 上下文，风险高于便利。
2. **introspection 缓存的失效时机**：60s TTL 对「用户被禁用后立即断权」有滞后。若安全要求高，把 TTL 降到 5s 或直接关闭缓存。
3. **是否需要 `resources`**：把「文档列表」做成 MCP resource 而非 tool，客户端可自动挂载。v1 先不做，等客户端支持度明确。
4. **多租户下的限流**：MCP 自己限流，还是靠 RAG 按 key 限流？倾向后者（唯一事实源），MCP 只做超时与重试上限。
