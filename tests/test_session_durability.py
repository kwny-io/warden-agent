"""审批持久化崩溃窗口 + 收尾顺序 + 确定性失败不可重试 的回归测试。

这些测试都在模拟"两次写之间断电"——真实进程崩溃无法在单测里复现，
所以用"在第二次写上抛错的存储"来精确制造那个窗口，断言修复后的不变式：

  - 挂起审批时：审批单必须先落库，状态后落。否则崩在中间会留下"在等待审批、
    却没有审批单"的 run，永久卡死。
  - 批准执行时：工具结果必须先落库，审批单后清。否则崩在中间会**静默丢弃**
    这次已批准的动作。
  - 收尾时：先交付（类型化校验）再置 COMPLETED。否则校验失败会留下"完成却没结果"。
  - 策略拒绝是**确定性失败**：标 retryable=False，恢复不再重试。
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from tests.conftest import ScriptedModel, weather_tool

from warden_agent.core.run.status import RunStatus
from warden_agent.model.model import ChatResponse, Message, ToolCall
from warden_agent.policy.policy import Decision, PolicyDenied, PolicyEngine, PolicyResult
from warden_agent.runtime.checkpoint import checkpoint_store_for
from warden_agent.runtime.recovery import RecoveryController
from warden_agent.runtime.session import (
    AgentSession,
    NeedsApproval,
    RunNotResumable,
    TypedOutputError,
)
from warden_agent.store.sqlite import SqliteStore


def _store() -> SqliteStore:
    return SqliteStore(Path(tempfile.mkdtemp()) / "t.db")


def _policy(decision: Decision) -> PolicyEngine:
    engine = PolicyEngine()
    engine.add(lambda name, args: PolicyResult(decision, f"{decision.name}"))
    return engine


def _ask_script() -> list[ChatResponse]:
    """第一次就想调会触发 ASK 的工具。"""
    return [ChatResponse(
        content=None,
        tool_calls=[ToolCall(id="c1", name="weather.get", arguments={"city": "上海"})],
        finish_reason="tool_calls",
    )]


class _CrashOnWaitingRunStore:
    """在"把 run 置为 WAITING_APPROVAL"落盘时抛错，模拟两次写之间的断电。"""

    def __init__(self, inner: SqliteStore) -> None:
        self._inner = inner

    def save_run(self, run: Any) -> None:
        if run.status == RunStatus.WAITING_APPROVAL:
            raise RuntimeError("模拟置 WAITING_APPROVAL 时崩溃")
        self._inner.save_run(run)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _CrashOnClearStore:
    """在"清除待审批单"时抛错，模拟"结果落库之后、清审批单之前"的断电。"""

    def __init__(self, inner: SqliteStore) -> None:
        self._inner = inner

    def clear_pending_approval(self, run_id: str) -> None:
        raise RuntimeError("模拟清除审批单时崩溃")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


# ---------- P1(a)：挂起审批时，审批单先落库 ----------


def test_挂起审批_审批单先落库_置状态前崩溃仍可批准() -> None:
    """崩在"置 WAITING_APPROVAL"这一步后，run 仍必须"可批准"（而不是永久卡死）。"""
    inner = _store()
    store = _CrashOnWaitingRunStore(inner)
    sess = AgentSession(run_id="r-half-approval", model=ScriptedModel(_ask_script()),
                        catalog=weather_tool(), policy_engine=_policy(Decision.ASK),
                        store=store)

    with pytest.raises(RuntimeError, match="WAITING_APPROVAL"):
        sess.start("查天气")

    # 关键：审批单必须先落库（修正前：状态先落、审批单没存 → 批不了也跑不了）
    assert inner.load_pending_approval("r-half-approval") is not None

    # 重启一个会话，能读回待审批请求 → 仍是"可批准"状态
    reopened = AgentSession(run_id="r-half-approval", model=ScriptedModel(_ask_script()),
                            catalog=weather_tool(), policy_engine=_policy(Decision.ASK),
                            store=inner)
    assert reopened.pending_approval() is not None


# ---------- P1(b)：批准时，工具结果先落库 ----------


def test_批准_结果先落库_清审批单前崩溃不丢已批准动作() -> None:
    """清审批单时崩溃，已批准工具的结果必须已经在库里（不能静默丢弃）。"""
    inner = _store()
    store = _CrashOnClearStore(inner)
    model = ScriptedModel(_ask_script() + [ChatResponse(content="天晴。", finish_reason="stop")])
    sess = AgentSession(run_id="r-approved", model=model, catalog=weather_tool(),
                        policy_engine=_policy(Decision.ASK), store=store)
    assert isinstance(sess.start("查天气"), NeedsApproval)

    with pytest.raises(RuntimeError, match="清除审批单"):
        sess.approve()

    # 关键：工具结果已在库（修正前：先清审批单、后执行，这个失败会让动作消失）
    msgs = inner.load_messages("r-approved")
    assert any(
        m.role == "tool" and m.tool_call and m.tool_call.id == "c1" for m in msgs
    ), "已批准的工具结果必须已落库，不能因清除审批单失败而丢失"


def test_批准_删除审批单发生在工具结果之后() -> None:
    """直接断言写入顺序：工具结果 append 必须先于 clear_pending_approval。"""
    inner = _store()
    events: list[str] = []

    class _Recorder:
        def __init__(self, inner: SqliteStore) -> None:
            self._inner = inner

        def append_message(self, run_id: str, message: Message) -> None:
            events.append(f"append:{message.role}")
            self._inner.append_message(run_id, message)

        def clear_pending_approval(self, run_id: str) -> None:
            events.append("clear")
            self._inner.clear_pending_approval(run_id)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    store = _Recorder(inner)
    model = ScriptedModel(_ask_script() + [ChatResponse(content="天晴。", finish_reason="stop")])
    sess = AgentSession(run_id="r-order", model=model, catalog=weather_tool(),
                        policy_engine=_policy(Decision.ASK), store=store)
    sess.start("查天气")
    sess.approve()

    assert "append:tool" in events and "clear" in events
    assert events.index("append:tool") < events.index("clear")


# ---------- P2：类型化交付失败 → FAILED（而不是 COMPLETED） ----------


class _Report(BaseModel):
    city: str
    condition: str


def test_类型化交付失败_run标记为FAILED而非COMPLETED() -> None:
    """模型返回的 JSON 过不了 schema 校验时，run 必须是 FAILED（可重试），
    而不是"已 COMPLETED 却没交付结果"（修正前 _mark_failed 对终态 no-op）。"""
    store = _store()
    # 缺 condition 字段 → 校验必失败
    model = ScriptedModel([ChatResponse(content='{"city": "上海"}', finish_reason="stop")])
    sess = AgentSession(run_id="r-typed-fail", model=model, catalog=weather_tool(),
                        policy_engine=_policy(Decision.ALLOW), store=store)

    with pytest.raises(TypedOutputError):
        sess.run_typed(_Report, "上海天气？")

    assert sess.status() == RunStatus.FAILED
    assert store.load_run("r-typed-fail").status == RunStatus.FAILED


# ---------- P3：策略拒绝是确定性失败，不可重试 ----------


def test_策略拒绝标为不可重试_恢复不再无限重试() -> None:
    """DENY 是确定性的：标 retryable=False，恢复计划判终态，resume 也被拦。"""
    store = _store()
    cp_store = checkpoint_store_for(store)
    assert cp_store is not None
    model = ScriptedModel(_ask_script())  # 永远不会被消费到第二句
    sess = AgentSession(run_id="r-deny", model=model, catalog=weather_tool(),
                        policy_engine=_policy(Decision.DENY), store=store,
                        checkpoint_store=cp_store)
    with pytest.raises(PolicyDenied):
        sess.start("删库")

    cp = cp_store.load("r-deny")
    assert cp is not None
    assert cp.status == RunStatus.FAILED
    assert cp.retryable is False

    # 恢复计划不再把它排进"重试"队列
    plan = RecoveryController(cp_store).plan()
    assert plan.action_for("r-deny") == "skip_failed"
    assert [c.run_id for c in plan.to_retry] == []

    # 手动 resume 同样被拦（确定性失败重试也不会成功）
    reopened = AgentSession(run_id="r-deny", model=model, catalog=weather_tool(),
                            policy_engine=_policy(Decision.DENY), store=store,
                            checkpoint_store=cp_store)
    with pytest.raises(RunNotResumable):
        reopened.resume()
