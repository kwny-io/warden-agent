"""评测集测试 —— 黄金集全过 + 报告机制正确，作为 CI 质量门禁。

评测集回答的是"设计表现如何"：意图路由判得准不准、技能触发选得对不对、
端到端任务能不能完成。这里断言各类别通过率不低于阈值——一旦某个确定性
组件的行为退化（如触发词提取被改坏），评测会立刻红。
"""
from warden_agent.evals import (
    THRESHOLDS,
    EvalReport,
    format_report,
    run_all,
)
from warden_agent.evals.runner import CaseResult


def test_评测集全类别达到阈值() -> None:
    report = run_all()
    assert report.results, "黄金集不应为空"
    for s in report.summaries():
        assert s.total > 0, f"类别 {s.category} 没有用例"
        assert s.rate >= THRESHOLDS[s.category], (
            f"{s.category} 通过率 {s.rate:.0%} 低于阈值 {THRESHOLDS[s.category]:.0%}")


def test_黄金集规模符合预期() -> None:
    report = run_all()
    counts = {s.category: s.total for s in report.summaries()}
    assert counts == {"intent": 12, "skill": 8, "e2e": 6}


def test_报告渲染包含类别与整体通过率() -> None:
    text = format_report(run_all())
    for token in ("intent", "skill", "e2e", "overall", "%"):
        assert token in text


def test_报告的失败用例可定位() -> None:
    failing = EvalReport(results=(
        CaseResult(category="intent", name="样例", passed=False,
                   expected="proceed", actual="hint"),
    ))
    assert len(failing.failures()) == 1
    text = format_report(failing)
    assert "failures:" in text
    assert "样例" in text
    assert "proceed" in text and "hint" in text
