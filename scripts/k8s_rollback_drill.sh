#!/usr/bin/env bash
# 在**真的** k8s 集群上演练 `scripts/rollback.sh`（而不是假 kubectl）。
#
# 为什么需要它：假 kubectl 只能回答「你问的那个抽屉里有什么」，答不出「脚本自己会去哪个
# 抽屉」。第五十四批的 8 条假-kubectl 用例全绿，却漏掉了默认命名空间写死 `warden-agent`
# 这个真缺陷；第五十八批第一次拿真集群跑，又当场抓到一条：错误路径里写了文档式的
# `$manifest_ns`（反引号在 shell 里是命令替换）→ 屏幕上多一行 `warden: command not found`，
# 而「清单里声明的是 ___」后面是空的 —— 出事时最该看到的值消失了。
#
# 前置条件（都是本机事实，不需要网络）：
#   1) 有一个 kind 节点容器（默认名 warden-control-plane，可用 K8S_NODE 覆盖）
#   2) 宿主机有 docker；kubectl 用**节点里的**（/usr/bin/kubectl + /etc/kubernetes/admin.conf），
#      所以宿主机 ~/.kube/config 对不对、端口有没有被 Windows 保留段占掉都不影响本演练
#
# 用法：
#   scripts/k8s_rollback_drill.sh
#
# 它做什么：把仓库里的 deploy/k8s/ 与 scripts/rollback.sh 拷进节点，然后按契约逐个走一遍：
#   用例 1  造一个新 revision 再回滚（**不传 NAMESPACE**，命名空间必须从清单读出来）→ 期望 0
#   用例 2  一个只有 1 个 revision 的 Deployment                → 期望 2（确实没有历史）
#   用例 3  命名空间打错                                   → 期望 1 + kubectl 原话 + 清单命名空间
#   用例 4  脚本被挪到没有清单的地方                         → 期望 1（不猜默认值）
#   用例 5  DRY_RUN=1                                      → 期望 0 且不动集群
# 结束时把集群恢复到演练前的 revision（用例 1 的回滚本身就是恢复）。

set -uo pipefail

NODE="${K8S_NODE:-warden-control-plane}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
DRILL_NS="${DRILL_NS:-warden-drill}"
DEPLOYMENT="${DEPLOYMENT:-warden-agent}"
KUBECONFIG_IN_NODE="/etc/kubernetes/admin.conf"
WORKDIR="/tmp/k8s-rollback-drill"
FAILED=0

say() { printf '%s\n' "[drill] $*"; }
bad() { FAILED=$((FAILED + 1)); printf '%s\n' "[drill] ✗ $*"; }
good() { printf '%s\n' "[drill] ✓ $*"; }

K() { docker exec "$NODE" kubectl --kubeconfig="$KUBECONFIG_IN_NODE" "$@"; }
SCRIPT() { docker exec -e KUBECONFIG="$KUBECONFIG_IN_NODE" "$@" "$NODE" bash "$WORKDIR/repo/scripts/rollback.sh"; }

expect_rc() {
    # expect_rc <期望退出码> <实际退出码> <说明>
    if [ "$1" = "$2" ]; then good "$3（退出码 $2）"; else bad "$3：期望退出码 $1，实际 $2"; fi
}

command -v docker >/dev/null 2>&1 || { say "没有 docker，无法演练"; exit 1; }
docker inspect "$NODE" >/dev/null 2>&1 || { say "找不到节点容器 $NODE（用 K8S_NODE= 指定）"; exit 1; }
K get ns >/dev/null 2>&1 || { say "节点里的 kubectl 连不上 API（集群没起？）"; exit 1; }

say "把清单与脚本拷进 $NODE:$WORKDIR/repo（保持 <repo>/scripts + <repo>/deploy 的布局：脚本靠自身位置推断仓库根）"
docker exec "$NODE" sh -c "rm -rf $WORKDIR && mkdir -p $WORKDIR/repo"
( cd "$REPO_ROOT" && tar cf - deploy/k8s scripts/rollback.sh ) \
    | docker exec -i "$NODE" sh -c "tar xf - -C $WORKDIR/repo"
docker exec "$NODE" sh -c "chmod +x $WORKDIR/repo/scripts/rollback.sh"

namespace_in_manifest() {
    # 与 rollback.sh 同源的读法：只认 kind: Namespace 之后的第一处 name
    awk '
        /^kind:[[:space:]]*Namespace[[:space:]]*$/ { in_ns = 1; next }
        in_ns && /^---[[:space:]]*$/ { in_ns = 0 }
        in_ns && /^[[:space:]]+name:[[:space:]]*[^[:space:]]/ {
            line = $0
            sub(/^[[:space:]]+name:[[:space:]]*/, "", line)
            sub(/[[:space:]]+$/, "", line)
            print line
            exit
        }
    ' "$REPO_ROOT/deploy/k8s/namespace.yaml"
}
NS="$(namespace_in_manifest)"
[ -n "$NS" ] || { say "读不出清单里的命名空间"; exit 1; }
say "清单声明的命名空间 = $NS"

# --------------------------------------------------------------------------- #
say "用例 1：造一个新 revision，再**不传 NAMESPACE** 回滚（期望 0，且恢复成原来的 Pod 模板）"
before_rev="$(K -n "$NS" get deploy "$DEPLOYMENT" -o jsonpath='{.metadata.annotations.deployment\.kubernetes\.io/revision}')"
before_env="$(K -n "$NS" get deploy "$DEPLOYMENT" -o jsonpath='{.spec.template.spec.containers[0].env[*].name}')"
K -n "$NS" set env "deploy/$DEPLOYMENT" WARDEN_DRILL_MARK=1 >/dev/null 2>&1
K -n "$NS" rollout status "deploy/$DEPLOYMENT" --timeout=180s >/dev/null 2>&1
bumped_env="$(K -n "$NS" get deploy "$DEPLOYMENT" -o jsonpath='{.spec.template.spec.containers[0].env[*].name}')"
[ "$bumped_env" = "WARDEN_DRILL_MARK" ] \
    && good "新 revision 已就位（env=$bumped_env，原 revision=$before_rev）" \
    || bad "没能造出新 revision（env=$bumped_env）"

out="$(SCRIPT 2>&1)"; rc=$?
printf '%s\n' "$out" | sed 's/^/        /'
expect_rc 0 "$rc" "真集群回滚"
after_env="$(K -n "$NS" get deploy "$DEPLOYMENT" -o jsonpath='{.spec.template.spec.containers[0].env[*].name}')"
after_rev="$(K -n "$NS" get deploy "$DEPLOYMENT" -o jsonpath='{.metadata.annotations.deployment\.kubernetes\.io/revision}')"
[ -z "$after_env" ] && good "Pod 模板已还原（演练用的 env 不在了，revision=$after_rev）" \
                    || bad "Pod 模板没还原（env=$after_env）"
printf '%s' "$out" | grep -q "namespace=$NS" && good "命名空间是从清单读出来的（$NS）" \
                                            || bad "没说清用的是哪个命名空间"
printf '%s' "$out" | grep -q "command not found" && bad "脚本里有 shell 错误（command not found）" \
                                                  || good "没有 shell 错误"

# --------------------------------------------------------------------------- #
say "用例 2：只有 1 个 revision 的 Deployment（期望 2：确实没有历史）"
K create ns "$DRILL_NS" >/dev/null 2>&1
K -n "$DRILL_NS" create deployment "$DEPLOYMENT" \
    --image=docker.m.daocloud.io/library/alpine:3.20 -- \
    /bin/sh -c 'while true; do sleep 3600; done' >/dev/null 2>&1
out="$(SCRIPT -e NAMESPACE="$DRILL_NS" 2>&1)"; rc=$?
printf '%s\n' "$out" | sed 's/^/        /'
expect_rc 2 "$rc" "没有历史版本"
printf '%s' "$out" | grep -q "未做任何改动" && good "明确说了没做任何改动" || bad "没说「未做任何改动」"

# --------------------------------------------------------------------------- #
say "用例 3：命名空间打错（期望 1 + kubectl 原话 + 清单里声明的那个命名空间）"
out="$(SCRIPT -e NAMESPACE="${NS}-nope" 2>&1)"; rc=$?
printf '%s\n' "$out" | sed 's/^/        /'
expect_rc 1 "$rc" "看不到 Deployment"
printf '%s' "$out" | grep -q "NotFound" && good "带了 kubectl 的原话" || bad "没带 kubectl 原话"
printf '%s' "$out" | grep -qE "清单里声明的是[[:space:]]*${NS}" \
    && good "写出了清单里声明的命名空间（$NS）" \
    || bad "「清单里声明的是」后面没有值（第五十八批真集群演练抓到的那条）"
printf '%s' "$out" | grep -q "command not found" && bad "错误路径上有 shell 错误" || good "没有 shell 错误"

# --------------------------------------------------------------------------- #
say "用例 4：脚本被挪到没有清单的地方（期望 1：不猜默认值）"
docker exec "$NODE" sh -c "mkdir -p ${WORKDIR}/bare && cp $WORKDIR/repo/scripts/rollback.sh ${WORKDIR}/bare/"
out="$(docker exec -e KUBECONFIG="$KUBECONFIG_IN_NODE" "$NODE" bash "${WORKDIR}/bare/rollback.sh" 2>&1)"; rc=$?
printf '%s\n' "$out" | sed 's/^/        /'
expect_rc 1 "$rc" "清单读不到"
printf '%s' "$out" | grep -q "请显式传" && good "要求显式传 NAMESPACE" || bad "没要求显式传 NAMESPACE"

# --------------------------------------------------------------------------- #
say "用例 5：DRY_RUN=1（期望 0，且集群一个字节都不动）"
rev_before="$(K -n "$NS" get deploy "$DEPLOYMENT" -o jsonpath='{.metadata.annotations.deployment\.kubernetes\.io/revision}')"
out="$(SCRIPT -e DRY_RUN=1 -e NAMESPACE="$NS" 2>&1)"; rc=$?
printf '%s\n' "$out" | sed 's/^/        /'
expect_rc 0 "$rc" "dry-run"
rev_after="$(K -n "$NS" get deploy "$DEPLOYMENT" -o jsonpath='{.metadata.annotations.deployment\.kubernetes\.io/revision}')"
[ "$rev_before" = "$rev_after" ] && good "dry-run 没有改集群（revision 仍是 $rev_after）" \
                                || bad "dry-run 改了集群（$rev_before → $rev_after）"

# --------------------------------------------------------------------------- #
say "清理演练用命名空间 $DRILL_NS"
K delete ns "$DRILL_NS" --wait=false >/dev/null 2>&1

if [ "$FAILED" -eq 0 ]; then
    say "全部用例符合契约"
    exit 0
fi
say "$FAILED 条不符合契约"
exit 1
