#!/usr/bin/env bash
# 变异测试门禁（mutation testing）：证明"测试全绿"不等于"测试有效"
#
# 干什么：把源码里的比较符、边界、布尔、返回值逐个改掉，再跑一遍测试——
#   测试变红 → 突变体被"杀死"：说明真有测试在盯着这段逻辑；
#   测试仍绿 → 突变体"存活"：说明这段逻辑**没有任何测试约束它**。
# 覆盖率只能回答"这行代码跑过没有"；变异测试才回答"这行代码改错了会不会被发现"。
#
# 分片：变异测试是**逐个突变体**跑测试的，一次全量测试约 5 分钟 ⇒ 串行整仓是小时级。
# 所以把要变异的东西按"安全关键 + 体量小 + 逻辑密集"分成若干片，每片只跑覆盖到它的测试文件。
# 分片表（哪一片、变谁、跑哪些测试、门槛多少、实测基线）在 scripts/mutation_shards.py，
# 那是**唯一来源**；本脚本跑之前会按它把 pyproject.toml 的 [tool.mutmut] 重写成对应分片，
# 退出时（含异常退出）一定还原成默认分片，避免留下脏工作区。
#
# 用法：
#   bash scripts/mutation.sh                      # 默认分片 core-security
#   bash scripts/mutation.sh --shard=concurrency   # 只跑某一片
#   bash scripts/mutation.sh --all-shards          # 串行跑全部（CI 用 matrix 并行跑，不这样跑）
#   bash scripts/mutation.sh --docker --shard=ratelimit   # 在容器里跑（见下）
#   ./.venv/Scripts/python.exe scripts/mutation_shards.py list      # 看分片表
#   ./.venv/Scripts/python.exe scripts/mutation_shards.py changed   # 只挑改动涉及的分片
#
# 在哪儿跑：
#   * CI：.github/workflows/ci.yml 的 mutation job（matrix 一片一个 job；夜间 + 手动触发，不占 PR 的时间）
#   * 本地 Windows：mutmut 3 在原生 Windows 上**拒跑**（其 issue #397 要求 WSL），
#     所以用容器：`bash scripts/mutation.sh --docker [--shard=xxx]`
#     （容器里用 `uv sync --frozen --extra dev` 从**仓库自己的 uv.lock** 装依赖：
#       手写包清单会漂移——2026-09-30 就漂了一次，见下面 docker 分支里的注释）
#
# 门槛口径见 scripts/mutation_gate.py。注意本脚本**故意不用 `set -e`**：
# mutmut 在有存活突变体时退出码非 0 是预期情况，达标与否交给分数门禁判断，
# 否则"存活 1 个但分数远高于门槛"也会让流水线红，反而看不出真实严重程度。
set -uo pipefail

cd "$(dirname "$0")/.."

SHARD=""
ALL_SHARDS=0
USE_DOCKER=0

usage() {
    sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --shard) SHARD="${2:-}"; shift 2 ;;
        --shard=*) SHARD="${1#--shard=}"; shift ;;
        --all-shards) ALL_SHARDS=1; shift ;;
        --docker) USE_DOCKER=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "未知参数：$1（用 --help 看用法）" >&2; exit 2 ;;
    esac
done

PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then
    echo "找不到 python3/python，无法做分数门禁" >&2
    exit 2
fi

if [ "$USE_DOCKER" = 1 ]; then
    # Git Bash 会把容器内路径 /repo 改写成 C:/Program Files/Git/repo，所以：
    #   * 挂载点用 `pwd -W` 拿 Windows 路径
    #   * 关掉 MSYS 路径转换
    ROOT="$(pwd -W 2>/dev/null || pwd)"
    INNER="bash scripts/mutation.sh"
    [ "$ALL_SHARDS" = 1 ] && INNER="$INNER --all-shards"
    [ -n "$SHARD" ] && INNER="$INNER --shard=$SHARD"
    # 容器名带上分片：跑不同分片时不会互撞（之前固定叫 warden-mutmut，
    # 上次被中断的容器没清掉，下一次就直接 Conflict 而连片都跑不起来）。
    #
    # 依赖必须**从仓库自己的 uv.lock 装**，不能手写一张包清单：
    #   手写的清单会漂移。2026-09-30 就漂了一次——第 10 批给本地加了 pytest-timeout，
    #   容器清单没跟上，而 pyproject 的 `filterwarnings = ["error"]` 会把
    #   「Unknown config option: timeout」这个警告升级成 pytest 的 INTERNALERROR，
    #   于是变异测试在"收集统计"阶段直接死掉（runner returned 3，看着像 mutmut 坏了）。
    #   用 uv sync --frozen 就不可能再漂：容器里的依赖 = 仓库里被锁定的那一套。
    #   uv 的下载缓存挂成具名卷，第二次之后就不用重新下（首次冷启动约 2~3 分钟）。
    exec env MSYS_NO_PATHCONV=1 docker run --rm --name "warden-mutmut${SHARD:+-$SHARD}" \
        -v "$ROOT:/repo" -w /repo -e TERM=dumb -e COLUMNS=200 \
        -v warden-uv-cache:/root/.cache/uv \
        python:3.12-slim bash -lc "
            set -e
            pip install -q uv
            export UV_PROJECT_ENVIRONMENT=/tmp/venv
            uv sync --frozen --extra dev --quiet
            export PATH=/tmp/venv/bin:\$PATH
            cd /repo
            $INNER
        "
fi

if ! command -v mutmut >/dev/null 2>&1; then
    echo "缺 mutmut：先 uv sync --frozen --extra dev（或 pip install -e '.[dev]'）" >&2
    exit 2
fi

# 无论怎么退出（包括中途被 Ctrl-C、被测用例把脚本搞挂）都要把配置还原成默认分片，
# 否则 pyproject.toml 会带着"上一个分片"的状态留在工作区里，下次提交就带进仓库了。
restore_config() {
    "$PY" scripts/mutation_shards.py render core-security >/dev/null 2>&1 || true
}
trap restore_config EXIT

# 该分片的门槛：表里的实测基线 − 2 个点；尚未实测的分片返回 none（只报告不判红）。
# 允许用环境变量 MUTATION_MIN_SCORE 覆盖（实验中想临时收紧/放宽时用）。
threshold_for() {
    local shard="$1" value
    if [ -n "${MUTATION_MIN_SCORE:-}" ]; then
        echo "$MUTATION_MIN_SCORE"
        return
    fi
    value="$("$PY" scripts/mutation_shards.py min-score "$shard")" || return 2
    echo "$value"
}

# 清掉上一次的变异状态。**这不是洁癖，是防止另一种假绿**：
# mutmut 把"这个突变体已经评估过了"记在 mutants/ 下的状态文件里（还把它拷过去的项目影子副本留在那儿）。
# 不清掉的话，换分片跑时它会认为"没什么要跑的"，然后 export 出**上一个分片的统计**——
# 实测撞到过：concurrency 与 ratelimit 两片都报 242/251（= core-security 的数字），
# 而 mutants/mutmut-results-*.txt 是 0 字节。三个分片报同一个分数，一眼就该看出不对。
reset_mutation_state() {
    rm -rf mutants/mutmut-stats.json mutants/mutmut-cicd-stats.json \
           mutants/src mutants/tests mutants/pyproject.toml mutants/uv.lock
}

# 统计文件的修改时间：跑之前记一下，跑完对比，用来证伪"我看到的分数是本次跑出来的"。
stats_stamp() {
    stat -c %Y mutants/mutmut-cicd-stats.json 2>/dev/null || echo 0
}

run_shard() {
    local shard="$1" min stamp_before stamp_after total

    echo
    echo "==================================================================="
    echo "== 分片：$shard"
    echo "==================================================================="

    # 把 [tool.mutmut] 换成这一片（幂等：本来就是它时不写文件）
    "$PY" scripts/mutation_shards.py render "$shard" || return 2

    min="$(threshold_for "$shard")" || return 2
    if [ "$min" = "none" ]; then
        # 没有实测基线就不判红：宁可"报告但先不拦"，也不要拿一个编出来的门槛去卡人
        # （编出来的门槛会被人关掉，而被关掉的门禁等于没有门禁）。
        echo "！！ 该分片尚无实测基线：本次只报告分数、不判红（见 docs/mutation.md 第七节）"
        min=0.0
    fi
    echo "本片门槛：$min"

    reset_mutation_state
    stamp_before="$(stats_stamp)"

    # mutmut 在"存在存活突变体"时退出码非 0，这里不当场失败，交给下面的分数门禁
    mutmut run || true

    echo
    echo '== 存活 / 超时 / 无测试的突变体（这份清单就是"看起来测了、其实没测住"的地方）=='
    mkdir -p mutants
    # tee 两份：文件给"事后看得见"（mutants/ 已在 .gitignore 里），stdout 给人当下看
    mutmut results | tee "mutants/mutmut-results-$shard.txt"

    # GitHub 的 job summary：门禁红的时候，人第一眼要看到的应该是"哪段逻辑没被测住"，
    # 而不是去翻几千行日志。这里就是写一个文件，不需要额外的第三方 action。
    if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
        {
            echo "## 变异测试（分片 $shard）：存活 / 超时 / 无测试的突变体"
            echo
            echo '下面每一条都是「代码改坏了，但没有任何测试报警」——要么补测，要么写成注释说明它为何语义等价。'
            echo
            echo '```'
            cat "mutants/mutmut-results-$shard.txt"
            echo '```'
        } >> "$GITHUB_STEP_SUMMARY"
    fi

    echo
    echo "== 分数门禁（分片 $shard）=="
    mutmut export-cicd-stats

    # 反假绿：统计文件必须是**本次**新写的。否则"看到了分数"其实是看到了上一次的产物——
    # 那种情况下门禁会拿着别人的成绩单放行，比没有门禁更危险。
    stamp_after="$(stats_stamp)"
    if [ "$stamp_after" = "$stamp_before" ]; then
        echo "！！ 统计文件未被本次运行重写 ⇒ 结果不可信（拿到的是旧数字）" >&2
        return 2
    fi
    total="$("$PY" -c "import json,sys;print(json.load(open('mutants/mutmut-cicd-stats.json')).get('total','?'))" 2>/dev/null || echo '?')"
    echo "本次评估的突变体数：$total"

    # 门禁自带退出码区分（0 过 / 1 分数不够 / 2 没产出统计文件 / 3 文件读不出来 / 4 配置空转），
    # 这里直接透传它的退出码，不再包一层判断。
    "$PY" scripts/mutation_gate.py "$min"
}

if [ "$ALL_SHARDS" = 1 ]; then
    # 串行跑全部：本地核查用。CI 里用 matrix 并行（一片一个 job），比这里快得多。
    failure=0
    while read -r name; do
        run_shard "$name" || failure=$?
    done < <("$PY" scripts/mutation_shards.py list | awk '{print $1}')
    exit "$failure"
fi

run_shard "${SHARD:-core-security}"
