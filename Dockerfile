# Warden Agent Python —— 容器镜像
# 把 Agent HTTP/SSE 服务打包成能被 Docker 部署的镜像（本地/云都可跑）。
#
# 构建 + 运行：
#   docker build -t warden-agent .
#   docker run -p 8000:8000 -e DEEPSEEK_API_KEY=sk-xxx warden-agent
# 或直接用 docker-compose（连 PostgreSQL 的那套）。
#
# T10 起：多阶段构建。第 1 阶段用 node 构建 React+TS+Tailwind 前端，
# 第 2 阶段把构建产物放进 /app/web/dist，由 FastAPI 单端口托管（/ 是 SPA，API 同源）。

# ---------- 阶段 1：构建 React 前端 ----------
FROM node:20-slim AS web-build
WORKDIR /build
# 先拷锁文件装依赖（利用 Docker 层缓存）
COPY web/package.json web/package-lock.json* ./
RUN npm ci || npm install
# 再拷源码构建
COPY web/ ./
RUN npm run build

# ---------- 阶段 2：Python 后端 + 托管前端构建产物 ----------
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WARDEN_HOST=0.0.0.0 \
    WARDEN_DB_PATH=/data/warden.db

WORKDIR /app

# 先只装依赖（利用 Docker 层缓存，改代码不用重装依赖）
COPY pyproject.toml ./
RUN pip install --no-cache-dir ".[postgres]"

# 拷贝后端源码
COPY src ./src

# 拷贝前端构建产物到 /app/web/dist（server.py 用仓库相对路径自动找到并托管）
COPY --from=web-build /build/dist ./web/dist

# 非 root 运行（最小权限）：容器逃逸时能拿到的权限越小越好，成本几乎为零
RUN useradd --create-home --uid 10001 warden \
    && mkdir -p /data \
    && chown -R warden:warden /data
USER warden

EXPOSE 8000

# 存活探针：编排层据此判断"要不要重启"。不查依赖（依赖问题由 /health/ready 表达）。
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request,sys; \
p='http://127.0.0.1:'+os.environ.get('PORT','8000')+'/health/live'; \
sys.exit(0 if urllib.request.urlopen(p, timeout=2).status == 200 else 1)"

# 默认启动 HTTP/SSE 服务。注意：WARDEN_HOST=0.0.0.0 是对外监听，
# 因此**必须**提供 WARDEN_API_KEY，否则 run_server 会 fail-closed 拒绝启动。
CMD ["python", "-m", "warden_agent.web.run_server"]
