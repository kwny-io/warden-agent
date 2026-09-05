"""AgentLoop 执行循环测试。

覆盖基础循环行为 + 【loop 深度①】工具调用失败自恢复 + 【loop 深度②】记忆取舍。
"""
import pytest
from tests.conftest import ScriptedModel, weather_tool

from warden_agent.loop.loop import AgentLoop
from warden_agent.model.model import ChatResponse, ToolCall


def test_模型调用工具后给出结论() -> None:
    """完整走一次：模型先用工具，拿到结果后再给最终结论。"""
    model = ScriptedModel([
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="c1", name="weather.get", arguments={"city": "上海"})],
            finish_reason="tool_calls"),
        ChatResponse(content="今天晴，25度。", finish_reason="stop"),
    ])
    reply = AgentLoop(model=model, catalog=weather_tool()).run("上海天气？")
    assert "晴" in reply.text
    # 对话里应记录：工具调用 -> 工具结果 -> 最终结论
    assert "tool" in [m.role for m in reply.messages]


def test_模型想调未注册工具_喂回错误自恢复() -> None:
    """loop 深度①：模型调未注册工具时，不再直接崩，而是把错误喂回让它纠正。

    模型先乱调一个不存在的工具(收到错误)，然后改正用正确的工具，最终完成任务。
    """
    model = ScriptedModel([
        # 第一步：乱调一个没注册的工具（id 相同，代表"同一次尝试"）
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="c1", name="not.registered", arguments={"path": "/"})],
            finish_reason="tool_calls"),
        # 第二步：模型看到错误后，纠正成正确工具
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="c2", name="weather.get", arguments={"city": "上海"})],
            finish_reason="tool_calls"),
        # 第三步：给最终结论
        ChatResponse(content="天气晴，25度。", finish_reason="stop"),
    ])
    reply = AgentLoop(model=model, catalog=weather_tool()).run("上海天气？")
    assert "25度" in reply.text
    # 关键断言：对话里那条"错误喂回"消息在，证明失败被自恢复了
    error_msgs = [m for m in reply.messages if m.role == "tool" and "执行失败" in m.content]
    assert len(error_msgs) == 1
    assert "not.registered" in error_msgs[0].content


def test_工具失败超重试上限_放弃该工具并告知() -> None:
    """loop 深度①：同一工具反复失败超上限后，放弃该工具，明确让模型换招。"""
    model = ScriptedModel([
        # 连续两次用同一个不存在的工具（id 相同，同一尝试累计失败）
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="c1", name="never.works", arguments={})],
            finish_reason="tool_calls"),
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="c1", name="never.works", arguments={})],
            finish_reason="tool_calls"),
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="c1", name="never.works", arguments={})],
            finish_reason="tool_calls"),
        # 超上限后模型换招：改用存在的工具
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="c2", name="weather.get", arguments={"city": "北京"})],
            finish_reason="tool_calls"),
        ChatResponse(content="北京晴。", finish_reason="stop"),
    ])
    reply = AgentLoop(model=model, catalog=weather_tool(), max_tool_retries=2).run("北京天气")
    # 至少出现一次"放弃该工具"的提示
    give_up = [m for m in reply.messages if m.role == "tool" and "放弃该工具" in m.content]
    assert len(give_up) == 1
    # 模型最终用正确工具完成
    assert "北京晴" in reply.text


def test_工具内部抛异常_也喂回错误不崩() -> None:
    """loop 深度①：工具内部异常(非KeyError)同样被转成错误喂回，不击穿 loop。"""
    from warden_agent.tool.catalog import ToolCatalog, function_tool

    @function_tool("boom.tool", "会炸的工具", {"type": "object", "properties": {}})
    def boom() -> str:
        raise ValueError("内部出了错")

    catalog = ToolCatalog()
    catalog.register(boom)

    model = ScriptedModel([
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="c1", name="boom.tool", arguments={})],
            finish_reason="tool_calls"),
        ChatResponse(content="我意识到工具出错了。", finish_reason="stop"),
    ])
    reply = AgentLoop(model=model, catalog=catalog).run("试一下")
    assert "意识到" in reply.text
    # 错误信息被记录进对话（而不是崩溃）
    errs = [m for m in reply.messages if m.role == "tool" and "ValueError" in m.content]
    assert len(errs) == 1


def test_模型直接回答_不经过工具() -> None:
    """模型一次就直接回答，循环只走一轮就结束。"""
    model = ScriptedModel([ChatResponse(content="你好！", finish_reason="stop")])
    reply = AgentLoop(model=model, catalog=weather_tool()).run("你好")
    assert reply.text == "你好！"


def test_循环超过上限_视为失败() -> None:
    """模型一直要调工具不停 = 死循环，循环保护必须兜底报错。"""
    model = ScriptedModel([
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="c1", name="weather.get", arguments={"city": "上海"})],
            finish_reason="tool_calls"),
    ] * 20)  # 永远调工具，没有收尾
    with pytest.raises(RuntimeError, match="迭代超过上限"):
        AgentLoop(model=model, catalog=weather_tool(), max_iterations=3).run("hi")


# ---- 【loop 深度②】记忆取舍：按需取用 + 启发式写入判断 ----

def _make_memory_service():
    from warden_agent.memory import InMemoryMemoryStore, MemoryScope, MemoryService
    return MemoryService(InMemoryMemoryStore()), MemoryScope.SESSION


def test_记忆按需取用_注入相关上下文() -> None:
    """loop 深度②取用端：有 memory 时，检索相关记忆注入系统上下文(只取相关,不全量)。"""
    from warden_agent.memory import MemoryContent
    svc, scope = _make_memory_service()
    # 先记一条"用户偏好简体"——与"天气"无关,不应被"上海天气?"检索出来
    svc.approve(svc.propose(scope, "pref.lang", MemoryContent(text="用户喜欢简体中文")))
    # 再记一条与天气相关的
    svc.approve(svc.propose(scope, "city.preferred", MemoryContent(text="用户常问上海天气")))

    model = ScriptedModel([ChatResponse(content="好的。", finish_reason="stop")])
    loop = AgentLoop(model=model, catalog=weather_tool(), memory=svc)
    loop.run("上海天气怎么样")

    # 直接验证 recall_context 行为(按需取用)：命中的是"与天气相关"那条,不相关的没被取用
    ctx = loop._recall_context("上海天气怎么样")
    assert "上海" in ctx            # 命中了与天气相关的记忆
    assert "简体中文" not in ctx    # 不相关的没被取用(符合"按需取用")


def test_记忆写入_过短被取舍掉() -> None:
    """loop 深度②写入端：太短的内容(琐碎/临时)不配进记忆,被启发式过滤。"""
    from warden_agent.memory import InMemoryMemoryStore, MemoryService
    svc = MemoryService(InMemoryMemoryStore())
    loop = AgentLoop(model=ScriptedModel([ChatResponse(content="hi", finish_reason="stop")]),
                     catalog=weather_tool(), memory=svc)
    # 不足 6 字 → 不值得记 → 返回 False 且不产生任何记忆候选
    assert loop.remember("好") is False
    assert len(svc.pending()) == 0


def test_记忆写入_值得记则提议() -> None:
    """loop 深度②写入端：有意义的事实被记下(propose 进候选区)。

    断言只落到 loop 自己的"取舍启发式"上(返回值),不强依赖 memory 内部 pending 实现。
    """
    from warden_agent.memory import InMemoryMemoryStore, MemoryService
    svc = MemoryService(InMemoryMemoryStore())
    loop = AgentLoop(model=ScriptedModel([ChatResponse(content="hi", finish_reason="stop")]),
                     catalog=weather_tool(), memory=svc)
    # 值得记(长度适中) → 返回 True，说明它通过了"取舍"判断、走了 propose
    assert loop.remember("用户偏好用中文回复") is True
    # 对照：毫无信息量的短内容 → 被取舍掉
    assert loop.remember("嗯") is False


# ---- 【loop 深度④】上下文管理：超长裁剪 + 摘要 ----

def test_上下文超长_裁剪早期并留摘要() -> None:
    """loop 深度④：消息太多时,早期历史被压缩成摘要,最近窗口 + system 保留。"""
    from warden_agent.model.model import Message

    model = ScriptedModel([ChatResponse(content="收到。", finish_reason="stop")])
    loop = AgentLoop(model=model, catalog=weather_tool(), max_context_chars=200)

    # 造一段很长的历史(远超 200 字符),包含 system + 很多轮 user/assistant
    msgs = [Message(role="system", content="系统提示")]
    for i in range(20):
        msgs.append(Message(role="user", content=f"第{i}轮提问,这里有一段足够长的内容用来撑大体积"))
        msgs.append(Message(role="assistant", content=f"第{i}轮回答,对应的结论要点也较长一些"))

    managed = loop._manage_context(msgs)

    # system 仍在
    assert managed[0].role == "system"
    # 出现了摘要系统消息(早期对话被压缩成一句)
    assert any(m.role == "system" and "早期对话摘要" in m.content for m in managed)
    # 总字符降到阈值附近以下,不再超 200 太多
    total = sum(len(m.content or "") for m in managed)
    assert "第0轮" not in "".join(m.content for m in managed)  # 最早的被裁掉
    assert total < 200 * 3  # 大幅缩小(摘要 + 最近窗口)


def test_上下文不超长_原样返回() -> None:
    """loop 深度④：没超过阈值时不裁剪,原样透传。"""
    from warden_agent.model.model import Message

    model = ScriptedModel([ChatResponse(content="好。", finish_reason="stop")])
    loop = AgentLoop(model=model, catalog=weather_tool(), max_context_chars=10 ** 9)
    msgs = [Message(role="system", content="s"), Message(role="user", content="hi")]
    assert loop._manage_context(msgs) is msgs  # 同一对象,未裁剪


def test_上下文阈值0_不启用裁剪() -> None:
    """loop 深度④：max_context_chars=0(默认)表示不启用裁剪。"""
    from warden_agent.model.model import Message

    loop = AgentLoop(model=ScriptedModel([ChatResponse(content="好。", finish_reason="stop")]),
                     catalog=weather_tool())  # 默认 0
    msgs = [Message(role="system", content="s"), Message(role="user", content="x" * 5000)]
    assert loop._manage_context(msgs) is msgs  # 未裁剪


# ---- 【loop 深度⑤】意图判断:重复调用检测(防原地打转) ----

def test_同一工具重复调用_被意图判断拦下() -> None:
    """loop 深度⑤：模型反复发同一工具+参数(原地打转)时,第二次被"注意"提示拦下,
    不再重复执行,而是让它换策略或直接回答。"""
    model = ScriptedModel([
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="c1", name="weather.get", arguments={"city": "上海"})],
            finish_reason="tool_calls"),
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="c2", name="weather.get", arguments={"city": "上海"})],
            finish_reason="tool_calls"),
        ChatResponse(content="我今天就查这个。", finish_reason="stop"),
    ])
    reply = AgentLoop(model=model, catalog=weather_tool()).run("上海天气")
    # 出现"已调用过...不要再重复"的意图提示
    hints = [m for m in reply.messages if m.role == "tool" and "已调用过" in m.content]
    assert len(hints) == 1
    # 且 weather 工具实际执行只有 1 次(第二次被拦下)
    tool_msgs = [m for m in reply.messages
                 if m.role == "tool" and m.content == "上海: 晴, 25度"]
    assert len(tool_msgs) == 1
