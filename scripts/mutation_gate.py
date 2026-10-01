"""变异测试的分数门禁（被 `scripts/mutation.sh` 调用，也被单测直接调用）。

为什么要单独一个文件而不是在 shell 里写一段 python -c：
  "分数够了没有"是个**会改变 CI 结论的决策**，和审批决策、回滚判断是同一类东西——
  它必须能被单测固定住（见 `tests/test_mutation_gate.py` 的固定 JSON 夹具），
  而不是藏在流水线的一段内联字符串里，改错了没人知道。

分数的定义（保守取向）：
    分母 = total - skipped - no_tests   （只算"本来能被杀"的突变体）
    分子 = killed                        （timeout 不算分子）
  为什么 timeout 不进分子：突变体让测试挂住通常是"被发现了"，但也可能是机器慢/环境问题。
  把它算成"杀死"会用一个不确定的信号抬高分数——宁可让分数低一点、看得见。

退出码（CI 靠它决定红绿，所以每个码只对应一件事）：
    0 = 达标
    1 = 分数低于门槛
    2 = 用法错 / 统计文件不存在（**没跑出结果 ≠ 通过**）
    3 = 统计文件读不出来（不是 JSON 对象 / JSON 语法错）
    4 = 可评估突变体为 0（[tool.mutmut] 配置指向了不存在的文件）
    5 = 突变体一个都没跑完（total>0 但 killed/survived/timeout 全为 0）——
        实测撞到过：mutmut 先"生成突变体"、再跑**一次基线测试**收集统计，
        基线里只要有一条测试失败就整个中止，于是 206 个突变体全部停在 "not checked"，
        export-cicd-stats 导出 total=206 而计数全 0。
        这**不是分数问题**：算成 0.0% 会让人去补测试（方向全错），
        而且分片尚无实测基线时 0.0 >= 门槛 0.0 还会**直接放行**（存在过的假绿）。
        要区分开：先去把基线测试跑绿（`--shard=<片> --deselect` 或直接修那条测试），
        再重新跑变异。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_STATS_PATH = Path("mutants/mutmut-cicd-stats.json")

# 退出码用常量而不是散落的魔法数字：CI 与单测都按名字对，改错一个数字不会静默错位
OK = 0
BELOW_THRESHOLD = 1
USAGE_ERROR = 2
BAD_STATS = 3
EMPTY_MUTANTS = 4
NOTHING_RAN = 5


@dataclass(frozen=True)
class GateResult:
    """门禁结论：原始计数 + 算出来的分数 + 是否达标。"""

    killed: int
    survived: int
    timeout: int
    no_tests: int
    skipped: int
    total: int
    tested: int
    score: float
    min_score: float

    @property
    def ran(self) -> bool:
        """突变体到底有没有真的跑过一轮。

        `total > 0` 但 `killed + survived + timeout` 全为 0，只有一种解释：mutmut
        **一个突变体都没执行完**（最常见的是基线测试失败导致整体中止，见模块 docstring 的
        退出码 5）。
        这个区分很关键：把"没跑"当成"0 分"不仅会指错方向（维护者去补测试），
        在没有基线的分片上还会因为 `0.0 >= 0.0` 而**悄悄通过**。
        """
        return self.total == 0 or (self.killed + self.survived + self.timeout) > 0

    @property
    def passed(self) -> bool:
        return self.tested > 0 and self.score >= self.min_score

    def render(self) -> str:
        return (
            f"killed={self.killed} survived={self.survived} timeout={self.timeout} "
            f"no_tests={self.no_tests} skipped={self.skipped} total={self.total}\n"
            f"mutation score = {self.killed}/{self.tested} = {self.score:.1f}% "
            f"(门槛 {self.min_score:.1f}%)"
        )


def evaluate(stats: dict[str, object], min_score: float) -> GateResult:
    """把 mutmut 导出的 stats 字典算成门禁结论（纯函数，好测）。"""

    def as_int(key: str) -> int:
        value = stats.get(key, 0)
        if not isinstance(value, int | float | str):
            raise ValueError(f"stats 里的 {key} 不是数字: {value!r}")
        return int(value)

    killed = as_int("killed")
    total = as_int("total")
    skipped = as_int("skipped")
    no_tests = as_int("no_tests")
    tested = total - skipped - no_tests
    score = 0.0 if tested <= 0 else killed / tested * 100
    return GateResult(
        killed=killed,
        survived=as_int("survived"),
        timeout=as_int("timeout"),
        no_tests=no_tests,
        skipped=skipped,
        total=total,
        tested=tested,
        score=score,
        min_score=min_score,
    )


def load_stats(path: Path = DEFAULT_STATS_PATH) -> dict[str, object]:
    if not path.exists():
        raise FileNotFoundError(
            f"没有找到 {path}——先跑 mutmut run && mutmut export-cicd-stats"
            "（scripts/mutation.sh 会做）"
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} 的内容不是 JSON 对象")
    return data


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("用法: mutation_gate.py <最低分> [stats 路径]", file=sys.stderr)
        return USAGE_ERROR
    try:
        min_score = float(args[0])
    except ValueError:
        print(f"最低分不是数字: {args[0]!r}", file=sys.stderr)
        return USAGE_ERROR
    path = Path(args[1]) if len(args) > 1 else DEFAULT_STATS_PATH

    # 读不出来必须**区别于"分数不够"**：一个缺文件/格式错的流水线看起来像"没过门禁"，
    # 维护者会去补测试——完全找错方向。给不同退出码，日志里一眼看得出是哪类问题。
    try:
        stats = load_stats(path)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return USAGE_ERROR
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"统计文件读不出来：{exc}", file=sys.stderr)
        return BAD_STATS

    result = evaluate(stats, min_score)
    print(result.render())
    if result.tested <= 0:
        # 0 个可评估突变体 = 配置写错了（source_paths/only_mutate 指向不存在的文件），
        # 不能当成"通过"——那会让门禁在配置坏掉时静默变成空转。
        print("可评估突变体为 0：检查 [tool.mutmut] 的 source_paths / only_mutate 是否指向真实文件")
        return EMPTY_MUTANTS
    if not result.ran:
        # 注意顺序：这一条在 passed 之前判——0.0 分且门槛也是 0.0 时 passed 会为真，
        # 必须先把"根本没跑"拦下来，否则就是假绿。
        print(
            f"突变体一个都没跑完（total={result.total} 但 killed/survived/timeout 全为 0）：\n"
            "  先单独把这一片的基线测试跑绿："
            f"  python scripts/mutation_shards.py render --shard=<片名> 后 "
            "pytest <片里的测试文件>\n"
            "  mutmut 在跑突变体前会先跑一次测试收统计，那次失败会让整轮中止——"
            "此时报的不是分数问题，别去补测试。",
            file=sys.stderr,
        )
        return NOTHING_RAN
    if not result.passed:
        print('低于门槛：看上面的"存活/超时/无测试"清单，补测试或说明为什么这段逻辑没法测')
        return BELOW_THRESHOLD
    print("通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
