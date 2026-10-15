#!/usr/bin/env bash
# 配置访问边界：段尾探针（reviewer-facing）
#
# 背景：守卫 `tests/test_config_surface.py::test_只有授权模块能读某个变量` 要求
#   「真实读取某变量的一方」必须与「注册表 src/warden_agent/core/settings.py 的 ENV_SPECS 里
#    登记给谁读（owner/consumers）」一致。
#   这条守卫在 dev 全史上**只红过两个相邻窗口（共 4 个提交）**，且都在紧邻的修复提交里对齐：
#     PG  族：0de45f4、48e92b8          → 修于 1755d02（consumers 补 cli.py）
#     WEB 族：7cf1f2f、093b77f          → 修于 dbde382（server.py 删掉对 WARDEN_WEB_DIST 的重复读取）
#   冻结 HEAD c9a0f475（2026-10-02）晚于以上全部修复提交 ⇒ 在它自己的树上这条守卫是绿的。
#
# 用法（在仓库根跑）：
#   bash scripts/segcheck_config_surface.sh <sha> [<sha>...]     # 逐条判定
#   bash scripts/segcheck_config_surface.sh -f <文件>            # 每行一个 sha（# 与空行忽略）
#   bash scripts/segcheck_config_surface.sh --self-test          # 有牙自检（见下）
# 输出：<sha12> <日期> <GREEN|RED|NO-GUARD|NO-TEST|UNKNOWN|DIRTY-TREE> <主题> （tests/failures/errors=a/b/c）
#       RED 另起行抄出「失败用例名」与「违反项原文」，并把违反的变量名写进 <sha>.viol（供自检比对）
# 退出码：全绿 0；出现红/判不了 1；参数问题 2；--self-test 判错 3
#   （单条判定：0=GREEN 1=RED 2=NO-GUARD/NO-TEST 3=UNKNOWN/DIRTY-TREE）
#
# 设计约束（都是踩过的坑）：
#   * ★每个 sha 在**它自己的新树**上跑，并断言这棵树干净：
#     守卫用 `rglob("*.py")` 扫全树，**未跟踪**的新 .py（上一步实验、被中断的运行、会真改源码的
#     工具如 mutant_check）会被当成本提交的代码扫进去 ⇒ 红/绿都不可信。
#     已跟踪文件不必担心（`checkout -f` 自己会对齐，它确实会删「当前提交有、目标提交没有」的文件）。
#   * `违反` 一栏必须**只取失败断言行**（`^\s*E\s+-\s+WARDEN_...`）。
#     早期扫描脚本把整份 pytest 日志里所有 `WARDEN_*` 提及都算成违反项，于是把守卫文件 docstring
#     里提到的 WARDEN_BASE_URL / WARDEN_API_KEY 等也算了进去，同一棵树于是「多报」成 11 个变量
#     —— 那是假证据，不是树污染（0de45f4 的真违反就是 5 个 WARDEN_PG_*）。
#   * 计数走 --junitxml：本仓 pytest 在 -q 下**不**输出「N passed」摘要行（数点会读错）。
#   * 日志固定 UTF-8（PYTHONIOENCODING/PYTHONUTF8）：否则在 cp936 控制台下 pytest 的中文输出是乱码。
#   * 只读：只用 git worktree 临时检出，判完即删，不动当前工作树。
#   * --self-test 不只验「红/绿判对」，还验「违反项原文精确」：污染的树/提取面太宽能骗过前者，骗不过后者。
#     它的牙已用变异矩阵实跑验证（2026-10-15）：
#       M1/M2 复用同一棵树（M2 连干净断言也删）⇒ 自检仍 PASS，说明「树复用」本身不是多报的来源；
#       M3 期望违反集故意少写一个变量 ⇒ 自检 FAIL（不是无脑过）；
#       M4 把下面「只取失败断言行」换成「grep 整份日志的 WARDEN_*」⇒ 自检 FAIL（拦得住）。
set -u

REPO=$(git rev-parse --show-toplevel 2>/dev/null) || { echo "不在 git 仓库里" >&2; exit 2; }
cd "$REPO" || exit 2
PY="$REPO/.venv/Scripts/python.exe"
[ -x "$PY" ] || PY="python"
GUARD="tests/test_config_surface.py"
export PYTHONIOENCODING=utf-8 PYTHONUTF8=1

LOGDIR="${TMPDIR:-/tmp}/wa-segcheck-logs"
ROOT="$LOGDIR/trees"
mkdir -p "$ROOT"

CUR_WT=""
cleanup() { [ -n "$CUR_WT" ] && git -C "$REPO" worktree remove --force "$CUR_WT" >/dev/null 2>&1; CUR_WT=""; }
trap cleanup EXIT INT TERM

extract() { # $1=xml $2=log $3=violfile → 打印 "tests/failures/errors"；违反变量写进 $3
  "$PY" - "$1" "$2" "$3" <<'PYEOF'
import re, sys, xml.etree.ElementTree as ET
xml, log, out = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    r = ET.parse(xml).getroot()
    s = r if r.tag == 'testsuite' else r.find('testsuite')
    counts = '%s/%s/%s' % (s.get('tests'), s.get('failures'), s.get('errors'))
except Exception:
    counts = '?/?/?'
viol = []
try:
    text = open(log, encoding='utf-8', errors='replace').read()
except Exception:
    text = ''
for line in text.splitlines():
    m = re.match(r'^\s*E\s+-\s+(WARDEN_[A-Z0-9_]+)', line)
    if m and m.group(1) not in viol:
        viol.append(m.group(1))
open(out, 'w', encoding='utf-8', newline='').write(''.join(v + '\n' for v in viol))
print(counts)
PYEOF
}

judge() { # 外层：无论内部怎么返回，判完一定把这棵临时树删掉（否则树会堆在磁盘上、也会脏 git worktree list）
  judge_inner "$@"
  local rc=$?
  cleanup
  return $rc
}

judge_inner() { # $1=sha → 打印一行判定；rc: 0=GREEN 1=RED 2=NO-GUARD/NO-TEST 3=UNKNOWN/DIRTY
  local sha=$1 full short d s log xml viol counts rc W
  if ! full=$(git rev-parse --verify --quiet "$sha^{commit}" 2>/dev/null); then
    printf "%-12s %-10s UNKNOWN      %s\n" "$sha" "-" "不是本仓的提交（或被改写/未 fetch）"
    return 3
  fi
  short=$(git rev-parse --short=12 "$full")
  d=$(git log -1 --format=%ad --date=short "$full")
  s=$(git log -1 --format=%s "$full" | cut -c1-46)
  log="$LOGDIR/$short.log"; xml="$LOGDIR/$short.xml"; viol="$LOGDIR/$short.viol"
  rm -f "$xml" "$viol"

  # ★ 每个 sha 一棵新树
  W="$ROOT/$short"
  rm -rf "$W"; git -C "$REPO" worktree prune >/dev/null 2>&1
  if ! git -C "$REPO" worktree add -q --force --detach "$W" "$full" >/dev/null 2>&1; then
    printf "%-12s %-10s UNKNOWN      %s\n" "$short" "$d" "建不出工作树"
    return 3
  fi
  CUR_WT="$W"
  # 断言：这棵树必须干净；不干净就说明脚本被人改回了「复用同一棵树」的写法
  if [ -n "$(git -C "$W" status --porcelain 2>/dev/null)" ]; then
    printf "%-12s %-10s DIRTY-TREE   %s  （工作树里有残留文件，判定不可信）\n" "$short" "$d" "$s"
    return 3
  fi
  if [ ! -f "$W/$GUARD" ]; then
    printf "%-12s %-10s NO-GUARD     %s\n" "$short" "$d" "$s"
    return 2
  fi

  ( cd "$W" && PYTHONPATH=src "$PY" -m pytest "$GUARD" -q -p no:cacheprovider --tb=short \
      --junitxml="$xml" > "$log" 2>&1 )
  rc=$?
  counts=$(extract "$xml" "$log" "$viol")
  if [ "$rc" -eq 0 ]; then
    printf "%-12s %-10s GREEN        %s  （tests/failures/errors=%s）\n" "$short" "$d" "$s" "$counts"
    return 0
  elif [ "$rc" -eq 5 ]; then
    printf "%-12s %-10s NO-TEST      %s\n" "$short" "$d" "$s"
    return 2
  fi
  printf "%-12s %-10s RED          %s  （tests/failures/errors=%s）\n" "$short" "$d" "$s" "$counts"
  "$PY" - "$log" <<'PYEOF'
import re, sys
lines = open(sys.argv[1], encoding='utf-8', errors='replace').read().splitlines()
for f in [l.strip() for l in lines if l.startswith('FAILED')][:4]:
    print('    失败：' + f[:120])
for v in [re.sub(r'^E\s*-\s*', '', l).strip() for l in lines if re.match(r'^E\s+-\s', l)][:6]:
    print('    违反：' + v[:160])
PYEOF
  [ -s "$viol" ] && echo "    违反变量：$(tr '\n' ' ' < "$viol")（原文：$log）"
  return 1
}

expect_red() { # $1=sha $2=期望违反变量（空格分隔，顺序无关）→ 判红 + 违反项必须精确相等
  judge "$1" >/dev/null; local rc=$? got want
  [ "$rc" -eq 1 ] || { echo "  ✗ $1 应判 RED，实得 rc=$rc"; return 1; }
  want=$(printf '%s\n' $2 | sort | tr '\n' ' ')
  got=$(tr -d '\r' < "$LOGDIR/$(git rev-parse --short=12 "$1").viol" | sort | tr '\n' ' ')
  if [ "$got" != "$want" ]; then
    echo "  ✗ $1 违反项不精确：期望 [$want] 实得 [$got]（实得里多出来的，通常是提取面太宽：把整份日志里的 WARDEN_* 提及也算成了违反项）"
    return 1
  fi
  echo "  ✓ $1 RED 且违反项精确：$got"
  return 0
}

if [ "${1:-}" = "--self-test" ]; then
  echo "== 有牙自检 =="
  echo "-- A. 4 个已知红：判红 + 违反项必须精确 --"
  bad=0
  PG="WARDEN_PG_DB WARDEN_PG_HOST WARDEN_PG_PASSWORD WARDEN_PG_PORT WARDEN_PG_USER"
  for c in 0de45f4 48e92b8; do expect_red "$c" "$PG" || bad=1; done
  for c in 7cf1f2f 093b77f; do expect_red "$c" "WARDEN_WEB_DIST" || bad=1; done
  echo "-- B. 4 个已知绿：必须判绿 --"
  for c in 1755d02 dbde382 17cd5e5; do
    judge "$c" >/dev/null; [ $? -eq 0 ] || { echo "  ✗ $c 应判 GREEN"; bad=1; }
  done
  judge "$(git rev-parse HEAD)" >/dev/null; [ $? -eq 0 ] || { echo "  ✗ 当前 HEAD 应判 GREEN"; bad=1; }
  echo "-- C. 不存在的 sha 必须判 UNKNOWN(rc=3)，不能默认成绿 --"
  judge deadbeef1 >/dev/null; ec=$?; [ "$ec" -eq 3 ] || { echo "  ✗ 实得 rc=$ec"; bad=1; }
  if [ "$bad" -eq 0 ]; then echo "SELF-TEST PASS：4 红（含违反项精确）+ 4 绿 + UNKNOWN 全部判对"; exit 0; fi
  echo "SELF-TEST FAIL（探针漏判）"; exit 3
fi

if [ "${1:-}" = "-f" ]; then
  f="${2:-}"
  [ -n "$f" ] && [ -f "$f" ] || { echo "用法: $0 -f <文件，每行一个 sha>" >&2; exit 2; }
  shas=$(grep -vE '^[[:space:]]*(#|$)' "$f" | tr -d '\r')
elif [ -n "${1:-}" ]; then
  shas="$*"
else
  echo "用法: $0 <sha>... | $0 -f <文件> | $0 --self-test" >&2
  exit 2
fi

rc_all=0
for sha in $shas; do
  judge "$sha"; rc=$?
  if [ "$rc" -eq 1 ] || [ "$rc" -eq 3 ]; then rc_all=1; fi
done
exit $rc_all
