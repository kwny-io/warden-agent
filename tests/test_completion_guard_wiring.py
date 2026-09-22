"""完成门禁（CompletionGuard）接线回归。

`CompletionGuard` 此前**定义了却没接进主循环**——`AgentSession` 直接
`begin_completing()/complete()`，门禁形同虚设。本批把它收口进 `_mark_completed`：
完成路径唯一，且带"本次新产生的"悬挂工具调用时不放行。
"""

from __future__ import annotations

import pytest

from warden_agent.agent import InMemoryRunStore
from warden_agent.core.run.status import RunStatus
from warden_agent.model.fake import FakeModel
from warden_agent.model.model import Message, ToolCall
from warden_agent.policy.policy import PolicyEngine
from warden_agent.runtime.checkpoint import CompletionGuardError
from warden_agent.runtime.session import AgentSession
from warden_agent.tool.catalog import ToolCatalog


def _session() -> AgentSession:
    sess = AgentSession(
        run_id="r-guard",
        model=FakeModel(),
        catalog=ToolCatalog(),
        policy_engine=PolicyEngine(),
        store=InMemoryRunStore(),
    )
    sess.run.mark_queued()
    sess.run.start()  # RUNNING —— begin_completing 只允许从 RUNNING
    return sess


def test_悬挂工具调用会挡住完成() -> None:
    """assistant 发起了 tool_call 却没配对结果 → 不许进入 COMPLETED。"""
    sess = _session()
    sess.messages.append(Message(
        role="assistant", content="[调用工具 weather.get]",
        tool_call=ToolCall(id="call-x", name="weather.get", arguments={}),
    ))
    assert sess._dangling_tool_ids() == {"call-x"}

    with pytest.raises(CompletionGuardError):
        sess._mark_completed("答案")
    assert not sess.run.is_terminal(), "门禁不过时不得进入终态"


def test_工具调用配对后不算悬挂() -> None:
    sess = _session()
    call = ToolCall(id="call-y", name="weather.get", arguments={})
    sess.messages.append(Message(role="assistant", content="[调用工具]", tool_call=call))
    sess.messages.append(Message(role="tool", content="晴", tool_call=call))
    assert sess._dangling_tool_ids() == set()
    sess._mark_completed("答案")
    assert sess.run.status == RunStatus.COMPLETED


def test_恢复来的悬空调用不算本次未执行() -> None:
    """历史里本就有悬空调用（进程崩在"记下调用、还没执行"之间）时，仍应能正常完成。"""
    sess = _session()
    call = ToolCall(id="call-hist", name="weather.get", arguments={})
    sess.messages.append(Message(role="assistant", content="[调用工具]", tool_call=call))
    # 驱动开始时的基线已包含这条历史悬空 → 完成门禁不应因它而拦
    sess._dangling_baseline = sess._dangling_tool_ids()
    sess._mark_completed("答案")
    assert sess.run.status == RunStatus.COMPLETED


def test_正常完成通过门禁并落终态() -> None:
    sess = _session()
    sess._mark_completed("最终回答")
    assert sess.run.status == RunStatus.COMPLETED
