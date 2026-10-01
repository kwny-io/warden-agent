#!/usr/bin/env bash
# 回滚：把 warden-agent 拉回上一个已验证的版本。
#
# 为什么需要它：升级命令写得再漂亮，没有回滚就等于「只有前进档」。
# 出事的时候人是要在压力下操作的——这时候需要的是**一条命令**，
# 而不是「翻 kubectl 文档 + 回忆上一个 revision 是几」。
#
# 契约（fail-closed）：
#   退出码 0  回滚完成且健康检查通过
#   退出码 2  没有可回滚的历史版本（不做任何改动）
#   退出码 3  回滚指令已下发，但健康检查失败 —— **需要人介入**，不要自动再滚
#   退出码 1  用法/环境错误（缺 kubectl 等），未做任何改动
#
# 用法：
#   scripts/rollback.sh                      # 回滚到上一个 revision
#   REVISION=3 scripts/rollback.sh           # 回滚到指定 revision
#   DRY_RUN=1 scripts/rollback.sh            # 只打印将要执行的命令
#   NAMESPACE=staging DEPLOYMENT=warden-agent scripts/rollback.sh
#
# 注意（必须知道的边界）：`rollout undo` 只还原 **Deployment 的 Pod 模板**。
# 如果这一版发布同时改了 ConfigMap / Secret / 数据库 schema，回滚**不会**还原它们——
# 那种情况需要人工按发布说明里的回滚步骤处理，本脚本会在结尾明确提示。

set -uo pipefail

NAMESPACE="${NAMESPACE:-warden-agent}"
DEPLOYMENT="${DEPLOYMENT:-warden-agent}"
REVISION="${REVISION:-}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"
DRY_RUN="${DRY_RUN:-0}"

EXIT_OK=0
EXIT_USAGE=1
EXIT_NO_HISTORY=2
EXIT_UNHEALTHY=3

say() { printf '%s\n' "[rollback] $*"; }
fail() { local code="$1"; shift; printf '%s\n' "::error::$*" >&2; exit "$code"; }

run() {
    # DRY_RUN 时只打印。集中在这里，避免「有人漏了一个 dry-run 判断」。
    if [ "$DRY_RUN" = "1" ]; then
        printf '[dry-run] %s\n' "$*"
        return 0
    fi
    "$@"
}

command -v kubectl >/dev/null 2>&1 || fail "$EXIT_USAGE" "找不到 kubectl：本脚本假设你在能访问集群的位置运行。"

current_revision() {
    # 当前在跑的 revision（Deployment 注解）。取不到就返回空串——调用方 fail-closed。
    kubectl -n "$NAMESPACE" get deployment "$DEPLOYMENT" \
        -o jsonpath='{.metadata.annotations.deployment\.kubernetes\.io/revision}' 2>/dev/null
}

choose_revision() {
    if [ -n "$REVISION" ]; then
        printf '%s' "$REVISION"
        return 0
    fi
    local cur prev
    cur="$(current_revision)"
    [ -n "$cur" ] || return 1
    # rollout history 的表：首行是表头，数据行首列是 revision 号。
    # 取「严格小于当前」的最大值 = 上一个版本（不用当前值 -1，因为 revision 号可能跳号）。
    prev="$(kubectl -n "$NAMESPACE" rollout history "deployment/$DEPLOYMENT" 2>/dev/null \
        | awk 'NR > 1 && $1 ~ /^[0-9]+$/ { print $1 }' \
        | awk -v cur="$cur" '$1 < cur' \
        | tail -n 1)"
    [ -n "$prev" ] || return 1
    printf '%s' "$prev"
}

health_ok() {
    if ! kubectl -n "$NAMESPACE" rollout status "deployment/$DEPLOYMENT" \
        --timeout="${HEALTH_TIMEOUT}s" >/dev/null 2>&1; then
        return 1
    fi
    local desired ready
    desired="$(kubectl -n "$NAMESPACE" get deployment "$DEPLOYMENT" -o jsonpath='{.spec.replicas}' 2>/dev/null)"
    ready="$(kubectl -n "$NAMESPACE" get deployment "$DEPLOYMENT" -o jsonpath='{.status.readyReplicas}' 2>/dev/null)"
    [ -n "$desired" ] || return 1
    # readyReplicas 在「没有任何 Pod ready」时是**空串**（字段被省略），不能当成 0。
    [ "${ready:-0}" = "$desired" ]
}

target="$(choose_revision)" || fail "$EXIT_NO_HISTORY" \
    "没有可回滚的历史版本（namespace=$NAMESPACE deployment=$DEPLOYMENT）。未做任何改动。"

say "从 revision $(current_revision) 回滚到 revision $target（namespace=$NAMESPACE）"
run kubectl -n "$NAMESPACE" rollout undo "deployment/$DEPLOYMENT" --to-revision="$target" \
    || fail "$EXIT_USAGE" "rollout undo 执行失败，未确认回滚是否生效——请人工检查集群。"

if [ "$DRY_RUN" = "1" ]; then
    say "dry-run：未真正下发，跳过健康检查。"
    exit "$EXIT_OK"
fi

if health_ok; then
    say "回滚完成，且全部副本 ready。"
    say "提醒：ConfigMap / Secret / 数据库 schema 的变更**不在**本脚本的回滚范围内，请按发布说明人工确认。"
    exit "$EXIT_OK"
fi

fail "$EXIT_UNHEALTHY" "回滚已下发但健康检查未通过（${HEALTH_TIMEOUT}s 内未全部 ready）。请人工介入，不要盲目再回滚一版——连续回滚会把状态变得更难判断。"
