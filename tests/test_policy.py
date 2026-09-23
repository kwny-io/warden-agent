"""审批策略测试。"""
import pytest
from tests.conftest import ScriptedModel, weather_tool

from warden_agent.loop.loop import AgentLoop
from warden_agent.model.model import ChatResponse, ToolCall
from warden_agent.policy.policy import (
    ApprovalRequired,
    Decision,
    PolicyDenied,
    PolicyEngine,
    PolicyResult,
    deny_when_path_in_protected,
)


def test_deny比ask优先级高() -> None:
    """同一动作，一条说 ASK、一条说 DENY，最终取最严的 DENY。"""
    engine = PolicyEngine()
    engine.add(lambda name, args: PolicyResult(Decision.ASK, "ask"))
    engine.add(lambda name, args: PolicyResult(Decision.DENY, "deny"))
    result = engine.evaluate("x", {})
    assert result.decision == Decision.DENY


def test_ask策略_挂起等批准_绝不执行() -> None:
    """ASK 不再被当成 EXECUTE：loop 挂起并抛 ApprovalRequired，工具绝不执行。

    与产品路径（runtime/session.py 的 _hold_for_approval）语义一致。
    """
    engine = PolicyEngine()
    engine.add(lambda name, args: PolicyResult(Decision.ASK, "需人工批准"))
    model = ScriptedModel([
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="c1", name="weather.get", arguments={"city": "上海"})],
            finish_reason="tool_calls"),
        ChatResponse(content="天晴。", finish_reason="stop"),
    ])
    loop = AgentLoop(model=model, catalog=weather_tool(), policy_engine=engine)
    with pytest.raises(ApprovalRequired) as exc:
        loop.run("查天气")
    assert exc.value.tool_name == "weather.get"
    assert exc.value.arguments == {"city": "上海"}
    # 工具没被执行：模型只被调用了一次（挂起后不再继续）
    assert model.calls == 1


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


# ---- 零规则默认 + 逐规则异常隔离 ----

def test_零规则_默认放行() -> None:
    """没有配置任何策略时，门禁默认放行（刻意固定的默认，不是漏配）。"""
    result = PolicyEngine().evaluate("anything", {"x": 1})
    assert result.decision == Decision.ALLOW
    assert "默认放行" in result.reason


def test_单条规则抛异常_不击穿门禁_按最严处理() -> None:
    """某条规则自身写错抛异常时，门禁不崩溃，且按最严(DENY)处理（fail-closed）。"""
    def boom(name: str, args: dict) -> PolicyResult:
        raise ValueError("规则写错了")

    engine = PolicyEngine()
    engine.add(boom)
    engine.add(lambda name, args: PolicyResult(Decision.ALLOW, "ok"))
    result = engine.evaluate("x", {})
    assert result.decision == Decision.DENY
    assert "ValueError" in result.reason


# ---- 受保护路径策略：组件级比较 ----

def test_受保护路径_子目录命中() -> None:
    policy = deny_when_path_in_protected(("/data",))
    assert policy("fs.read", {"path": "/data/secret.txt"}).decision == Decision.DENY
    assert policy("fs.read", {"path": "/data"}).decision == Decision.DENY


def test_受保护路径_前缀相似但不命中() -> None:
    """`/data-evil` 不是 `/data` 的子目录，不能被 startswith 误伤。"""
    policy = deny_when_path_in_protected(("/data",))
    assert policy("fs.read", {"path": "/data-evil"}).decision == Decision.ALLOW
    assert policy("fs.read", {"path": "/database"}).decision == Decision.ALLOW


def test_受保护路径_反斜杠也按组件识别() -> None:
    policy = deny_when_path_in_protected(("C:/Users/me",))
    assert policy("fs.read", {"path": "C:\\Users\\me\\.ssh\\id_rsa"}).decision == Decision.DENY


def test_受保护路径_无path参数放行() -> None:
    policy = deny_when_path_in_protected(("/data",))
    assert policy("weather.get", {"city": "上海"}).decision == Decision.ALLOW
