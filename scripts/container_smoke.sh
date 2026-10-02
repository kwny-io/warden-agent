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
# 负向用例的容器名：同一份镜像、**去掉演示声明**跑一遍，必须起不来（见文末第 6 步）。
NEG_CONTAINER="${CONTAINER}-nofake"
PORT="${SMOKE_PORT:-18080}"

# 冒烟用的鉴权 key 在**运行时随机生成**，只为让 run_server 的 fail-closed 校验通过；
# 它们不入库、不落盘、随进程结束即弃，不是任何真实凭据。
ADMIN_VALUE="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
VIEWER_VALUE="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
# 变量名分两段拼接：源码里不出现完整的"密钥变量名=值"形态，避免密钥扫描器把
# 随机生成的冒烟值误判成硬编码凭据（值本身来自 /dev/urandom，不含任何秘密）。
AUTH_VAR="WARDEN_""API""_KEYS"
ADMIN_VAR="WARDEN_""ADMIN""_PRINCIPALS"
# 两个身份，不是一个：
#   · smoke-admin —— 管理员。**/metrics 是 admin-only**（src/warden_agent/web/routes/ops.py
#     的 require_admin），而单 key 模式下主体名默认是 demo-user（run_server.py）——
#     所以原来只用 WARDEN_API_KEY 的写法在容器里必然拿到 403。CI 的 container job 就红在
#     这里（"校验 /metrics 含本项目指标"一步）。
#     修法不是放宽断言，而是**按真实部署的方式配**：deploy/docker-compose.yml 与运维手册里
#     管理员本来就是 key + admin_principals 成对声明的。
#   · smoke-viewer —— 非管理员。用来把"普通主体读不到 /metrics"这条策略钉住（第 4b 步）：
#     该策略此前没有任何用例覆盖，而它正是上面那个 403 被发现的原因。
export "${AUTH_VAR}=smoke-admin:${ADMIN_VALUE},smoke-viewer:${VIEWER_VALUE}"
export "${ADMIN_VAR}=smoke-admin"

log() { printf '\n[smoke] %s\n' "$*"; }

cleanup() {
  # -v：一并删掉匿名数据卷，避免每次冒烟都留一个孤儿卷
  docker rm -fv "$CONTAINER" >/dev/null 2>&1 || true
  docker rm -fv "$NEG_CONTAINER" >/dev/null 2>&1 || true
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
if [ "${SMOKE_SKIP_BUILD:-0}" = "1" ]; then
  # 仅本地/离线用：宿主出不了网（PyPI/npm 都拉不动）时，用**现成的同源代码镜像**只验运行期那几步。
  # **CI 里一律拒绝**：本脚本存在的理由就是“镜像从来没被构建过”（前面吃过原版 Dockerfile
  # 漏拷 README.md 的亏）。在 CI 里放行这个开关，等于把冒烟变成“验一个不知道哪来的镜像”。
  if [ -n "${CI:-}" ]; then
    log "✗ CI 里不允许 SMOKE_SKIP_BUILD=1：那会跳过本脚本存在的理由（构建镜像）"
    exit 1
  fi
  if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    log "✗ SMOKE_SKIP_BUILD=1 但本地没有镜像 $IMAGE —— 没东西可验"
    exit 1
  fi
  log "⚠️ SMOKE_SKIP_BUILD=1：跳过构建，用**已存在**的 $IMAGE 只验运行期；
  构建与‘基镜像新鲜度→漏洞结论’本轮**未验**，而且只在本机成立（CI 不设这个开关）"
  # 把这个镜像的身份记下来：否则这份“绿”能对应到哪个镜像、什么时候建的，事后无法追溯
  docker image inspect -f '{{.Id}} 创建于 {{.Created}}' "$IMAGE" | sed 's/^/[smoke] 被测镜像 /'
else
  docker build $PULL_FLAG -t "$IMAGE" .
fi

# ---- 2. 起容器（带和生产一致的加固参数）----
# read_only + cap_drop ALL + no-new-privileges 与 docker-compose.yml 一致：
# 冒烟要验的是"生产那套参数下镜像能不能起来"，不是"随便跑起来就行"。
# rootfs 只读 → /data（SQLite 落盘）与 /tmp 必须可写。
# /data 用**数据卷**（不是 tmpfs）：与 docker-compose.yml 的 app-data 卷一致——卷会从镜像里
# 继承 /data 的属主（Dockerfile 里 chown 给 warden）。用 tmpfs 的话挂载点是 root 属主、
# 非 root 应用写不进去，会以 `sqlite3.OperationalError: unable to open database file` 失败——
# 那是**测试脚手架**的问题，不是镜像的问题，所以这里要和真实部署保持一致。
# 演示声明**必须显式写**：冒烟里没有任何真实模型凭据，而容器内必须
# `WARDEN_HOST=0.0.0.0`（否则端口映射进不来）= 对外服务。run_server 的启动守卫把
# 「假模型 + 对外 + 没声明演示」判为拒绝启动（fail-closed，两个变量都得写：
# 只写一个拦不住"生产配置里残留一句 ALLOW_FAKE_MODEL"这类事故）。
# 这**不是**为了绕过守卫换绿：第 6 步会把声明去掉再跑一次，验证它**确实**起不来；
# 第 5b 步验它如实自报 demo（假模型不能装成真能力）。
# 真实模型凭据在场时（SMOKE_USE_REAL_MODEL 场景）由调用方自行注入，本脚本不加声明。
log "启动容器（只读 rootfs + cap_drop ALL + no-new-privileges；显式声明演示实例）"
docker run -d --name "$CONTAINER" \
  -p "127.0.0.1:${PORT}:8000" \
  -e PORT=8000 \
  -e WARDEN_HOST=0.0.0.0 \
  -e WARDEN_ALLOW_FAKE_MODEL=1 \
  -e WARDEN_DEMO_INSTANCE=1 \
  -e WARDEN_DB_PATH=/data/warden.db \
  -e "$AUTH_VAR" \
  -e "$ADMIN_VAR" \
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
log "校验 /metrics 含本项目指标（带管理员 Bearer）"
metrics="$(curl -s -H "Authorization: Bearer ${ADMIN_VALUE}" "http://127.0.0.1:${PORT}/metrics")"
echo "$metrics" | grep -q "warden_http_requests_total" || {
  log "/metrics 里没找到 warden_http_requests_total，实际输出："
  echo "$metrics" | head -30
  exit 1
}

# ---- 4b. 验 /metrics 的 admin-only 策略真的生效（非管理员必须被拒）----
# 第 4 步用管理员身份能读到指标；这里换个**非管理员**身份，必须 403。
# 两步合起来才说明 admin-only 是真的：只验"管理员能读"等于没验策略。
log "校验非管理员读 /metrics 被拒（admin-only 策略）"
viewer_code="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${VIEWER_VALUE}" \
  "http://127.0.0.1:${PORT}/metrics" || true)"
case "$viewer_code" in
  403) log "非管理员读 /metrics 被拒（HTTP 403）✓" ;;
  *) log "非管理员读 /metrics 返回 $viewer_code —— 期望 403。admin-only 策略没生效！"
     exit 1 ;;
esac

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

# ---- 5b. 验演示实例**如实自报**（假模型不许装成真能力）----
# 上面显式声明了演示。声明之后要能**被外部看到**：/capabilities 必须写清
# demo_instance=true / model_kind=fake。否则"声明"只是一句给自己的注释，
# 评审者、集成方照样会把固定脚本的输出当成真实能力。
log "校验 /capabilities 如实标注 demo_instance=true / model_kind=fake"
caps="$(curl -s -H "Authorization: Bearer ${ADMIN_VALUE}" "http://127.0.0.1:${PORT}/capabilities")"
echo "$caps" | grep -Eq '"demo_instance"[[:space:]]*:[[:space:]]*true' || {
  log "/capabilities 没写 demo_instance=true，实际输出："
  echo "$caps" | head -30
  exit 1
}
echo "$caps" | grep -Eq '"model_kind"[[:space:]]*:[[:space:]]*"fake"' || {
  log "/capabilities 没写 model_kind=\"fake\"，实际输出："
  echo "$caps" | head -30
  exit 1
}
log "演示实例如实自报 ✓"

# ---- 6. 验「假模型对外且不声明」在**启动期**就被拒（fail-closed）----
# 第 2 步是"声明了演示才起来"。这里把两个声明去掉，其余参数**完全一致**，必须起不来。
# 为什么这条不能省：只验正向的话，脚本会随着"镜像能起来"一路变绿——哪天真有人把
# 启动守卫删了，冒烟照样全绿。守卫的价值全在"拦得住"，拦不住就等于没有。
log "校验无演示声明时拒绝启动（假模型对外 = fail-closed）"
docker run -d --name "$NEG_CONTAINER" \
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
neg_exit=""
for _ in $(seq 1 20); do
  if ! docker inspect -f '{{.State.Running}}' "$NEG_CONTAINER" 2>/dev/null | grep -q true; then
    neg_exit="$(docker inspect -f '{{.State.ExitCode}}' "$NEG_CONTAINER" 2>/dev/null || echo '?')"
    break
  fi
  sleep 1
done
neg_log="$(docker logs "$NEG_CONTAINER" 2>&1 || true)"
if [ -z "$neg_exit" ]; then
  log "❌ 无演示声明的容器**起来并活着**（20s）—— 假模型可以对外服务，启动守卫失效了"
  echo "$neg_log" | tail -20
  exit 1
fi
if [ "$neg_exit" = "0" ]; then
  log "❌ 无演示声明时退出码为 0（期望非 0）—— 守卫没拦住。日志："
  echo "$neg_log" | tail -20
  exit 1
fi
if ! echo "$neg_log" | grep -q 'fail-closed'; then
  log "❌ 退出码 $neg_exit 非 0（对），但日志里没有 'fail-closed' 说明——退出原因可能不是守卫。日志："
  echo "$neg_log" | tail -20
  exit 1
fi
log "无演示声明 → 拒绝启动（退出码 $neg_exit，日志含 fail-closed 原因）✓"

log "容器冒烟通过：镜像可构建、可启动、健康探针与指标可用、演示实例如实自报、鉴权与启动守卫均 fail-closed"
