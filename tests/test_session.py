"""AgentSession：真审批 + 完整状态机恢复 测试。"""
import tempfile
from pathlib import Path

from tests.conftest import ScriptedModel, weather_tool

from warden_agent.core.run.status import AgentRun, RunStatus
from warden_agent.model.model import AgentChatModel, ChatRequest, ChatResponse, Message, ToolCall
from warden_agent.policy.policy import Decision, PolicyEngine, PolicyResult
from warden_agent.runtime.session import AgentSession, FinalReply, NeedsApproval
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


def test_流式工具执行结果作为事件推出且与落库正文一致() -> None:
    """工具结果必须上屏：以前只有模型看得到（界面只有"🔧 调用工具：x"）。

    stream() 要在执行后推一条 `tool_result`，且文本与**落库的 tool 消息正文完全一致**：
    两边各拼一份的话，界面显示的与喂回模型的内容会慢慢分叉（审计时看到的东西就不是
    模型当时看到的东西了）。这条断言就是"单一来源"的钉子。
    """
    store = _store()
    model = ScriptedModel([
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="c1", name="weather.get", arguments={"city": "上海"})],
            finish_reason="tool_calls"),
        ChatResponse(content="天晴。", finish_reason="stop"),
    ])
    sess = AgentSession(run_id="r-toolres", model=model, catalog=weather_tool(),
                        policy_engine=_policy(Decision.ALLOW), store=store)
    events = list(sess.stream("查上海天气"))
    types = [e["type"] for e in events]
    # 调用在前、结果在后，都在 final 之前
    assert types.index("tool") < types.index("tool_result") < types.index("final")
    ev = next(e for e in events if e["type"] == "tool_result")
    assert ev["name"] == "weather.get"
    assert ev["ok"] is True
    stored = [m for m in store.load_messages("r-toolres") if m.role == "tool"]
    assert [m.content for m in stored] == [ev["text"]]  # 事件文本 == 落库正文
    assert ev["text"].strip() != ""


def test_流式工具失败时tool_result带ok为假且正文带失败前缀() -> None:
    """失败的工具也要上屏（并且要一眼看出来失败了）。

    前端就是靠正文的失败前缀把气泡标成琥珀色的，前缀文案变了这套标注会静默失效，
    所以这里把它当契约钉住（前端 `lib/display.ts` 的判定配合使用）。
    """
    store = _store()
    model = ScriptedModel([
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="c1", name="nope.absent", arguments={})],
            finish_reason="tool_calls"),
        ChatResponse(content="换一招。", finish_reason="stop"),
    ])
    sess = AgentSession(run_id="r-toolres-fail", model=model, catalog=weather_tool(),
                        policy_engine=_policy(Decision.ALLOW), store=store)
    events = list(sess.stream("调一个不存在的工具"))
    ev = next(e for e in events if e["type"] == "tool_result")
    assert ev["ok"] is False
    assert ev["text"].startswith("[工具 nope.absent 执行失败")
    # 失败也照样落库，且与事件文本同源
    stored = [m for m in store.load_messages("r-toolres-fail") if m.role == "tool"]
    assert [m.content for m in stored] == [ev["text"]]


def test_跨上下文逐块推进流式会话不炸() -> None:
    """回归（来自真实浏览器 e2e 里抓到的 500）：**生成器每个 next() 都在不同的 context 里跑**。

    uvicorn/Starlette 会把同步生成器丢进线程池逐块 next()，每块各持一份 context 副本。
    所以任何“包住整个流式生成器”的 ContextVar set/reset 都会错配：

        ValueError: <Token var=<ContextVar name='warden_memory_owner'> ...>
                    was created in a different Context

    现场表现：真实浏览器连发第二条消息 → 服务端 500（“正式 chat 失败”）。修法是把
    owner_scope 从 stream() 挪到 _execute()（不跨 yield，详见 session.py 里的注释）。
    本用例用 copy_context() 逐块推进来复现那个时序：修复前会在推完最后一块时报错，
    修复后必须与单 context 版本产出完全一致的事件序列。
    """
    import concurrent.futures as cf
    import contextvars

    store = _store()
    model = StreamScriptedModel(deltas=["你", "好", "！"], final_text="你好！")
    sess = AgentSession(run_id="r-ctx", model=model, catalog=weather_tool(),
                        policy_engine=_policy(Decision.ALLOW), store=store)

    gen = sess.stream("你好")

    def advance() -> dict | None:
        # 每块都在“另一个 context”里推进：等价于服务器线程池里换了个工作线程
        return contextvars.copy_context().run(lambda: next(gen, None))

    events: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=1) as pool:
        while True:
            event = pool.submit(advance).result(timeout=30)
            if event is None:
                break
            events.append(event)

    assert [e["type"] for e in events] == ["start", "delta", "delta", "delta", "final"]
    assert [e["text"] for e in events if e["type"] == "delta"] == ["你", "好", "！"]
    assert sess.is_terminal()


def test_会话完成后新消息可开启新一轮() -> None:
    """回归：同一 run_id 多轮对话。第一轮完成后，第二条消息应重开执行周期，
    而不是在状态机上撞 COMPLETED -> WAITING_APPROVAL 非法转换（UI 曾报 500）。"""
    store = _store()
    model = ScriptedModel([
        ChatResponse(content="天晴。", finish_reason="stop"),        # 第一轮：直接回答
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="c2", name="weather.get", arguments={"city": "上海"})],
            finish_reason="tool_calls"),                            # 第二轮：想调 ASK 工具
        ChatResponse(content="已批准，执行完成。", finish_reason="stop"),  # 批准后：给结论
    ])
    sess = AgentSession(run_id="r-multi", model=model, catalog=weather_tool(),
                        policy_engine=_policy(Decision.ASK), store=store)

    first = sess.start("第一句")
    assert isinstance(first, FinalReply)
    assert sess.status() == RunStatus.COMPLETED

    second = sess.start("第二句")
    assert isinstance(second, NeedsApproval)  # 修复前：这里抛非法状态转换
    assert sess.status() == RunStatus.WAITING_APPROVAL
    final = sess.approve()
    assert isinstance(final, FinalReply)
    assert final.text == "已批准，执行完成。"
    assert sess.status() == RunStatus.COMPLETED


def test_流式会话完成后新消息可开启新一轮() -> None:
    """回归：流式路径（/chat/stream）没有 _run_loop 的终态拦截，
    第二条消息曾直接撞 COMPLETED -> WAITING_APPROVAL。"""
    store = _store()
    model = ScriptedModel([
        ChatResponse(content="第一轮结束。", finish_reason="stop"),
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="c2", name="weather.get", arguments={"city": "上海"})],
            finish_reason="tool_calls"),
        ChatResponse(content="第二轮结束。", finish_reason="stop"),
    ])
    sess = AgentSession(run_id="r-multi-stream", model=model, catalog=weather_tool(),
                        policy_engine=_policy(Decision.ASK), store=store)
    list(sess.stream("第一句"))
    assert sess.is_terminal()

    events = list(sess.stream("第二句"))
    types = [e["type"] for e in events]
    assert "needs_approval" in types  # 修复前：这里抛非法状态转换
    final = sess.approve()
    assert isinstance(final, FinalReply)
    assert final.text == "第二轮结束。"


def test_重启恢复后审批_消息序列合法() -> None:
    """回归：崩溃重启恢复会话时，system 应插在最前面（而不是追加到末尾），
    批准后发给模型的历史才是合法序列——否则 system 拆散 tool_call 配对，真实 API 400。"""

    class RecordingModel(AgentChatModel):
        def __init__(self, reply: ChatResponse) -> None:
            self.reply = reply
            self.seen: list[ChatRequest] = []

        def chat(self, request: ChatRequest) -> ChatResponse:
            self.seen.append(request)
            return self.reply

    store = _store()
    # 模拟断电残留：run 停在 WAITING_APPROVAL，库里只有 user + assistant(tool_call)，
    # 没有 tool 结果，也没有 system（system 每次启动才在内存补）
    run = AgentRun("r-sys")
    run.mark_queued()
    run.start()
    run.wait_for_approval()
    store.save_run(run)
    store.save_pending_approval("r-sys", "appr-1", "weather.get", {"city": "上海"}, "需审批")
    store.append_message("r-sys", Message(role="user", content="查天气"))
    store.append_message(
        "r-sys",
        Message(role="assistant", content="[调用工具 weather.get]",
                tool_call=ToolCall(id="c9", name="weather.get", arguments={"city": "上海"})),
    )

    model = RecordingModel(ChatResponse(content="已删除。", finish_reason="stop"))
    sess = AgentSession(run_id="r-sys", model=model, catalog=weather_tool(),
                        policy_engine=_policy(Decision.ALLOW), store=store)
    final = sess.approve()
    assert final.text == "已删除。"  # type: ignore[attr-defined]

    sent = model.seen[0].messages
    # system 必须在最前面（回归点：修复前被追加到末尾）
    assert sent[0].role == "system"
    # tool 结果紧跟 assistant(tool_call)，中间没有别的角色拆散配对
    pairs = [(m.role, (m.tool_call.id if m.tool_call else None)) for m in sent]
    assert pairs == [
        ("system", None),
        ("user", None),
        ("assistant", "c9"),
        ("tool", "c9"),
    ]


def test_悬空工具调用的历史发给模型前自动补齐() -> None:
    """回归：历史里 assistant(tool_call) 后面没有 tool 结果（曾被中断），
    下一次调模型前应自动补一条同 id 的合成 tool 结果，否则真实 API 400。"""

    class RecordingModel(AgentChatModel):
        """记录收到的 messages，用于断言补齐结果。"""

        def __init__(self, reply: ChatResponse) -> None:
            self.reply = reply
            self.seen: list[ChatRequest] = []

        def chat(self, request: ChatRequest) -> ChatResponse:
            self.seen.append(request)
            return self.reply

    store = _store()
    # 伪造一段被中断的历史：user + assistant(tool_call)，没有 tool 结果
    dangling = ToolCall(id="call-x", name="weather.get", arguments={"city": "上海"})
    store.save_run(AgentRun("r-dangling"))
    store.append_message("r-dangling", Message(role="user", content="查天气"))
    store.append_message(
        "r-dangling",
        Message(role="assistant", content="[调用工具 weather.get]", tool_call=dangling),
    )

    model = RecordingModel(ChatResponse(content="好的。", finish_reason="stop"))
    sess = AgentSession(run_id="r-dangling", model=model, catalog=weather_tool(),
                        policy_engine=_policy(Decision.ALLOW), store=store)
    final = sess.start("继续")
    assert final.text == "好的。"  # type: ignore[attr-defined]

    sent = model.seen[0].messages
    idx_call = next(
        i for i, m in enumerate(sent) if m.tool_call and m.tool_call.id == "call-x"
    )
    repair = sent[idx_call + 1]
    assert repair.role == "tool"
    assert repair.tool_call is not None and repair.tool_call.id == "call-x"
    assert "中断" in repair.content


