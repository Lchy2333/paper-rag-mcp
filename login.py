"""浏览器登录 helper：拉起本机临时登录页，换取 JWT 并写入凭据文件。

流程（DESIGN.md §6.4）：
1. 本机 127.0.0.1 随机端口起临时服务（内置登录页 + /submit 代理）；
2. 打开浏览器 → 用户输入用户名密码 → 本地服务**代理转发** Agent /api/auth/login
   （服务端到服务端转发，不受浏览器 CORS 限制，前端仓库零改动）；
3. 拿到 JWT 写入凭据文件（默认 ~/.paper-rag-mcp/credentials.json）；
4. server.py 读 token 的优先级：Authorization 头（http 模式）> PAPER_RAG_TOKEN > 凭据文件。

安全要点：
- 回调只绑 127.0.0.1、随机端口，登录成功即退出，超时 5 分钟自动关闭；
- 表单携带一次性 state，/submit 校验，防止本机端口被其他网页 CSRF；
- 密码/token 不打印日志（token 只输出指纹），不进 URL/浏览器地址栏；
- token 全程只落在运行本命令的这台机器上。

远端部署预留：登录页地址来自 settings.agent_base_url（改 .env 即指向远端 Agent）；
streamable-http 模式下命令结束会打印可直接粘贴的客户端配置片段。
"""
from __future__ import annotations

import json
import secrets
import threading
import webbrowser
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs

import requests

import auth
from config import credentials_path, save_credentials, settings

LOGIN_TIMEOUT_SECONDS = 300
LOGIN_POST_TIMEOUT = 10.0

# 占位符 __STATE__ / __ERROR__ 由 serve_login_once 填充（不用 str.format，避免与 CSS 冲突）
_LOGIN_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PaperRAG 登录</title>
<style>
  body { font-family: system-ui, "PingFang SC", "Segoe UI", sans-serif; background: #f5f6f7;
         display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }
  .card { background: #fff; border: 1px solid #e3e5e8; border-radius: 12px; padding: 28px 32px; width: 320px; }
  h1 { font-size: 18px; margin: 0 0 6px; }
  p.tip { color: #667085; font-size: 12px; margin: 0 0 18px; line-height: 1.6; }
  label { display: block; font-size: 13px; color: #344054; margin: 12px 0 4px; }
  input { width: 100%; box-sizing: border-box; padding: 8px 10px; border: 1px solid #d0d5dd;
          border-radius: 8px; font-size: 14px; }
  button { margin-top: 18px; width: 100%; padding: 9px 0; border: 0; border-radius: 8px;
           background: #16a34a; color: #fff; font-size: 14px; cursor: pointer; }
  button:hover { background: #15803d; }
  .error { background: #fef3f2; color: #b42318; border: 1px solid #fecdca; border-radius: 8px;
           padding: 8px 10px; font-size: 13px; margin-bottom: 12px; }
</style>
</head>
<body>
<div class="card">
  <h1>PaperRAG 登录</h1>
  <p class="tip">这是本机临时页面（127.0.0.1），仅用于给本机的 PaperRAG MCP 登录，凭证只保存在本机。</p>
  __ERROR__
  <form method="post" action="/submit">
    <input type="hidden" name="state" value="__STATE__">
    <label for="username">用户名</label>
    <input id="username" name="username" autocomplete="username" required>
    <label for="password">密码</label>
    <input id="password" name="password" type="password" autocomplete="current-password" required>
    <button type="submit">登录</button>
  </form>
</div>
</body>
</html>
"""

_DONE_PAGE = """<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>PaperRAG 登录成功</title></head>
<body style="font-family: system-ui, sans-serif; text-align: center; padding-top: 18vh;">
  <h2>登录成功</h2>
  <p>凭证已交还本机终端，可关闭此页面。</p>
</body>
</html>
"""


def serve_login_once(
    timeout: float = LOGIN_TIMEOUT_SECONDS,
    announce: Callable[[str], None] | None = None,
) -> str:
    """起本机临时登录服务并阻塞等待登录完成，返回 JWT。

    announce(url)：端口绑定后被调用（本地登录页地址），用于打印与打开浏览器。
    超时未完成登录抛 SystemExit；成功返回 token（**不写文件**，由调用方保存）。
    """
    state = secrets.token_urlsafe(24)
    captured: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        # 静音访问日志：请求体里有密码
        def log_message(self, *args) -> None:
            pass

        def _html(self, status: int, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path in ("/", "/login"):
                self._html(200, _LOGIN_PAGE.replace("__STATE__", state).replace("__ERROR__", ""))
            else:
                self._html(404, "<h1>404</h1>")

        def do_POST(self) -> None:
            if self.path != "/submit":
                return self._html(404, "<h1>404</h1>")
            length = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(length).decode("utf-8"))
            if form.get("state", [""])[0] != state:
                return self._html(403, "<h1>403 会话校验失败</h1>")
            username = form.get("username", [""])[0].strip()
            password = form.get("password", [""])[0]

            def fail(message: str) -> None:
                error = f'<div class="error">{escape(message)}</div>'
                self._html(200, _LOGIN_PAGE.replace("__STATE__", state).replace("__ERROR__", error))

            try:
                resp = requests.post(
                    f"{settings.agent_base_url.rstrip('/')}/api/auth/login",
                    json={"username": username, "password": password},
                    timeout=LOGIN_POST_TIMEOUT,
                )
            except requests.RequestException as exc:
                # 底层异常打到终端（页面保持友好文案），方便自诊断端口/代理问题
                print(f"[login] 连接 Agent 失败：{type(exc).__name__}: {exc}")
                return fail("无法连接登录服务（Agent），请确认其已启动后重试")
            if resp.status_code != 200:
                detail = f"登录失败（HTTP {resp.status_code}）"
                try:
                    body = resp.json()
                    if isinstance(body, dict) and isinstance(body.get("detail"), str) and body["detail"].strip():
                        detail = body["detail"].strip()
                except ValueError:
                    pass
                return fail(detail)

            token = str(resp.json().get("access_token") or "").strip()
            if not token:
                return fail("登录响应异常，请稍后重试")
            captured["token"] = token
            self._html(200, _DONE_PAGE)
            threading.Thread(target=server.shutdown, daemon=True).start()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    watchdog = threading.Timer(timeout, server.shutdown)
    watchdog.daemon = True
    watchdog.start()
    try:
        if announce:
            announce(f"http://127.0.0.1:{server.server_address[1]}/")
        server.serve_forever()
    finally:
        watchdog.cancel()
        server.server_close()

    token = captured.get("token")
    if not token:
        raise SystemExit(f"超时（{timeout:g} 秒）未完成登录，已退出。可重新运行本命令。")
    return token


def main() -> None:
    def announce(url: str) -> None:
        print(f"本机登录页：{url}")
        print("若浏览器未自动打开，请手动访问上面的地址。")
        webbrowser.open(url)

    print(f"登录服务（Agent）：{settings.agent_base_url}")
    token = serve_login_once(announce=announce)
    path = save_credentials(token)
    print(f"登录成功，凭证已保存：{path}（指纹 {auth.token_fingerprint(token)}，不打印原文）")

    if settings.mcp_transport == "streamable-http":
        print("\nstreamable-http 模式：把下面片段粘贴进客户端的 MCP 配置")
        print("（远端部署时把 url 换成 MCP Server 的实际地址）\n")
        print(
            json.dumps(
                {
                    "mcpServers": {
                        "paper-rag": {
                            "url": f"http://{settings.mcp_host}:{settings.mcp_port}/mcp",
                            "headers": {"Authorization": f"Bearer {token}"},
                        }
                    }
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        print()
    else:
        print("\nstdio 模式：无需其他配置，直接启动 server.py 即可（会自动读取凭据文件）。")


if __name__ == "__main__":
    main()
