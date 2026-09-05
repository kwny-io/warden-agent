"""AgentSession：真审批 + 完整状态机恢复 测试。"""
import tempfile
from pathlib import Path

from tests.conftest import ScriptedModel, weather_tool

from warden_agent.core.run.status import RunStatus
from warden_agent.model.model import AgentChatModel, ChatRequest, ChatResponse, ToolCall
from warden_agent.policy.policy import Decision, PolicyEngine, PolicyResult
from warden_agent.runtime.session import AgentSession, NeedsApproval
from warden_agent.store.sqlite import SqliteStore


class StreamScriptedModel(AgentChatModel):
    """返回 deltas 的流式假模型：模拟模型边生成边吐增量。"""

    def __init__(self, deltas, final_text) -> None:
        self._deltas = deltas
        self._final_text = final_text
        self.calls = 0

    def chat(self, request: ChatRequest) -> ChatResponse:
        self.calls += 1
        return ChatResponse(content=self._final_text, deltas=list(self._deltas),
                            finish_reason="stop")


def _policy(decision: Decision) -> PolicyEngine:
    engine = PolicyEngine()
    engine.add(lambda name, args: PolicyResult(decision, f"{decision.name}"))
    return engine


def _store() -> SqliteStore:
    return SqliteStore(Path(tempfile.mkdtemp()) / "t.db")


def test_ask工具_挂起等批准_批准后执行并继续() -> None:
    """核心：ASK 工具不再直接放行，而是挂起等批准；批准后才执行并给出最终答复。"""
    store = _store()
    model = ScriptedModel([
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="c1", name="weather.get", arguments={"city": "上海"})],
            finish_reason="tool_calls"),   # 第一次：想调 weather.get（会触发 ASK）
        ChatResponse(content="天晴。", finish_reason="stop"),  # 批准执行后：给结论
    ])
    sess = AgentSession(run_id="r-approve", model=model, catalog=weather_tool(),
                        policy_engine=_policy(Decision.ASK), store=store)

    outcome = sess.start("查上海天气")
    # 第一次：不直接返回答案，而是"需要审批"，Run 停在 WAITING_APPROVAL
    assert isinstance(outcome, NeedsApproval)
    assert outcome.approval.tool_name == "weather.get"
    assert sess.status() == RunStatus.WAITING_APPROVAL

    # 批准 -> 执行工具 -> 继续 -> 拿到最终答复，Run 完成
    final = sess.approve()
    assert isinstance(final, object)
    assert final.text == "天晴。"  # type: ignore[attr-defined]
    assert sess.status() == RunStatus.COMPLETED


def test_ask工具_拒绝后告诉模型_继续() -> None:
    """拒绝：不执行工具，把"已拒绝"作为工具结果，模型据此继续。"""
    store = _store()
    model = ScriptedModel([
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="c1", name="weather.get", arguments={"city": "上海"})],
            finish_reason="tool_calls"),
        ChatResponse(content="已跳过该操作。", finish_reason="stop"),
    ])
    sess = AgentSession(run_id="r-reject", model=model, catalog=weather_tool(),
                        policy_engine=_policy(Decision.ASK), store=store)
    outcome = sess.start("hi")
    assert isinstance(outcome, NeedsApproval)
    final = sess.reject()
    assert final.text == "已跳过该操作。"  # type: ignore[attr-defined]
    # 对话里应包含"用户拒绝"这条工具结果
    assert any("[用户拒绝" in m.content for m in final.messages)  # type: ignore[attr-defined]
    assert sess.status() == RunStatus.COMPLETED


def test_deny工具_直接抛错() -> None:
    store = _store()
    model = ScriptedModel([
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="c1", name="weather.get", arguments={"city": "上海"})],
            finish_reason="tool_calls"),
    ])
    sess = AgentSession(run_id="r-deny", model=model, catalog=weather_tool(),
                        policy_engine=_policy(Decision.DENY), store=store)
    try:
        sess.start("hi")
        assert False, "应抛 PolicyDenied"
    except Exception as e:
        assert type(e).__name__ == "PolicyDenied"


def test_完整状态机恢复_含等待审批中间态() -> None:
    """中等运行时"会话完成" -> 重启 -> 从存档恢复状态和对话（含中间态）。"""
    db = Path(tempfile.mkdtemp()) / "recover.db"

    # 第一次运行：走到 WAITING_APPROVAL 就"断电"
    store = SqliteStore(db)
    model = ScriptedModel([
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="c1", name="weather.get", arguments={"city": "上海"})],
            finish_reason="tool_calls"),
    ])
    sess = AgentSession(run_id="r-recover", model=model, catalog=weather_tool(),
                        policy_engine=_policy(Decision.ASK), store=store)
    sess.start("查天气")
    assert sess.status() == RunStatus.WAITING_APPROVAL
    store.close()  # 模拟断电/关闭

    # 重启：用新连接 + 新的会话，从数据库恢复
    store2 = SqliteStore(db)
    sess2 = AgentSession(run_id="r-recover", model=model, catalog=weather_tool(),
                         policy_engine=_policy(Decision.ASK), store=store2)
    # 状态恢复到了 WAITING_APPROVAL（中间态），而不是从头再来
    assert sess2.status() == RunStatus.WAITING_APPROVAL
    assert sess2.pending_approval() is not None
    assert len(sess2.messages) >= 3  # 恢复了 system + user + assistant(想调工具)
    store2.close()


def test_工具结果消息携带匹配的tool_call_id() -> None:
    """真实 API 回归测试：tool 结果必须引用 assistant 发起的 tool_call.id，
    否则真实 API 会报"assistant tool_calls 必须一一被 tool 响应"(400)，
    这是 mock 测不出的坑（由真实调用发现）。"""
    store = _store()
    model = ScriptedModel([
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="call-abc", name="weather.get", arguments={"city": "上海"})],
            finish_reason="tool_calls"),
        ChatResponse(content="上海今天晴。", finish_reason="stop"),
    ])
    sess = AgentSession(run_id="r-toolid", model=model, catalog=weather_tool(),
                        policy_engine=_policy(Decision.ALLOW), store=store)
    final = sess.start("查上海天气")

    # 应该有一条 assistant(tool_call=call-abc) 和一条 tool(tool_call=call-abc) 配对
    assistant_calls = [m.tool_call.id for m in final.messages  # type: ignore[attr-defined]
                       if m.role == "assistant" and m.tool_call]
    tool_calls = [m.tool_call.id for m in final.messages  # type: ignore[attr-defined]
                  if m.role == "tool" and m.tool_call]
    assert assistant_calls == ["call-abc"]
    assert tool_calls == ["call-abc"]  # 工具结果必须携带同一 id，才能真实 API 续轮


def test_流式_stream逐增量产出事件() -> None:
    """SSE 打字机核心：stream() 应先把每个增量(delta)推出，最后推 final。"""
    store = _store()
    model = StreamScriptedModel(deltas=["你", "好", "！"], final_text="你好！")
    sess = AgentSession(run_id="r-stream", model=model, catalog=weather_tool(),
                        policy_engine=_policy(Decision.ALLOW), store=store)
    events = list(sess.stream("你好"))
    types = [e["type"] for e in events]
    # start -> 三个 delta -> final
    assert types[0] == "start"
    assert types[1:-1] == ["delta", "delta", "delta"]
    assert types[-1] == "final"
    # delta 的文本逐段正确
    texts = [e["text"] for e in events if e["type"] == "delta"]
    assert texts == ["你", "好", "！"]
    # 完成后会话处于终态
    assert sess.is_terminal()


