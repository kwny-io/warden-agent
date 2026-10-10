#!/usr/bin/env bash
# 对 **registry 抖动** 做退避重试：docker 的 build / push / pull / login / buildx imagetools inspect
# 都要走 registry，而 registry 会限流（429）、会 5xx、会超时、会把连接掐掉。
#
# 为什么值得一个脚本（而不是在 workflow 里手写 while）：
#   **第三方限流能把一个已交付的提交标成红。** 2026-10-09 实测：提交 `f5901ac` 的 CI
#   首轮红，同一棵树的另一个提交全绿。两处现场都死在 Docker Hub 匿名拉取限流：
#   ① `sbom` 作业的 `docker build --pull`；
#   ② **`container` 作业真正跑构建的那一处**——它在 `scripts/container_smoke.sh` 里
#      （不是 ci.yml：ci.yml 里 `docker build --pull -t "$image"` 那一行属于 `image-signing`）。
#   ③ **普查抓到的第三处**（同一类匿名拉取，本批一并接线）：`ci.yml:85` 的
#      `docker pull prom/prometheus:v2.55.0`——`quality` 作业预热 promtool 求值要用的镜像。
#      它原先是一条裸 `run:`，抖一下整个门禁就红。`docker pull` 幂等，重试没有重复副作用。
#      （判据写进 tests/test_registry_retry.py 第 7 节；那里同时写明了**故意不包**的边界。）
#   报错原文：`#5 [internal] load metadata for docker.io/library/node:20-slim` →
#   `unexpected status from HEAD request to registry-1.docker.io/v2/library/node/manifests/20-slim:
#    429 Too Many Requests` → `failed to solve`。
#   代码一行没错，绿的红的却不一样 ⇒ 「红」这件事本身不可信了，得先把抖动吃掉。
#
# 设计取舍：
#   1) **只重试可重试的错**。构造错了（Dockerfile 语法、缺文件）、认证错了、配置错了，
#      重试只会让红来得更慢、还把真原因刷成一行行"第 N 次"。不可重试 ⇒ 立刻失败（rc 2）。
#      判据见下面 `RETRYABLE_RE` 的注释：**不许拿短词做全文匹配**。
#   2) **stdout 只透传"最终成功那一次"的输出**，日志一律走 stderr。因为上游会这样用：
#        digest=$(bash scripts/registry_retry.sh -- docker buildx imagetools inspect ...)
#      一旦把「第 2 次尝试」这类日志写进 stdout、或者把**失败尝试**留下的半截 stdout 也接上去，
#      digest 就被污染了（而 `ci.yml` 是用 `grep -qE '^sha256:[0-9a-f]{64}$'` **逐行**校验它的，
#      脏值混进去不会报错，只会往下走）。所以失败尝试的 stdout **整段丢掉**。
#      代价：成功那一次的 stdout 要等命令跑完才出现（构建进度本来就走 stderr，无影响）。
#   3) **stderr 落临时文件、本次尝试结束后整体回显**：判据要读错误原文，而 `2> >(tee …)`
#      这种进程替换是**异步**的——判据可能在 `tee` 落盘前就读到半截（复核 #3 指出的平台相关性）。
#      改成同步重定向去掉这层不确定性；**代价要认下来**：一次尝试跑多久，屏幕就安静多久
#      （构建进度本来走 stderr，所以十几分钟的构建在 CI 上不再有中间输出，只有收尾回显）。
#   4) 回执与判据都在日志里：每次尝试都写明「第 N/M 次」与错误首行，
#      并且**每一行都带一个 ASCII 机器标记**（`OK` / `RETRY` / `NONRETRYABLE` / `EXHAUSTED`
#      ＋ `attempt=n/m`）——因为 CI 日志与用例都要能 grep 它，而中文在不同终端下
#      会被不同编码解（本机 cp936 实测会把中文解成乱码，ASCII 标记不会）。
#   5) 给 GitHub 的 step summary 也写一行（`$GITHUB_STEP_SUMMARY` 存在时）：
#      rc 2 与 rc 3 在 UI 上只差一个数字，看的人得能一眼分清
#      「这次是配置/代码错，别重跑」还是「registry 抖动，重跑就行」。
#
# 用法:
#   bash scripts/registry_retry.sh -- docker build --pull -t warden-agent:sbom .
#   bash scripts/registry_retry.sh -- docker push "$image"
#   bash scripts/registry_retry.sh -- docker pull prom/prometheus:v2.55.0
#   digest=$(bash scripts/registry_retry.sh -- docker buildx imagetools inspect "$image" \
#     --format '{{.Manifest.Digest}}')
#
# 退出码:
#   0  成功（可能前面已经吃掉了几次可重试的失败）
#   1  用法/参数错（没给 `--`、命令为空、环境变量非法）
#   2  不可重试的失败 —— 立刻失败，不消耗剩余尝试（构造错/认证错/配置错）
#   3  尝试用尽，仍是可重试错误 —— 抖动没被吃掉，先看 registry 状态再重跑
#
# 环境变量:
#   REGISTRY_RETRY_ATTEMPTS  总尝试次数（默认 3，必须是 >=1 的整数）
#   REGISTRY_RETRY_SLEEPS    每次失败后的等待秒数，空格分隔（默认 "20 60"；
#                            最后一次失败后不再等待。不足时沿用最后一个值）
# 时间预算：最坏 = 尝试次数 × 该命令耗时 + 求和(SLEEPS 前 n-1 档)。
#   例：3 次 × 约 5 分钟的构建 + (20+60)s ≈ 16 分钟 —— 小于 `image-signing` 的
#   `timeout-minutes: 30`。改大 `REGISTRY_RETRY_ATTEMPTS` 时要重算这个数。

set -u

ATTEMPTS="${REGISTRY_RETRY_ATTEMPTS:-3}"
SLEEPS="${REGISTRY_RETRY_SLEEPS:-20 60}"

# 可重试的判据：**每条措辞都必须自己就足够独特**（长短语，或必须成对出现），
# 不许拿 `429`、`EOF`、`no such host` 这种短词做全文匹配——第一版就是那样写的，会误伤：
#   - `dockerfile parse error on line 429` 里也有 `429` ⇒ 构造错被当成限流，重试三遍
#     还打印「这不是代码问题」，把注意力从真原因上引开（归因反了）；
#   - `unexpected EOF`（Dockerfile 被截断）会被裸 `EOF` 命中 → 同上；
#   - `no such host` 也可能是"registry 域名写错了"——那是配置错，必须立刻失败。
# 抖动侧的真身长这样（实测）：`unexpected status from HEAD request … : 429 Too Many Requests`；
# `received unexpected HTTP status: 500 Internal Server Error`。所以下面每条都是
# 长短语或成对措辞，构造错文本里不会出现。
# 反例（`manifest unknown`、`denied`、`unauthorized`、"语法错"）**故意不在清单里**：
# 它们不是抖动，重试只会更慢。
RETRYABLE_RE='429 Too Many Requests|toomanyrequests|unexpected status from HEAD request|unexpected HTTP status: 5|Bad Gateway|Service Unavailable|Gateway Timeout|Internal Server Error|TLS handshake timeout|i/o timeout|Client.Timeout exceeded|Connection reset|connection reset|Temporary failure in name resolution'

log()  { printf '%s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; summary "- ❌ $*"; exit "${2:-1}"; }

# GitHub 的 step summary：存在才写。这是给人看的"这次该怎么处置"的结论。
summary() {
  [ -n "${GITHUB_STEP_SUMMARY:-}" ] || return 0
  printf '%s\n' "$*" >> "$GITHUB_STEP_SUMMARY" 2>/dev/null || true
}

usage() { sed -n '2,/^set -u/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; }

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  --) shift ;;
  *) die "用法：bash scripts/registry_retry.sh -- <命令> [参数…]（要跑的命令前必须有 --；-h 看帮助）" 1 ;;
esac
[ "$#" -ge 1 ] || die "缺少要跑的命令" 1
case "$ATTEMPTS" in ''|*[!0-9]*) die "REGISTRY_RETRY_ATTEMPTS 必须是正整数，当前为 '$ATTEMPTS'" 1 ;; esac
[ "$ATTEMPTS" -ge 1 ] || die "REGISTRY_RETRY_ATTEMPTS 必须 >= 1，当前为 '$ATTEMPTS'" 1

sleep_for() {  # $1 = 第几次失败（从 1 开始）
  local idx=$(( $1 - 1 )) i=0 val=""
  for val in $SLEEPS; do
    [ "$i" -eq "$idx" ] && { printf '%s' "$val"; return 0; }
    i=$(( i + 1 ))
  done
  # 需要比给的档数更多的等待时：沿用最后一个值（不因为档数不够就退化成不等待）
  printf '%s' "${val:-0}"
}

attempt=1
seen=""      # 每次失败的首行都留一份：用尽时一并打印，别让前几次的现场只剩一行"第 N 次"
# 临时文件只建一次、每轮复用（每轮都 mktemp 会让前几轮的文件落在 /tmp 里没人收）。
# `mktemp` 失败时必须带着身份失败：拿到空字符串后 `>"$outlog"` 就成了「重定向到空文件名」，
# 判据读到空值——那是「查不动」，不是「没问题」。
errlog="$(mktemp)" || die "建不出临时文件（mktemp 失败），无法判定 registry 状态" 2
outlog="$(mktemp)" || die "建不出临时文件（mktemp 失败），无法判定 registry 状态" 2
# 退出时统一收掉：成功 / 不可重试 / 用尽 / 被中断四条路径都覆盖，不再散点 rm（散点会漏）
trap 'rm -f "$outlog" "$errlog" 2>/dev/null || true' EXIT
while [ "$attempt" -le "$ATTEMPTS" ]; do
  log "[registry_retry] 第 ${attempt}/${ATTEMPTS} 次：$*"
  # stderr 进临时文件（本次尝试结束后整体回显，见文件头第 3 条）；stdout：先落文件，
  # **只有成功那一次**才原样吐出去——失败尝试的半截 stdout 会污染上游的 $( )。
  # 同步重定向，不用 `tee` + 进程替换：进程替换是异步的，判据读 $errlog 可能读到半截。
  # 复核 #3 提过这个风险；本机用 200KB stderr、429 收尾连跑 8 次**未复现**（8/8 判定正确），
  # 仍改同步以去掉平台相关性。代价：构建日志不再是流式，而是本次尝试结束后整体回显到 stderr。
  "$@" >"$outlog" 2>"$errlog"
  rc=$?
  cat "$errlog" >&2
  err="$(cat "$errlog" 2>/dev/null || true)"
  first_line="$(printf '%s\n' "$err" | grep -v '^[[:space:]]*$' | head -1)"

  if [ "$rc" -eq 0 ]; then
    log "[registry_retry] OK attempt=${attempt}/${ATTEMPTS}"
    if [ "$attempt" -eq 1 ]; then
      log "[registry_retry] 一次成功"
    else
      log "[registry_retry] 第 ${attempt}/${ATTEMPTS} 次成功（前面 $(( attempt - 1 )) 次是可重试的失败，已吃掉）"
      summary "- ✅ registry 抖动被吃掉：第 ${attempt}/${ATTEMPTS} 次成功（前 $(( attempt - 1 )) 次失败）"
    fi
    cat "$outlog"
    exit 0      # 临时文件由 EXIT trap 统一收，这里不再单独 rm
  fi

  # 失败：stdout 丢弃（下一次尝试的 >"$outlog" 会截断它），stderr 已在上方回显过
  seen="${seen}${seen:+；}第${attempt}次(rc=${rc})：${first_line}"

  if ! printf '%s' "$err" | grep -qE "$RETRYABLE_RE"; then
    log "[registry_retry] NONRETRYABLE rc=${rc} attempts_used=${attempt}"
    log "[registry_retry] 不可重试的失败（rc=${rc}，不在可重试清单里）⇒ 立刻失败，不消耗剩余尝试"
    log "[registry_retry] 错误首行：${first_line}"
    summary "- ❌ 不可重试的失败（rc=${rc}，只跑了 ${attempt} 次）：这不像 registry 抖动，**别重跑**，先看上面的错误原文。"
    exit 2
  fi

  if [ "$attempt" -ge "$ATTEMPTS" ]; then
    log "[registry_retry] EXHAUSTED attempts=${ATTEMPTS} rc=${rc}"
    log "[registry_retry] ${ATTEMPTS} 次尝试用尽，仍是可重试错误（rc=${rc}）"
    log "[registry_retry] 各次错误首行：${seen}"
    log "[registry_retry] 这不是代码问题：请先看 registry 状态/限流，再重跑这一步。"
    summary "- ⚠️ ${ATTEMPTS} 次尝试用尽，全是 registry 抖动（rc=${rc}）：**重跑这一步就行**，不是代码问题。经过：${seen}"
    exit 3
  fi

  nap="$(sleep_for "$attempt")"
  log "[registry_retry] RETRY attempt=${attempt}/${ATTEMPTS} sleep=${nap}"
  log "[registry_retry] 第 ${attempt}/${ATTEMPTS} 次失败（可重试），${nap}s 后重试。错误首行：${first_line}"
  sleep "$nap"
  attempt=$(( attempt + 1 ))
done

die "不该走到这里" 3
