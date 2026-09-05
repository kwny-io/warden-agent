"""审批策略测试。"""
import pytest
from tests.conftest import ScriptedModel, weather_tool

from warden_agent.loop.loop import AgentLoop, PolicyDenied
from warden_agent.model.model import ChatResponse, ToolCall
from warden_agent.policy.policy import Decision, PolicyEngine, PolicyResult


def test_deny比ask优先级高() -> None:
    """同一动作，一条说 ASK、一条说 DENY，最终取最严的 DENY。"""
    engine = PolicyEngine()
    engine.add(lambda name, args: PolicyResult(Decision.ASK, "ask"))
    engine.add(lambda name, args: PolicyResult(Decision.DENY, "deny"))
    result = engine.evaluate("x", {})
    assert result.decision == Decision.DENY


def test_ask策略_教学版直接执行() -> None:
    """目录里的工具 + 遇到 ASK：教学版不挂起，直接执行并返回结果。"""
    engine = PolicyEngine()
    engine.add(lambda name, args: PolicyResult(Decision.ASK, "需人工批准"))
    model = ScriptedModel([
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="c1", name="weather.get", arguments={"city": "上海"})],
            finish_reason="tool_calls"),
        ChatResponse(content="天晴。", finish_reason="stop"),
    ])
    reply = AgentLoop(model=model, catalog=weather_tool(), policy_engine=engine).run("查天气")
    assert "天晴" in reply.text


def test_deny策略_直接拒绝执行() -> None:
    """模型想调被 DENY 的工具：抛 PolicyDenied，工具绝不执行。"""
    engine = PolicyEngine()
    engine.add(lambda name, args: PolicyResult(Decision.DENY, "禁止"))
    model = ScriptedModel([
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="c1", name="weather.get", arguments={"city": "上海"})],
            finish_reason="tool_calls"),
    ])
    with pytest.raises(PolicyDenied):
        AgentLoop(model=model, catalog=weather_tool(), policy_engine=engine).run("hi")
