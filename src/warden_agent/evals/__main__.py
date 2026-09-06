"""CLI 入口：python -m warden_agent.evals —— 跑评测集并打印报告。

类别通过率低于阈值时以退出码 1 结束（可作 CI 质量门禁）。
"""

from __future__ import annotations

from warden_agent.evals.runner import format_report, run_all


def main() -> int:
    report = run_all()
    print(format_report(report))
    if not report.meets_thresholds():
        print("\n[未达标] 存在低于阈值的类别（intent/skill >= 0.9, e2e = 1.0）")
        return 1
    print("\n[达标] 全部类别通过率不低于阈值")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
