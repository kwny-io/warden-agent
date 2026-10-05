#!/usr/bin/env bash
# 反向对照：证明 gitleaks 的 allowlist 没有把扫描器**关掉**。
#
# 为什么需要它：allowlist 是「让扫描器闭嘴」的开关，写错方向就是**静默失守**——那比红灯糟。
#
# 判据用**退出码**（gitleaks：1=有命中，0=无命中），不要用 grep 输出文本：
#   本步骤第一版写的是 `grep -q "leaks found"`，而 "no leaks found" **也含**这个子串，
#   实测两种情形都匹配 → 这条对照**永真**、根本没在验（典型的假绿）。
#
# 对照值刻意与被放宽的夹具**同形状、不同值**（`sk-live-…`，见 .gitleaks.toml 顶部）：
#   (a) 扫描器被整体关掉（catch-all allowlist / 空规则集）→ 对照也消失 → 失败；
#   (b) allowlist 放宽过度（把 `sk-live-` 整族或 `TOPSECRET` 这种子串放宽）→ 同样照出来。
# 写死字面量而不现场随机：随机值可能碰巧低熵、掉到规则的熵阈值以下 → 偶发假红。
# 它只出现在临时目录里（不进仓库），所以不会污染主扫描。
#
# ② 是**负面对照自检**：配一份 catch-all allowlist 再跑，对照**必须消失**。
#   这一步专门拦「对照其实没喂到扫描器」（例如挂载失败、扫了空目录）——那种情况下
#   ① 的「通过」毫无意义，而 ② 会红。
#
# 用法：bash scripts/gitleaks_reverse_control.sh [config路径]（默认仓内 .gitleaks.toml）
# 退出码：0=全部通过；1=对照失败（扫描器被关掉/放宽过度/对照没喂到）
set -uo pipefail

# Windows 上两件事必须**成对**做，否则路径会互相打架：
#   1) 关掉 MSYS 的自动路径转换（不关的话，容器**内部**的 `/cfg.toml` 会被转成
#      `C:/Program Files/Git/cfg.toml` —— 实测报 `open … no such file or directory`）；
#   2) 自动转换关了之后，**宿主**路径就得自己用 cygpath 换成 Windows 形式（见 topath）。
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${1:-$REPO_ROOT/.gitleaks.toml}"
# 第六十三批起钉住 digest，且**必须与 .github/workflows/ci.yml 里门禁那一步完全一致**：
# 反向对照的全部意义就是证明“扫描器没被关掉”，两个程序不是同一个就什么都证明不了。
# 台账、取法与验证方式见 tests/test_ci_tool_pinning.py 顶部。
IMG="ghcr.io/gitleaks/gitleaks:v8.21.2@sha256:0e99e8821643ea5b235718642b93bb32486af9c8162c8b8731f7cbdc951a7f46"
CONTROL_VALUE='sk-live-TOPSECRET123457'

CTL_DIR="$(mktemp -d)"
trap 'rm -rf "$CTL_DIR"' EXIT
mkdir -p "$CTL_DIR/work"
printf 'api_key = "%s"\n' "$CONTROL_VALUE" >"$CTL_DIR/work/control_fixture.py"

# Windows 上的 docker 不认 MSYS 的 /tmp/... 与 /d/...：它会**静默**挂一个空目录，
# 于是扫描结果永远是 "no leaks found"（看起来像"没命中"，其实是"没看到文件"）。
# 有 cygpath 就换成 Windows 路径（Linux/CI 上没有 cygpath，原样传）。
topath() {
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -w "$1" 2>/dev/null || printf '%s' "$1"
  else
    printf '%s' "$1"
  fi
}

# 跑一次扫描；退出码原样返回（0=无命中，1=有命中）
run_scan() {
  local cfg="$1" cfgarg=() args=(detect --no-git --source /ctl --no-banner --redact)
  [ -n "$cfg" ] && cfgarg=(-v "$(topath "$cfg"):/cfg.toml:ro") && args+=(--config /cfg.toml)
  docker run --rm -v "$(topath "$CTL_DIR/work"):/ctl:ro" ${cfgarg+"${cfgarg[@]}"} "$IMG" "${args[@]}" \
    >"$CTL_DIR/out.txt" 2>&1
}

echo "反向对照：control=$CONTROL_VALUE  config=$CONFIG"

run_scan "$CONFIG"
code=$?
echo "① 主对照：gitleaks exit=$code（期望 1）"
if [ "$code" -ne 1 ]; then
  echo "::error::反向对照失败（exit=$code，期望 1=有命中）——扫描器可能被关掉，或 allowlist 放宽过度；"
  echo "::error::对照都没被报出来，主扫描的「无泄漏」就不可信。"
  tail -6 "$CTL_DIR/out.txt"
  exit 1
fi
echo "   未被放宽的同形状假密钥仍被报出 ✓"

printf '[extend]\nuseDefault = true\n\n[allowlist]\nregexes = [".*"]\n' >"$CTL_DIR/catchall.toml"
run_scan "$CTL_DIR/catchall.toml"
code2=$?
echo "② 负面对照自检：catch-all allowlist 下 gitleaks exit=$code2（期望 0=被吞掉）"
if [ "$code2" -ne 0 ]; then
  echo "::error::负面对照自检失败（exit=$code2，期望 0）——这条对照可能**根本没喂到扫描器**"
  echo "::error::（挂载失败/扫了空目录/镜像没起来），那样 ① 的「通过」没有意义。"
  tail -6 "$CTL_DIR/out.txt"
  exit 1
fi
echo "   配错方向时它确实会消失（说明 ① 真的在验东西）✓"

echo "反向对照全部通过 ✓"
