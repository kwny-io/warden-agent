#!/usr/bin/env bash
# 回滚：把 warden-agent 拉回上一个已验证的版本。
#
# 为什么需要它：升级命令写得再漂亮，没有回滚就等于「只有前进档」。
# 出事的时候人是要在压力下操作的——这时候需要的是**一条命令**，
# 而不是「翻 kubectl 文档 + 回忆上一个 revision 是几」。
#
# 契约（fail-closed）：
#   退出码 0  回滚完成且健康检查通过
#   退出码 1  用法/环境错误（缺 kubectl；命名空间推断不出来；**连当前 Deployment 都没看到**；
#            读不到当前 revision 注解）—— 未做任何改动
#   退出码 2  **确实**没有可回滚的历史版本（不做任何改动）
#   退出码 3  回滚指令已下发，但健康检查失败 —— **需要人介入**，不要自动再滚
#
# 命名空间默认值从哪来：`deploy/k8s/namespace.yaml`（清单是事实源）。
# 读不出来就停下要你显式传 —— 本脚本不再持有一个「大概对」的默认值（第五十四批的教训）。
# 同理，“拿不到 Deployment”和“没有更早的版本”必须说成两句话：
# 前者是 1（什么都没看到），后者是 2（看到了、只是没有历史）。合并说会把人带偏。
#
# 用法：
#   scripts/rollback.sh                      # 回滚到上一个 revision（命名空间/名字取自清单）
#   REVISION=3 scripts/rollback.sh           # 回滚到指定 revision
#   DRY_RUN=1 scripts/rollback.sh            # 只打印将要执行的命令
#   NAMESPACE=staging scripts/rollback.sh    # 显式覆盖命名空间
#
# 注意（必须知道的边界）：`rollout undo` 只还原 **Deployment 的 Pod 模板**。
# 如果这一版发布同时改了 ConfigMap / Secret / 数据库 schema，回滚**不会**还原它们——
# 那种情况需要人工按发布说明里的回滚步骤处理，本脚本会在结尾明确提示。

set -uo pipefail

# REPO_ROOT 靠脚本自身位置推断（与 scripts/pin_image_digests.sh 同一手法）：清单就在仓库里。
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
NAMESPACE_MANIFEST="$REPO_ROOT/deploy/k8s/namespace.yaml"

DEPLOYMENT="${DEPLOYMENT:-warden-agent}"
REVISION="${REVISION:-}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"
DRY_RUN="${DRY_RUN:-0}"

EXIT_OK=0
EXIT_USAGE=1
EXIT_NO_HISTORY=2
EXIT_UNHEALTHY=3

# `choose_revision` 的内部返回码（**不是**进程退出码）：“读不到”与“确实没有”分开，
# 因为这两件事在出事时要对人说的话完全不同。
RC_OK=0
RC_NO_CURRENT=4
RC_NO_PREVIOUS=5

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

# --------------------------------------------------------------------------- #
# 命名空间从哪来：清单本身（deploy/k8s/namespace.yaml）
#
# 为什么不在脚本里写死一个字符串：第五十四批就是这么栽的 —— 脚本默认 `warden-agent`，
# 而 namespace.yaml、发布手册（deploy/k8s/README.md，11 条 apply + 手工回滚）通篇是 `warden`。
# 整天没人发现，因为出事前这条路径一次都不会被走到；而真走到时它的表现不是
# 「脚本报错」这么干净：`kubectl get deployment` 的失败被 `2>/dev/null` 吞掉，
# 于是脚本走到「没有可回滚的历史版本」那一行 —— **在那个时间地点为假的一句话**，
# 配上契约里写着「不做任何改动」的退出码 2。值班的人据此得出的结论是
# 「这版回不了，只能往前修」，而历史就在隔壁命名空间里。
#
# 读不出来（文件不在 / 解析不出合法名字）就停下要你显式给值：
# 猜一个默认值，等于把同一类事故再留一次。
# --------------------------------------------------------------------------- #
namespace_from_manifest() {
    local file="$NAMESPACE_MANIFEST" ns
    [ -f "$file" ] || return 1
    # 只认 `kind: Namespace` 之后的第一处 name：同一个文件里还有 ResourceQuota /
    # LimitRange 的名字（`warden-quota` / `warden-limits`），不能抓错。
    ns="$(awk '
        /^kind:[[:space:]]*Namespace[[:space:]]*$/ { in_ns = 1; next }
        in_ns && /^---[[:space:]]*$/ { in_ns = 0 }
        in_ns && /^[[:space:]]+name:[[:space:]]*[^[:space:]]/ {
            line = $0
            sub(/^[[:space:]]+name:[[:space:]]*/, "", line)
            sub(/[[:space:]]+$/, "", line)
            print line
            exit
        }
    ' "$file")" || return 1
    # K8s 命名空间的字符规则：小写字母/数字/连字符，且首尾必须是字母数字
    case "$ns" in
        "" | *[!a-z0-9-]* | -* | *-) return 1 ;;
    esac
    printf '%s' "$ns"
}

if [ -z "${NAMESPACE:-}" ]; then
    NAMESPACE="$(namespace_from_manifest)" || fail "$EXIT_USAGE" "命名空间既没有显式给，也没能从清单推断出来。
看的文件：$NAMESPACE_MANIFEST
这里不回退到任何默认值 —— 猜出来的命名空间在出事时会说假话（第五十四批）。
请显式传：NAMESPACE=<你的命名空间> scripts/rollback.sh（或先确认清单在不在）。未做任何改动。"
fi

current_revision() {
    # 当前在跑的 revision（Deployment 注解）。取不到就返回空串——调用方 fail-closed。
    kubectl -n "$NAMESPACE" get deployment "$DEPLOYMENT" \
        -o jsonpath='{.metadata.annotations.deployment\.kubernetes\.io/revision}' 2>/dev/null
}

# 存在性检查：把「连当前版本都没看到」与「看到了、但确实没有更早的版本」分成两句话。
# 合成一句就是假话（旧版的本条在命名空间写错时说「没有可回滚的历史版本」）。
require_deployment_visible() {
    local err ctx manifest_ns
    if err="$(kubectl -n "$NAMESPACE" get deployment "$DEPLOYMENT" -o name 2>&1 >/dev/null)"; then
        return 0
    fi
    ctx="$(kubectl config current-context 2>/dev/null || true)"
    manifest_ns="$(namespace_from_manifest 2>/dev/null || printf '（读不到清单）')"
    fail "$EXIT_USAGE" "拿不到 Deployment $DEPLOYMENT（namespace=$NAMESPACE）。kubectl 的原话：${err:-（无输出）}
这不是「没有可回滚的历史版本」，而是**连当前版本都没看到**，两者要查的地方不一样。先确认三件事：
  1) 命名空间 / 名字对不对：清单里声明的是 `$manifest_ns`（deploy/k8s/namespace.yaml）
  2) 当前 context 指向的集群对不对：${ctx:-（读不到）}
  3) 手上这份 kubeconfig 有没有对 deployment 的 get 权限
未做任何改动。要覆盖默认值：NAMESPACE=<ns> DEPLOYMENT=<name> scripts/rollback.sh"
}

choose_revision() {
    if [ -n "$REVISION" ]; then
        printf '%s' "$REVISION"
        return "$RC_OK"
    fi
    local cur prev
    cur="$(current_revision)"
    # 「读不到」不是「没有」：注解被清掉/没有读它的权限时，
    # 说「没有历史版本」同样是一句假话，且会让人以为回滚这条路不通。
    [ -n "$cur" ] || return "$RC_NO_CURRENT"
    # rollout history 的表：首行是表头，数据行首列是 revision 号。
    # 取「严格小于当前」的最大值 = 上一个版本（不用当前值 -1，因为 revision 号可能跳号）。
    prev="$(kubectl -n "$NAMESPACE" rollout history "deployment/$DEPLOYMENT" 2>/dev/null \
        | awk 'NR > 1 && $1 ~ /^[0-9]+$/ { print $1 }' \
        | awk -v cur="$cur" '$1 < cur' \
        | tail -n 1)"
    [ -n "$prev" ] || return "$RC_NO_PREVIOUS"
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

require_deployment_visible

target="$(choose_revision)"
choose_rc=$?
case "$choose_rc" in
    "$RC_OK") ;;
    "$RC_NO_CURRENT")
        fail "$EXIT_USAGE" "读不到 Deployment $DEPLOYMENT 的 revision 注解（namespace=$NAMESPACE），所以无法判断「上一个版本」是哪个。
这不是「没有历史版本」，而是**读不到当前是哪个版本**（注解被清掉、或没有读它的权限）。未做任何改动。
想直接指定版本：REVISION=<n> scripts/rollback.sh" ;;
    *)
        fail "$EXIT_NO_HISTORY" "没有可回滚的历史版本（namespace=$NAMESPACE deployment=$DEPLOYMENT，当前 revision=$(current_revision)）。未做任何改动。" ;;
esac

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
