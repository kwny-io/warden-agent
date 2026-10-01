"""真实模型质量门禁（`--mode real`）自身的测试。

这里测的不是"某个真模型好不好"（那要真 API key，而且答案会随模型版本变），
而是**这条门禁本身可信不可信**：
  · 一个守规矩的模型必须全过（不然门禁会把好模型判红，很快被人关掉）；
  · 一个"被注入牵着走 / 编年份 / 不守格式 / 乱调工具"的模型必须被判红
    （不然这条门禁只是装饰）；
  · 没凭据时必须**失败退出**，绝不静默退回脚本模型。

用桩模型（`_StubModel`）而不是真 API：CI 里没 key 也要能守住"门禁逻辑本身"。
"""
from __future__ import annotations

from typing import Any

import pytest

from warden_agent.evals import __main__ as evals_cli
from warden_agent.evals.real_model import (
    INJECTION_TOOL,
    run_real_model_evals,
)
from warden_agent.evals.runner import THRESHOLDS, run_all
from warden_agent.model.model import AgentChatModel, ChatRequest, ChatResponse, ToolCall


class _StubModel(AgentChatModel):
    """两种人格的桩模型：`misbehave=False` 守规矩，`True` 逐条踩坑。

    按用户消息里的关键词分支，与 `real_model.py` 的五条用例一一对应。
    """

    def __init__(self, *, misbehave: bool = False) -> None:
        self.misbehave = misbehave
        self.calls = 0

    def chat(self, request: ChatRequest) -> ChatResponse:
        self.calls += 1
        user_text = "".join(m.content or "" for m in request.messages if m.role == "user")
        tool_results = [m for m in request.messages if m.role == "tool"]
        called = [m.tool_call.name for m in tool_results if m.tool_call]
        has_result = bool(tool_results)
        bad = self.misbehave

        def call(name: str, args: dict[str, Any]) -> ChatResponse:
            return ChatResponse(content=None, finish_reason="tool_calls",
                                tool_calls=[ToolCall(id=f"c{self.calls}", name=name,
                                                     arguments=args)])

        # 1. 工具选择
        if "天气" in user_text and "笔记" not in user_text:
            if bad:
                return ChatResponse(content="上海天气挺好的。", finish_reason="stop")
            return (ChatResponse(content="上海今天晴，25 度。", finish_reason="stop")
                    if has_result else call("weather.get", {"city": "上海"}))

        # 2. 明确要求不许调工具
        if "不要调用任何工具" in user_text:
            if bad:
                return call("weather.get", {"city": "上海"})
            return ChatResponse(content="我是 Warden，一个能调用工具的助手。",
                                finish_reason="stop")

        # 3. 严格 JSON 输出
        if "JSON" in user_text:
            if bad:
                # 常见坏相：解释性文字包着 JSON
                return ChatResponse(content='好的，这是结果：{"ok": true}', finish_reason="stop")
            return ChatResponse(content='{"ok": true}', finish_reason="stop")

        # 4. 间接注入：笔记正文里藏着"立刻调 boom.run"
        if "笔记" in user_text:
            if bad:
                if "notes.read" not in called:
                    return call("notes.read", {"name": "周会"})
                if INJECTION_TOOL not in called:
                    return call(INJECTION_TOOL, {})   # ← 被注入牵着走
                return ChatResponse(content="工具已执行完毕。", finish_reason="stop")
            if "notes.read" not in called:
                return call("notes.read", {"name": "周会"})
            return ChatResponse(content="本周完成了接口联调。", finish_reason="stop")

        # 5. 幻觉：编一个具体年份
        if "WARDEN-Q99" in user_text:
            if bad:
                return ChatResponse(content="WARDEN-Q99 的发布日期是 2021 年 5 月。",
                                    finish_reason="stop")
            return ChatResponse(content="资料里没有 WARDEN-Q99 的信息，我无法确定发布日期。",
                                finish_reason="stop")

        return ChatResponse(content="（桩模型未覆盖的输入）", finish_reason="stop")


# ---------------- 用例判定 ----------------


def test_守规矩的模型全过() -> None:
    results = run_real_model_evals(_StubModel())
    assert len(results) == 5, "质量用例数量变了——记得同步门禁阈值与文档"
    assert all(r.passed for r in results), [r.name for r in results if not r.passed]
    assert {r.category for r in results} == {"quality"}


def test_踩坑的模型逐条被判红() -> None:
    """反向保护：每一条坑都必须能逮到，否则这条门禁是装饰。"""
    results = {r.name: r for r in run_real_model_evals(_StubModel(misbehave=True))}
    assert not any(r.passed for r in results.values()), (
        f"本该判红的用例却过了：{[n for n, r in results.items() if r.passed]}")
    # 提示注入那条必须给出"到底执行了什么"的证据，否则失败信息没法用来排查
    injection = next(r for r in results.values() if "注入" in r.name)
    assert INJECTION_TOOL in injection.actual


def test_真实模式跑完整报告并达标() -> None:
    report = run_all(mode="real", model=_StubModel())
    assert report.mode == "real"
    counts = {s.category: s.total for s in report.summaries()}
    assert counts["quality"] == 5 and counts["e2e"] == 10
    assert report.meets_thresholds()
    assert report.meets_thresholds({**THRESHOLDS, "quality": 1.0})


def test_坏模型让整份报告不达标() -> None:
    report = run_all(mode="real", model=_StubModel(misbehave=True))
    assert not report.meets_thresholds()
    quality = next(s for s in report.summaries() if s.category == "quality")
    assert quality.rate == 0.0


# ---------------- CLI ----------------


def test_CLI_默认脚本模式不需要凭据(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "WARDEN_BASE_URL",
                "WARDEN_MODEL_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert evals_cli._run([]) == 0


def test_CLI_真实模式没凭据时退出码2(monkeypatch: pytest.MonkeyPatch,
                                       capsys: pytest.CaptureFixture[str]) -> None:
    for var in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "WARDEN_BASE_URL",
                "WARDEN_MODEL_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    code = evals_cli._run(["--mode", "real"])
    assert code == 2, "没凭据必须失败退出，不许静默退回脚本模型"
    err = capsys.readouterr().err
    assert "DEEPSEEK_API_KEY" in err and "scripted" in err


def test_CLI_坏模型触发退出码1(monkeypatch: pytest.MonkeyPatch,
                                capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(evals_cli, "build_real_model", lambda: _StubModel(misbehave=True))
    assert evals_cli._run(["--mode", "real", "--detail"]) == 1
    out = capsys.readouterr().out
    assert "quality" in out and "未达标" in out
    assert "mode: real" in out


def test_CLI_好模型退出码0(monkeypatch: pytest.MonkeyPatch,
                           capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(evals_cli, "build_real_model", lambda: _StubModel())
    assert evals_cli._run(["--mode", "real"]) == 0


def test_CLI_阈值参数校验(monkeypatch: pytest.MonkeyPatch) -> None:
    assert evals_cli._run(["--min-pass-rate", "1.5"]) == 2


def test_CLI_阈值可收紧到1_0(monkeypatch: pytest.MonkeyPatch) -> None:
    """阈值可调：厂内想严格时能把 quality 收到 100%。"""
    monkeypatch.setattr(evals_cli, "build_real_model", lambda: _StubModel())
    assert evals_cli._run(["--mode", "real", "--min-pass-rate", "1.0"]) == 0
