#!/usr/bin/env bash
# 灰度发布（金丝雀）+ 自动回滚 —— 健康门控。
#
# 为什么要有它：本项目此前**没有灰度与自动回滚**，升级就是"停旧、起新，坏了再换回来"，
# 全凭人盯。这个脚本把"到底能不能放量"变成一条**自动化判据**：
#   起一个金丝雀实例 → 等它 /health/ready 通过 → 通过才允许切流量；不通过就地拆掉、保留旧版。
#
# 边界说清楚：**没有负载均衡/网关时，这个脚本无法替你切流量**——它做的是"门控"：
#   退出码 0 = 金丝雀健康，可以放量；非 0 = 金丝雀不健康，**别切**（旧版仍在服务，等于自动不升级）。
#   真正的切流（LB/Ingress/DNS 权重）是部署侧的事：拿到 0 之后，先切 5%→25%→100%，
#   每个档位盯 errors/延迟（见 deploy/observability 的告警规则库），异常就走 `--rollback` 语义。
#
# 用法:
#   scripts/canary_rollout.sh <新版镜像> [--promote]
#     --promote  金丝雀健康后，自动停掉旧容器（真正做到"替换"）。缺省只做门控、不动旧版。
#
# 环境变量:
#   CANARY_PORT/CANARY_CONTAINER/LIVE_CONTAINER/HEALTH_TIMEOUT_S/AUTH_VALUE 等
set -euo pipefail

export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL="*"

IMAGE="${1:-}"; shift || true
PROMOTE="no"
for arg in "$@"; do [ "$arg" = "--promote" ] && PROMOTE="yes"; done

if [ -z "$IMAGE" ]; then
  echo "用法: scripts/canary_rollout.sh <新版镜像> [--promote]" >&2
  exit 2
fi

CANARY_PORT="${CANARY_PORT:-18081}"
CANARY_CONTAINER="${CANARY_CONTAINER:-warden-canary}"
LIVE_CONTAINER="${LIVE_CONTAINER:-warden-live}"
HEALTH_TIMEOUT_S="${HEALTH_TIMEOUT_S:-60}"

# 金丝雀需要一个鉴权 key 才能通过 run_server 的 fail-closed 校验（随机生成，不是真实凭据）。
AUTH_VAR="WARDEN_""API""_KEY"
AUTH_VALUE="${WARDEN_SMOKE_API_KEY:-$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')}"
export "${AUTH_VAR}=${AUTH_VALUE}"

log() { printf '\n[canary] %s\n' "$*"; }

cleanup_canary() { docker rm -fv "$CANARY_CONTAINER" >/dev/null 2>&1 || true; }

log "启动金丝雀：$IMAGE（端口 $CANARY_PORT）"
cleanup_canary
docker run -d --name "$CANARY_CONTAINER" \
  -p "127.0.0.1:${CANARY_PORT}:8000" \
  -e PORT=8000 -e WARDEN_HOST=0.0.0.0 -e WARDEN_DB_PATH=/data/warden.db \
  -e "$AUTH_VAR" \
  --read-only --tmpfs /tmp --mount type=volume,dst=/data \
  --cap-drop ALL --security-opt no-new-privileges:true \
  "$IMAGE" >/dev/null

# 门控 1：就绪探针（/health/ready 会 ping 底层存储，比 /health/live 更强）
log "等待 /health/ready 通过（最多 ${HEALTH_TIMEOUT_S}s）"
healthy=""
deadline=$(( $(date +%s) + HEALTH_TIMEOUT_S ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  if ! docker inspect -f '{{.State.Running}}' "$CANARY_CONTAINER" 2>/dev/null | grep -q true; then
    log "金丝雀进程已退出 —— 不放量"; docker logs "$CANARY_CONTAINER" 2>&1 | tail -40
    cleanup_canary; exit 1
  fi
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${CANARY_PORT}/health/ready" || true)"
  [ "$code" = "200" ] && { healthy=1; break; }
  sleep 2
done
if [ -z "$healthy" ]; then
  log "就绪探针未通过（最后状态码 ${code:-无}）—— 不放量，旧版继续服务"; 
  docker logs "$CANARY_CONTAINER" 2>&1 | tail -40
  cleanup_canary; exit 1
fi

# 门控 2：鉴权仍是 fail-closed 的（别把一个裸奔的版本放出去）
unauth="$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:${CANARY_PORT}/approve" \
  -H 'Content-Type: application/json' -d '{"run_id":"x","approved":true}' || true)"
case "$unauth" in
  401|403) : ;;
  *) log "金丝雀的受保护接口在无 key 时返回 $unauth（期望 401/403）—— 不放量"; cleanup_canary; exit 1 ;;
esac

log "金丝雀健康、鉴权 fail-closed —— 可以放量"

if [ "$PROMOTE" = "yes" ]; then
  if docker inspect "$LIVE_CONTAINER" >/dev/null 2>&1; then
    log "停止旧容器 $LIVE_CONTAINER（--promote）"
    docker rm -fv "$LIVE_CONTAINER" >/dev/null 2>&1 || true
  fi
  log "旧版已停。请把网关/LB 指向金丝雀（或在部署系统里完成替换），并把金丝雀重命名为正式实例。"
else
  log "只做门控（未传 --promote）：旧容器 $LIVE_CONTAINER 未改动。切流由部署侧完成。"
fi

log "完成。异常回滚 = 不执行切流 / 把网关切回旧实例（旧实例未被销毁时零成本）"
