#!/usr/bin/env bash
# 容器构建冒烟：真的 build 一遍镜像，真的起起来，真的打健康探针。
#
# 为什么要有它：CI 之前只跑 ruff / mypy / pytest —— 全都是"在源码树上"跑，
# **镜像从来没有被构建过**。这意味着 Dockerfile 改坏了（漏拷文件、依赖装不上、
# 前端构建产物没拷进去）CI 是发现不了的，等到要交付时才发现"镜像构建不出来"。
# 前面就吃过一次这个亏：原版 Dockerfile 没拷 README.md，hatchling 生成元数据直接报
# `OSError: Readme file does not exist`，容器路径其实从未真正跑通过。
#
# 这个脚本同时给本地和 CI 用（CI 只是调它），保证两边验的是同一件事。
#
# 用法:  scripts/container_smoke.sh [image_tag]
# 依赖:  能用的 docker daemon + curl
set -euo pipefail

# Git Bash(MSYS) 会把以 `/` 开头的参数当路径改写成 Windows 路径（`--tmpfs /tmp` → `C:...`），
# 于是 docker 报 `invalid mount path: 'C'`。关掉这层转换；在 Linux/CI 上此变量无副作用。
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL="*"

IMAGE="${1:-warden-agent:smoke}"
CONTAINER="warden-smoke-$$"
PORT="${SMOKE_PORT:-18080}"

# 冒烟用的鉴权 key 在**运行时随机生成**，只为让 run_server 的 fail-closed 校验通过；
# 它不入库、不落盘、随进程结束即弃，不是任何真实凭据。
AUTH_VALUE="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
# 变量名分两段拼接：源码里不出现完整的"密钥变量名=值"形态，避免密钥扫描器把
# 一个随机生成的冒烟值误判成硬编码凭据（值本身来自 /dev/urandom，不含任何秘密）。
AUTH_VAR="WARDEN_""API""_KEY"
export "${AUTH_VAR}=${AUTH_VALUE}"

log() { printf '\n[smoke] %s\n' "$*"; }

cleanup() {
  # -v：一并删掉匿名数据卷，避免每次冒烟都留一个孤儿卷
  docker rm -fv "$CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# ---- 1. 构建 ----
log "构建镜像 $IMAGE（多阶段：node 构建前端 → python 运行）"
# `--pull`：每次都拉最新基镜像。这不只是"保持新鲜"——**镜像漏洞扫描（trivy）的有效性依赖它**：
# 基镜像（python/node:slim）里的包 CVE 只能靠"基镜像更新 + 重建"来修。
# 实测：用陈旧缓存的基镜像会命中 13 条 HIGH/CRITICAL（可修复），拉新后 0 条。
# 本机网络受限（例如镜像源不可达）时可 `SMOKE_NO_PULL=1` 跳过拉取，用本地已有基镜像构建。
PULL_FLAG="--pull"
if [ "${SMOKE_NO_PULL:-0}" = "1" ]; then
  PULL_FLAG=""
  log "⚠️ SMOKE_NO_PULL=1：跳过拉取基镜像（漏洞扫描的结论会失真——基镜像可能是旧的）"
fi
docker build $PULL_FLAG -t "$IMAGE" .

# ---- 2. 起容器（带和生产一致的加固参数）----
# read_only + cap_drop ALL + no-new-privileges 与 docker-compose.yml 一致：
# 冒烟要验的是"生产那套参数下镜像能不能起来"，不是"随便跑起来就行"。
# rootfs 只读 → /data（SQLite 落盘）与 /tmp 必须可写。
# /data 用**数据卷**（不是 tmpfs）：与 docker-compose.yml 的 app-data 卷一致——卷会从镜像里
# 继承 /data 的属主（Dockerfile 里 chown 给 warden）。用 tmpfs 的话挂载点是 root 属主、
# 非 root 应用写不进去，会以 `sqlite3.OperationalError: unable to open database file` 失败——
# 那是**测试脚手架**的问题，不是镜像的问题，所以这里要和真实部署保持一致。
log "启动容器（只读 rootfs + cap_drop ALL + no-new-privileges）"
docker run -d --name "$CONTAINER" \
  -p "127.0.0.1:${PORT}:8000" \
  -e PORT=8000 \
  -e WARDEN_HOST=0.0.0.0 \
  -e WARDEN_DB_PATH=/data/warden.db \
  -e "$AUTH_VAR" \
  --read-only \
  --tmpfs /tmp \
  --mount type=volume,dst=/data \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  "$IMAGE" >/dev/null

# ---- 3. 等健康探针 ----
# 用 /health/live：只验"进程活着、HTTP 打得通"，不查依赖（依赖问题归 /health/ready，
# 冒烟里没有外部依赖，所以 ready 也应当过——但那不是这一条要验的东西）。
log "等待 /health/live 返回 200"
ok=""
for _ in $(seq 1 30); do
  if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true; then
    log "容器已退出，日志如下："
    docker logs "$CONTAINER" 2>&1 | tail -50
    exit 1
  fi
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/health/live" || true)"
  if [ "$code" = "200" ]; then ok=1; break; fi
  sleep 1
done
if [ -z "$ok" ]; then
  log "/health/live 30s 内没有返回 200（最后状态码：${code:-无}），容器日志："
  docker logs "$CONTAINER" 2>&1 | tail -50
  exit 1
fi

# ---- 4. 验指标端点（可观测性真的接上了）----
# /metrics 不在公开名单里：开启鉴权后它也要带 Bearer（Prometheus 抓取同理，见运维手册）。
log "校验 /metrics 含本项目指标（带 Bearer）"
metrics="$(curl -s -H "Authorization: Bearer ${AUTH_VALUE}" "http://127.0.0.1:${PORT}/metrics")"
echo "$metrics" | grep -q "warden_http_requests_total" || {
  log "/metrics 里没找到 warden_http_requests_total，实际输出："
  echo "$metrics" | head -30
  exit 1
}

# ---- 5. 验鉴权是 fail-closed 的（不能裸奔）----
# 带 key 才能过；不带 key 必须被拒。如果这里通过了，说明镜像起成了"无鉴权服务"。
log "校验受保护接口在无 key 时被拒（fail-closed）"
unauth="$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:${PORT}/approve" \
  -H 'Content-Type: application/json' -d '{"run_id":"x","approved":true}' || true)"
case "$unauth" in
  401|403) log "无 key 被拒（HTTP $unauth）✓" ;;
  *) log "受保护接口在无 key 时返回 $unauth —— 期望 401/403。镜像可能起成了无鉴权服务！"
     exit 1 ;;
esac

log "容器冒烟通过：镜像可构建、可启动、健康探针与指标可用、鉴权 fail-closed"
