# paper-rag-mcp（MCP Server：stdio / streamable-http 双传输）
# 部署形态：streamable-http 常驻服务；配置全部来自环境变量（compose env_file）
# 注意：容器内不得携带 .env / PAPER_RAG_TOKEN / 凭据文件——
#       身份只来自每次请求的 Authorization 头，否则会触发本地凭据兜底、放过未认证请求
FROM python:3.12-slim AS deps
WORKDIR /app
COPY requirements.txt .
# 国内 PyPI 镜像（pypi.org 在国内慢/不稳定，同 agent 仓）
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt

FROM python:3.12-slim AS runtime
WORKDIR /app
COPY --from=deps /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin
COPY . .
ENV PYTHONUNBUFFERED=1
EXPOSE 8765
# 传输由 MCP_TRANSPORT 决定（部署时 = streamable-http）
# MCP_HOST 必须 0.0.0.0：容器内绑 127.0.0.1 时端口映射后外部不可达
CMD ["python", "server.py"]
