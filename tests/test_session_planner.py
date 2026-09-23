"""多步规划接进**产品路径**（AgentSession）的测试。

背景：`loop/loop.py` 已有可推进的有界多步规划（PlanState），但产品路径
`runtime/session.py` 此前只在每次请求里用 `plan_context(..., stage=0)` 注入一次
（current 恒为 0），多步行为在生产上是死的。这组测试钉住：

  1. 规划启用时，会话**跨迭代推进**步号（0→1→…→N 后即停），并每轮重注入当前阶段；
  2. 某步失败时**冻结/标记失败**、不推进，后续阶段不被静默标为完成；
  3. 审批挂起的那一步：批准执行成功后才推进；
  4. 关闭规划时行为不变（无规划注入、无额外规划调用）；
  5. 非复杂任务只规划一次（不每轮重复触发 planner.build）。
"""
from __future__ import annotations

from typing import Any

from tests.conftest import weather_tool

from warden_agent.agent import InMemoryRunStore
from warden_agent.core.run.status import RunStatus
from warden_agent.loop.planner import PlanStep, TaskPlan, plan_as_context
from warden_agent.model.model import AgentChatModel, ChatRequest, ChatResponse, ToolCall
from warden_agent.policy.policy import Decision, PolicyEngine, PolicyResult
from warden_agent.runtime.session import AgentSession, FinalReply, NeedsApproval
from warden_agent.tool.catalog import ToolCatalog


class ScriptedCapture(AgentChatModel):
    """按剧本返回响应，同时记录每次发给模型的请求（用来断言注入了什么）。"""

    def __init__(self, script: list[ChatResponse]) -> None:
        self._script = list(script)
        self.calls = 0
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        resp = self._script[min(self.calls, len(self._script) - 1)]
        self.calls += 1
        return resp

    def plan_lines(self) -> list[str]:
        """每次请求里注入的"当前阶段"行（含 ▶ 的那一行）。"""
        out: list[str] = []
        for req in self.requests:
            for m in req.messages:
                if m.role == "system" and (m.content or "").startswith("[任务规划]"):
                    line = next(
                        (ln for ln in (m.content or "").splitlines() if "▶" in ln), ""
                    )
                    if line:
                        out.append(line)
        return out


class ThreeStepPlanner:
    """固定 3 步、始终判定为复杂的测试规划器。"""

    def __init__(self) -> None:
        self.build_calls = 0
        self.plan = TaskPlan(is_complex=True, steps=[
            PlanStep("一", "目标一"),
            PlanStep("二", "目标二"),
            PlanStep("三", "目标三"),
        ])

    def build(self, text: str) -> TaskPlan:
        self.build_calls += 1
        return self.plan

    def context(self, plan: TaskPlan, current: int) -> str:
        return plan_as_context(plan, current)


class SimplePlanner:
    """始终判定为非复杂任务（模拟"简单任务不拆阶段"）。"""

    def __init__(self) -> None:
        self.build_calls = 0

    def build(self, text: str) -> TaskPlan:
        self.build_calls += 1
        return TaskPlan(is_complex=False)

    def context(self, plan: TaskPlan, current: int) -> str:
        return plan_as_context(plan, current)


def _tc(cid: str, name: str, args: dict[str, Any]) -> ToolCall:
    return ToolCall(id=cid, name=name, arguments=args)


def _session(model: AgentChatModel, *, planner: Any = None,
             catalog: ToolCatalog | None = None,
             policy: PolicyEngine | None = None) -> AgentSession:
    return AgentSession(
        run_id="run-plan",
        model=model,
        catalog=catalog or weather_tool(),
        policy_engine=policy or PolicyEngine(),
        store=InMemoryRunStore(),
        planner=planner,
    )


def _ask_policy() -> PolicyEngine:
    engine = PolicyEngine()
    engine.add(lambda name, args: PolicyResult(Decision.ASK, "ASK"))
    return engine


# ---------- 1. 跨迭代推进 ----------


def test_会话_规划随每步成功推进并在上下文里可见() -> None:
    """3 步计划：每完成一步推进，注入的阶段上下文随之从 1/3 走到 3/3。"""
    model = ScriptedCapture([
        ChatResponse(content=None, finish_reason="tool_calls",
                     tool_calls=[_tc("c1", "weather.get", {"city": "A"})]),
        ChatResponse(content=None, finish_reason="tool_calls",
                     tool_calls=[_tc("c2", "weather.get", {"city": "B"})]),
        ChatResponse(content=None, finish_reason="tool_calls",
                     tool_calls=[_tc("c3", "weather.get", {"city": "C"})]),
        ChatResponse(content="报告完成。", finish_reason="stop"),
    ])
    planner = ThreeStepPlanner()
    sess = _session(model, planner=planner)
    outcome = sess.start("调研三家公司并写报告")
    assert isinstance(outcome, FinalReply)

    plan = sess._plan_state  # noqa: SLF001 - 测试内部进度状态
    assert plan is not None
    assert (plan.current, plan.total) == (3, 3)
    assert plan.done is True

    lines = model.plan_lines()
    # 前 3 次请求的有 ▶ 行应分别是 1/3、2/3、3/3；到末步后无 ▶（不再有行）
    assert [ln.split("：")[0] for ln in lines] == [
        "▶ 1/3 一", "▶ 2/3 二", "▶ 3/3 三",
    ], f"阶段上下文应逐步推进，实际：{lines}"
    assert lines[0].endswith("目标一")
    assert lines[2].endswith("目标三")


def test_会话_规划到末步后即停不越界() -> None:
    """超过总步数的成功轮次不再推进（有界）。"""
    model = ScriptedCapture([
        ChatResponse(content=None, finish_reason="tool_calls",
                     tool_calls=[_tc("c1", "weather.get", {"city": "A"})]),
        ChatResponse(content=None, finish_reason="tool_calls",
                     tool_calls=[_tc("c2", "weather.get", {"city": "B"})]),
        ChatResponse(content=None, finish_reason="tool_calls",
                     tool_calls=[_tc("c3", "weather.get", {"city": "C"})]),
        ChatResponse(content=None, finish_reason="tool_calls",
                     tool_calls=[_tc("c4", "weather.get", {"city": "D"})]),
        ChatResponse(content="完成。", finish_reason="stop"),
    ])
    sess = _session(model, planner=ThreeStepPlanner())
    sess.start("调研三家并写报告")
    plan = sess._plan_state  # noqa: SLF001
    assert plan is not None
    assert plan.current == 3  # 第 4 个成功轮次不再推进
    assert plan.done is True


# ---------- 2. 失败冻结 ----------


def test_会话_失败步骤不推进且不标记后续完成() -> None:
    """工具执行失败：进度冻结在当前步并标失败，后续阶段不被静默算作完成。"""
    model = ScriptedCapture([
        ChatResponse(content=None, finish_reason="tool_calls",
                     tool_calls=[_tc("c1", "weather.missing", {"city": "A"})]),
        ChatResponse(content="改用其它方式完成。", finish_reason="stop"),
    ])
    sess = _session(model, planner=ThreeStepPlanner())
    outcome = sess.start("调研三家并写报告")
    assert isinstance(outcome, FinalReply)

    plan = sess._plan_state  # noqa: SLF001
    assert plan is not None
    assert plan.current == 0
    assert plan.failed is True
    assert plan.done is False
    snap = plan.to_dict()
    assert [s["done"] for s in snap["steps"]] == [False, False, False]


# ---------- 3. 审批挂起的那一步 ----------


def test_会话_审批挂起不推进_批准成功后才推进() -> None:
    model = ScriptedCapture([
        ChatResponse(content=None, finish_reason="tool_calls",
                     tool_calls=[_tc("c1", "weather.get", {"city": "上海"})]),
        ChatResponse(content="天晴。", finish_reason="stop"),
    ])
    sess = _session(model, planner=ThreeStepPlanner(), policy=_ask_policy())
    outcome = sess.start("调研三家并写报告")
    assert isinstance(outcome, NeedsApproval)
    # 挂起时该步尚未执行 —— 进度不应推进
    assert sess._plan_state is not None  # noqa: SLF001
    assert sess._plan_state.current == 0  # noqa: SLF001
    assert sess.status() == RunStatus.WAITING_APPROVAL

    final = sess.approve()
    assert isinstance(final, FinalReply)
    # 批准后工具执行成功 —— 推进到第 2 步
    assert sess._plan_state is not None  # noqa: SLF001
    assert sess._plan_state.current == 1  # noqa: SLF001


# ---------- 4. 关闭规划：行为不变 ----------


def test_会话_关闭规划时不注入阶段且行为不变() -> None:
    """planner=None 时：无规划注入、无规划状态，循环照常跑到最终回答。"""
    model = ScriptedCapture([
        ChatResponse(content=None, finish_reason="tool_calls",
                     tool_calls=[_tc("c1", "weather.get", {"city": "上海"})]),
        ChatResponse(content="完成", finish_reason="stop"),
    ])
    sess = _session(model, planner=None)
    outcome = sess.start("调研三家并写报告")
    assert isinstance(outcome, FinalReply)
    assert outcome.text == "完成"
    assert sess._plan_state is None  # noqa: SLF001
    # 任何请求都不该出现规划系统消息
    assert not any(
        (m.content or "").startswith("[任务规划]")
        for req in model.requests for m in req.messages
    )


# ---------- 5. 非复杂任务只规划一次 ----------


def test_会话_非复杂任务只触发一次规划() -> None:
    """简单任务不该每轮都重新调 planner.build（ModelPlanner 会多花一次模型调用）。"""
    model = ScriptedCapture([
        ChatResponse(content=None, finish_reason="tool_calls",
                     tool_calls=[_tc("c1", "weather.get", {"city": "上海"})]),
        ChatResponse(content="完成", finish_reason="stop"),
    ])
    planner = SimplePlanner()
    sess = _session(model, planner=planner)
    sess.start("上海天气怎么样")
    assert planner.build_calls == 1
    assert sess._plan_state is None  # noqa: SLF001
