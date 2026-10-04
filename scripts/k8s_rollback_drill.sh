#!/usr/bin/env bash
# 在**真的** k8s 集群上演练 `scripts/rollback.sh`（而不是假 kubectl）。
#
# 为什么需要它：假 kubectl 只能回答「你问的那个抽屉里有什么」，答不出「脚本自己会去哪个
# 抽屉」。第五十四批的 8 条假-kubectl 用例全绿，却漏掉了默认命名空间写死 `warden-agent`
# 这个真缺陷；第五十八批第一次拿真集群跑，又当场抓到一条：错误路径里写了文档式的
# `$manifest_ns`（反引号在 shell 里是命令替换）→ 屏幕上多一行 `warden: command not found`，
# 而「清单里声明的是 ___」后面是空的 —— 出事时最该看到的值消失了。
#
# ⚠️ 第六十批：这个脚本自己差点成为最贵的那个事故（见下面的「本脚本的安全约定」）。
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
#   用例 6  演练没有碰清单声明的那个（生产）命名空间             → 期望它的 revision/env 一个字节都没变
#
# --------------------------------------------------------------------------- #
# 本脚本的安全约定（第六十批加的，之前没有，而代价是一次「删掉生产命名空间」）
#
# 事实：`DRILL_NS` 过去只是个变量，谁都能传。用 `DRILL_NS=warden`（清单声明的那个）跑，
# 脚本会在生产命名空间里 `set env deploy/warden-agent WARDEN_DRILL_MARK=1`（改生产负载模板！），
# 最后无条件执行 `kubectl delete ns warden --wait=false` —— 本机 kind 里那个命名空间已经
# 跑了 11 天，里面有 postgres 与其数据。而且它**失败时也会删**（退出码 1 只是"有用例不符合契约"）。
# 复现方式（不碰真集群）见 CHANGELOG 第五十八/六十批：把假 docker 放进 PATH 记录 argv，
# 就能看到那条 `delete ns warden` 真的被下发。
#
# 所以现在：
#   1) `DRILL_NS` 名字里必须出现 `drill`（"名字自己得说清它是什么"），且**绝不等于**清单声明的命名空间；
#   2) 演练**不再碰**清单声明的命名空间：节点里那份 `namespace.yaml` 会被改写成演练命名空间，
#      于是「不传 NAMESPACE 时从清单读」这条契约仍然被测到（读的是一份真清单），
#      但被回滚的是演练自己的 Deployment；
#   3) 只删除**本次演练自己创建**的命名空间（`CREATED_NS`）；已存在的、且没有 `warden-drill=true`
#      标签的命名空间直接拒绝执行；
#   4) 清理放在 `trap ... EXIT` 里（Ctrl-C、超时、中途失败都会走），且**清理失败会把结论翻成失败**；
#   5) 最后一条用例专门核对生产命名空间的 revision/env 没变。
#
# 它**不**证明的事（别把结论说大）：不验业务是否真的恢复、不在 CI 里跑、单节点，
# 也不覆盖 `deploy/observability` 的 CR 有没有被 apply。
# --------------------------------------------------------------------------- #

set -uo pipefail

NODE="${K8S_NODE:-warden-control-plane}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
DRILL_NS="${DRILL_NS:-warden-drill}"
DEPLOYMENT="${DEPLOYMENT:-warden-agent}"
KUBECONFIG_IN_NODE="/etc/kubernetes/admin.conf"
WORKDIR="/tmp/k8s-rollback-drill"
FAILED=0
CREATED_NS=0
PROD_SNAPSHOT=""
PROD_SNAPSHOT_TAKEN=0

say() { printf '%s\n' "[drill] $*"; }
bad() { FAILED=$((FAILED + 1)); printf '%s\n' "[drill] ✗ $*"; }
good() { printf '%s\n' "[drill] ✓ $*"; }

K() { docker exec "$NODE" kubectl --kubeconfig="$KUBECONFIG_IN_NODE" "$@"; }
SCRIPT() { docker exec -e KUBECONFIG="$KUBECONFIG_IN_NODE" "$@" "$NODE" bash "$WORKDIR/repo/scripts/rollback.sh"; }

expect_rc() {
    # expect_rc <期望退出码> <实际退出码> <说明>
    if [ "$1" = "$2" ]; then good "$3（退出码 $2）"; else bad "$3：期望退出码 $1，实际 $2"; fi
}

namespace_in_file() {
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
    ' "$1"
}

namespace_in_manifest() { namespace_in_file "$REPO_ROOT/deploy/k8s/namespace.yaml"; }

# --------------------------------------------------------------------------- #
# 守卫 1/2：演练命名空间的名字必须自己说清它是什么，且绝不能是清单声明的那个
# --------------------------------------------------------------------------- #
case "$DRILL_NS" in
    *drill*) ;;
    *)
        say "拒绝执行：DRILL_NS=$DRILL_NS 里没有 'drill' 这个词。"
        say "这个脚本会删掉 DRILL_NS 指向的命名空间，所以它的名字必须自己说清它是演练用的。"
        exit 1
        ;;
esac

case "$DRILL_NS" in
    ""|default|kube-system|kube-public|kube-node-lease)
        say "拒绝执行：DRILL_NS=$DRILL_NS 是系统命名空间（本演练会删掉它指向的命名空间）"
        exit 1
        ;;
esac

command -v docker >/dev/null 2>&1 || { say "没有 docker，无法演练"; exit 1; }
docker inspect "$NODE" >/dev/null 2>&1 || { say "找不到节点容器 $NODE（用 K8S_NODE= 指定）"; exit 1; }
K get ns >/dev/null 2>&1 || { say "节点里的 kubectl 连不上 API（集群没起？）"; exit 1; }

NS="$(namespace_in_manifest)"
[ -n "$NS" ] || { say "读不出清单里的命名空间"; exit 1; }
say "清单声明的命名空间 = $NS（**本演练不会碰它**）"
say "演练用命名空间 = $DRILL_NS"

if [ "$DRILL_NS" = "$NS" ]; then
    say "拒绝执行：DRILL_NS 就是清单声明的命名空间（$NS）。"
    say "本脚本最后会删掉演练命名空间 —— 对生产命名空间来说是灾难性操作。"
    say "换一个名字（默认 $DRILL_NS 即 warden-drill）再跑。"
    exit 1
fi

# --------------------------------------------------------------------------- #
# 守卫 3：已经存在的命名空间必须是我们自己的（否则不删）
# --------------------------------------------------------------------------- #
if K get ns "$DRILL_NS" >/dev/null 2>&1; then
    owner="$(K get ns "$DRILL_NS" -o jsonpath='{.metadata.labels.warden-drill}' 2>/dev/null)"
    if [ "$owner" != "true" ]; then
        say "拒绝执行：命名空间 $DRILL_NS 已经存在，且没有 warden-drill=true 标签。"
        say "它不是本演练创建的，本脚本无权删除它。要么换名字，要么先自己确认后删掉。"
        exit 1
    fi
    say "命名空间 $DRILL_NS 已存在且带 warden-drill=true 标签 → 复用它，结束时由本演练删除"
fi

# --------------------------------------------------------------------------- #
# 清理：无论怎么退出都走这里；只删自己创建的；清理失败要把结论翻成失败
# --------------------------------------------------------------------------- #
cleanup() {
    rc=$?
    if [ "$CREATED_NS" -eq 1 ]; then
        say "清理演练用命名空间 $DRILL_NS（本次演练创建）"
        if K delete ns "$DRILL_NS" --wait=false >/dev/null 2>&1; then
            good "演练命名空间已下发删除（$DRILL_NS）"
        else
            bad "演练命名空间没删掉：$DRILL_NS —— 它还在集群里，下次同名演练会拒绝执行"
        fi
    fi
    if [ "$PROD_SNAPSHOT_TAKEN" -eq 1 ]; then
        now="$(prod_snapshot)"
        if [ "$now" = "$PROD_SNAPSHOT" ]; then
            good "演练没有碰生产命名空间 $NS（revision/env 与开始前一致）"
        else
            bad "演练改动了生产命名空间 $NS：开始前 [$PROD_SNAPSHOT]，现在是 [$now]"
        fi
    fi
    if [ "$FAILED" -ne 0 ] && [ "$rc" -eq 0 ]; then
        rc=1
    fi
    if [ "$FAILED" -eq 0 ]; then
        say "全部用例符合契约"
    else
        say "$FAILED 条不符合契约"
    fi
    exit "$rc"
}
trap cleanup EXIT

prod_snapshot() {
    printf 'rev=%s env=%s' \
        "$(K -n "$NS" get deploy "$DEPLOYMENT" -o jsonpath='{.metadata.annotations.deployment\.kubernetes\.io/revision}' 2>/dev/null)" \
        "$(K -n "$NS" get deploy "$DEPLOYMENT" -o jsonpath='{.spec.template.spec.containers[0].env[*].name}' 2>/dev/null)"
}

PROD_SNAPSHOT="$(prod_snapshot)"
PROD_SNAPSHOT_TAKEN=1
say "生产命名空间快照：$PROD_SNAPSHOT"

# 演练用的工作负载用**节点上已有的镜像**：生产正在跑的那个镜像一定已经在本节点上，
# 于是整个演练不依赖 registry（本机网络不稳时 `alpine:3.20` 会拉不下来，
# 表现为"回滚已下发但 180s 内没 ready"——那会让用例 1 以「集群真出事」的样子变红，
# 而实际上只是镜像没拉到）。
DRILL_IMAGE="${DRILL_IMAGE:-}"
if [ -z "$DRILL_IMAGE" ]; then
    DRILL_IMAGE="$(K -n "$NS" get deploy "$DEPLOYMENT" -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null)"
fi
if [ -z "$DRILL_IMAGE" ]; then
    say "读不到生产镜像，回退到 alpine（这次演练会依赖 registry）"
    DRILL_IMAGE="docker.m.daocloud.io/library/alpine:3.20"
fi
say "演练用镜像 = $DRILL_IMAGE（优先用生产正在跑的那个：它已经在节点上，不需要拉）"

# --------------------------------------------------------------------------- #
say "把清单与脚本拷进 $NODE:$WORKDIR/repo（保持 <repo>/scripts + <repo>/deploy 的布局：脚本靠自身位置推断仓库根）"
docker exec "$NODE" sh -c "rm -rf $WORKDIR && mkdir -p $WORKDIR/repo" \
    || { say "无法在节点里准备 $WORKDIR"; exit 1; }
( cd "$REPO_ROOT" && tar cf - deploy/k8s scripts/rollback.sh ) \
    | docker exec -i "$NODE" sh -c "tar xf - -C $WORKDIR/repo"
docker exec "$NODE" sh -c "chmod +x $WORKDIR/repo/scripts/rollback.sh"

# 把节点里那份清单的命名空间**改写成演练命名空间**：这样「不传 NAMESPACE 就从清单读」
# 这条契约仍然被测到（读的是一份真清单），但被回滚的是演练自己的 Deployment。
sed_expr="s/^  name: ${NS}\$/  name: ${DRILL_NS}/"
docker exec "$NODE" sed -i "$sed_expr" "$WORKDIR/repo/deploy/k8s/namespace.yaml" \
    || { say "改写节点里的 namespace.yaml 失败"; exit 1; }
# 核对改动真生效：**用读节点上那份文件的原文**，不靠"sed 应该成功了"
manifest_copy="${TMPDIR:-/tmp}/drill-namespace-$$.yaml"
docker exec "$NODE" cat "$WORKDIR/repo/deploy/k8s/namespace.yaml" > "$manifest_copy" 2>/dev/null
manifest_ns_in_node="$(namespace_in_file "$manifest_copy")"
rm -f "$manifest_copy"
if [ "$manifest_ns_in_node" != "$DRILL_NS" ]; then
    say "拒绝继续：节点里的清单仍然声明 $manifest_ns_in_node（期望 $DRILL_NS）——"
    say "改写没生效时它会去动生产命名空间，这是本脚本绝不能发生的事"
    exit 1
fi
good "节点里的清单已指向演练命名空间（$manifest_ns_in_node）"

# --------------------------------------------------------------------------- #
say "准备演练用命名空间 $DRILL_NS 与一个单副本 Deployment"
if K get ns "$DRILL_NS" >/dev/null 2>&1; then
    say "命名空间已存在（前面已验证它是本演练的），直接复用"
else
    if K create ns "$DRILL_NS" >/dev/null 2>&1; then
        CREATED_NS=1
        K label ns "$DRILL_NS" warden-drill=true --overwrite >/dev/null 2>&1 \
            || say "注意：没能给命名空间打上 warden-drill=true 标签（下次同名演练会拒绝执行）"
        good "已创建演练命名空间 $DRILL_NS（带 warden-drill=true 标签）"
    else
        say "创建演练命名空间失败：$DRILL_NS";
        exit 1
    fi
fi

K -n "$DRILL_NS" create deployment "$DEPLOYMENT" \
    --image="$DRILL_IMAGE" -- \
    /bin/sh -c 'while true; do sleep 3600; done' >/dev/null 2>&1

# --------------------------------------------------------------------------- #
say "用例 1：造一个新 revision，再**不传 NAMESPACE** 回滚（期望 0，且恢复成原来的 Pod 模板）"
before_rev="$(K -n "$DRILL_NS" get deploy "$DEPLOYMENT" -o jsonpath='{.metadata.annotations.deployment\.kubernetes\.io/revision}')"
before_env="$(K -n "$DRILL_NS" get deploy "$DEPLOYMENT" -o jsonpath='{.spec.template.spec.containers[0].env[*].name}')"
K -n "$DRILL_NS" set env "deploy/$DEPLOYMENT" WARDEN_DRILL_MARK=1 >/dev/null 2>&1
K -n "$DRILL_NS" rollout status "deploy/$DEPLOYMENT" --timeout=180s >/dev/null 2>&1
bumped_env="$(K -n "$DRILL_NS" get deploy "$DEPLOYMENT" -o jsonpath='{.spec.template.spec.containers[0].env[*].name}')"
[ "$bumped_env" = "WARDEN_DRILL_MARK" ] \
    && good "新 revision 已就位（env=$bumped_env，原 revision=$before_rev）" \
    || bad "没能造出新 revision（env=$bumped_env）"

out="$(SCRIPT 2>&1)"; rc=$?
printf '%s\n' "$out" | sed 's/^/        /'
expect_rc 0 "$rc" "真集群回滚"
after_env="$(K -n "$DRILL_NS" get deploy "$DEPLOYMENT" -o jsonpath='{.spec.template.spec.containers[0].env[*].name}')"
after_rev="$(K -n "$DRILL_NS" get deploy "$DEPLOYMENT" -o jsonpath='{.metadata.annotations.deployment\.kubernetes\.io/revision}')"
[ -z "$after_env" ] && good "Pod 模板已还原（演练用的 env 不在了，revision=$after_rev）" \
                    || bad "Pod 模板没还原（env=$after_env）"
printf '%s' "$out" | grep -q "namespace=$DRILL_NS" && good "命名空间是从清单读出来的（$DRILL_NS）" \
                                             || bad "没说清用的是哪个命名空间"
printf '%s' "$out" | grep -q "command not found" && bad "脚本里有 shell 错误（command not found）" \
                                                  || good "没有 shell 错误"

# --------------------------------------------------------------------------- #
say "用例 2：只有 1 个 revision 的 Deployment（期望 2：确实没有历史）"
K -n "$DRILL_NS" create deployment "${DEPLOYMENT}-single" \
    --image="$DRILL_IMAGE" -- \
    /bin/sh -c 'while true; do sleep 3600; done' >/dev/null 2>&1
out="$(docker exec -e KUBECONFIG="$KUBECONFIG_IN_NODE" -e NAMESPACE="$DRILL_NS" -e DEPLOYMENT="${DEPLOYMENT}-single" \
    "$NODE" bash "$WORKDIR/repo/scripts/rollback.sh" 2>&1)"; rc=$?
printf '%s\n' "$out" | sed 's/^/        /'
expect_rc 2 "$rc" "没有历史版本"
printf '%s' "$out" | grep -q "未做任何改动" && good "明确说了没做任何改动" || bad "没说「未做任何改动」"

# --------------------------------------------------------------------------- #
say "用例 3：命名空间打错（期望 1 + kubectl 原话 + 清单里声明的那个命名空间）"
out="$(SCRIPT -e NAMESPACE="${DRILL_NS}-nope" 2>&1)"; rc=$?
printf '%s\n' "$out" | sed 's/^/        /'
expect_rc 1 "$rc" "看不到 Deployment"
printf '%s' "$out" | grep -q "NotFound" && good "带了 kubectl 的原话" || bad "没带 kubectl 原话"
printf '%s' "$out" | grep -qE "清单里声明的是[[:space:]]*${DRILL_NS}" \
    && good "写出了清单里声明的命名空间（$DRILL_NS）" \
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
rev_before="$(K -n "$DRILL_NS" get deploy "$DEPLOYMENT" -o jsonpath='{.metadata.annotations.deployment\.kubernetes\.io/revision}')"
out="$(SCRIPT -e DRY_RUN=1 -e NAMESPACE="$DRILL_NS" 2>&1)"; rc=$?
printf '%s\n' "$out" | sed 's/^/        /'
expect_rc 0 "$rc" "dry-run"
rev_after="$(K -n "$DRILL_NS" get deploy "$DEPLOYMENT" -o jsonpath='{.metadata.annotations.deployment\.kubernetes\.io/revision}')"
[ "$rev_before" = "$rev_after" ] && good "dry-run 没有改集群（revision 仍是 $rev_after）" \
                                || bad "dry-run 改了集群（$rev_before → $rev_after）"

# --------------------------------------------------------------------------- #
say "用例 6：生产命名空间没被动过（期望 revision/env 与开始前一致）"
now="$(prod_snapshot)"
if [ "$now" = "$PROD_SNAPSHOT" ]; then
    good "生产命名空间 $NS 未被改动（$now）"
else
    bad "生产命名空间 $NS 被改动了：开始前 [$PROD_SNAPSHOT]，现在 [$now]"
fi

# 结论与清理都在 cleanup（trap EXIT）里，保证中途失败也走同一条路径
exit "$([ "$FAILED" -eq 0 ] && echo 0 || echo 1)"
