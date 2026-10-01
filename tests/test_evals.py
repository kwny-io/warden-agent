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
    """脚本模式的硬门禁：intent/skill/e2e 全要达标。

    只看**有数据的**类别：quality（真实模型质量）在脚本模式下根本没跑，
    把"0 条 → 0%"当真会让默认门禁无缘无故变红。
    """
    report = run_all()
    assert report.results, "黄金集不应为空"
    for s in report.summary_against(THRESHOLDS):
        assert s.total > 0, f"类别 {s.category} 没有用例"
        assert s.rate >= THRESHOLDS[s.category], (
            f"{s.category} 通过率 {s.rate:.0%} 低于阈值 {THRESHOLDS[s.category]:.0%}")
    assert report.meets_thresholds()


def test_脚本模式不产生质量类别数据() -> None:
    """类别隔离：脚本模型无法评"模型好坏"，quality 必须是空的而不是满分的。

    这条是在守一个很容易写错的地方：拿脚本模型去跑提示注入用例，
    报告上会显示"模型质量 100%"，而实际跑的是自己写的剧本——比不跑更糟。
    """
    report = run_all()
    assert report.mode == "scripted"
    assert [s.total for s in report.summaries() if s.category == "quality"] == [0]


def test_真实模式没传模型时直接报错() -> None:
    """不允许静默退回脚本模型（否则等于自己判自己）。"""
    import pytest

    with pytest.raises(ValueError, match="真实模型"):
        run_all(mode="real")


def test_未知模式报错() -> None:
    import pytest

    with pytest.raises(ValueError, match="未知评测模式"):
        run_all(mode="vibes")


def test_黄金集规模符合预期() -> None:
    report = run_all()
    counts = {s.category: s.total for s in report.summaries()}
    # e2e 从 6 扩到 10：原来的断言只判"回答非空"，等于没测；
    # 现在断在**轨迹与决策**上（失败自愈 / 防打转 / 意图门禁 / 策略 DENY /
    # 迭代上限收口 / 参数保真 / 配对不变量）。
    # quality=0 不是"漏了"：真实模型质量要 --mode real 才跑（见 test_evals_real_mode.py）。
    assert counts == {"intent": 12, "skill": 8, "e2e": 10, "quality": 0}
    assert report.mode == "scripted"


def test_报告渲染包含类别与整体通过率() -> None:
    text = format_report(run_all())
    for token in ("intent", "skill", "e2e", "overall", "%", "mode: scripted"):
        assert token in text
    # 没跑过的类别不要印出来（`0/0 0.0%` 会被读成"模型质量 0 分"）
    assert "quality" not in text


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
