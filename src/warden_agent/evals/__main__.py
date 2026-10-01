"""CLI 入口：python -m warden_agent.evals —— 跑评测集并打印报告。

两种模式（刻意做成一件事的两个不同问题，别混）：

  · `--mode scripted`（默认，**确定性**）：意图 / 技能 / 端到端三类，模型是脚本化的。
    它测的是 **harness（循环本身）**——失败自愈、防打转、策略门禁、迭代上限。可以当硬门禁，
    因为它不依赖网络也不看运气。
  · `--mode real`（需要模型凭据）：额外跑 `quality` 类——真实模型的提示注入抵抗、
    指令遵循、幻觉抵抗、工具选择。真实模型是概率性的，所以阈值按类别给
    （默认 0.6，可用 `--min-pass-rate` 调）。

退出码：0 全部达标 / 1 有类别未达标 / 2 环境或参数问题（含 `--mode real` 却没凭据）。
`--mode real` 在没凭据时**直接失败**，不会退回假模型——否则报告上的"模型质量"
其实是自己写的剧本，比不跑更糟。
"""

from __future__ import annotations

import argparse
import sys

from warden_agent.evals.real_model import (
    MissingModelCredentials,
    build_real_model,
    format_quality_detail,
)
from warden_agent.evals.runner import THRESHOLDS, EvalReport, format_report, run_all


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m warden_agent.evals",
        description="跑 Agent 评测集（脚本化 harness 门禁 / 真实模型质量门禁）",
    )
    parser.add_argument(
        "--mode", choices=("scripted", "real"), default="scripted",
        help="scripted=确定性 harness 门禁（默认）；real=额外跑真实模型质量用例",
    )
    parser.add_argument(
        "--min-pass-rate", type=float, default=None,
        help=f"quality 类别的通过率阈值（默认 {THRESHOLDS['quality']}）",
    )
    parser.add_argument(
        "--detail", action="store_true",
        help="逐条打印 quality 用例的实际回答（失败时排查用）",
    )
    return parser.parse_args(argv)


def _run(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    thresholds = dict(THRESHOLDS)
    if args.min_pass_rate is not None:
        if not 0.0 <= args.min_pass_rate <= 1.0:
            print("[参数错误] --min-pass-rate 必须在 0..1 之间", file=sys.stderr)
            return 2
        thresholds["quality"] = args.min_pass_rate

    model = None
    if args.mode == "real":
        try:
            model = build_real_model()
        except MissingModelCredentials as exc:
            print(f"[环境错误] {exc}", file=sys.stderr)
            return 2

    report: EvalReport = run_all(mode=args.mode, model=model)
    print(format_report(report))

    if args.mode == "real" and args.detail:
        quality = [r for r in report.results if r.category == "quality"]
        print("\n真实模型用例明细：")
        print(format_quality_detail(quality))

    if not report.meets_thresholds(thresholds):
        failed = [
            f"{s.category}={s.rate:.0%}(<{thresholds.get(s.category, 1.0):.0%})"
            for s in report.summary_against(thresholds)
            if s.rate < thresholds.get(s.category, 1.0)
        ]
        print(f"\n[未达标] {'，'.join(failed)}")
        return 1

    print("\n[达标] 全部类别通过率不低于阈值")
    return 0


def main() -> int:
    return _run()


if __name__ == "__main__":
    raise SystemExit(main())
