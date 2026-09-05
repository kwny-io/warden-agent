"""多 Agent（主管模式）测试。"""
from tests.conftest import ScriptedModel

from warden_agent.loop.loop import AgentLoop
from warden_agent.model.model import ChatResponse, ToolCall
from warden_agent.multiagent.supervisor import (
    build_supervisor,
    make_handoff,
    wrap_agent_as_tool,
)
from warden_agent.tool.catalog import ToolCatalog


def _single_reply_agent(text: str) -> AgentLoop:
    """造一个只会回固定一句话的子 Agent。"""
    model = ScriptedModel([ChatResponse(content=text, finish_reason="stop")])
    return AgentLoop(model=model, catalog=ToolCatalog())


def test_子agent被包装成工具() -> None:
    researcher = _single_reply_agent("调研结果：上海靠海，人口两千万。")
    tool = wrap_agent_as_tool(researcher, "research", "调研一个话题", "topic")

    catalog = ToolCatalog()
    catalog.register(tool)
    # 主管调 research -> 子 Agent run("上海") -> 返回它的回答
    result = catalog.execute("research", {"topic": "上海"})
    assert "上海" in str(result)


def test_主管先调研再成稿() -> None:
    """主管通过脚本化模型：先调 research，再调 write，最后给结论。"""
    researcher = _single_reply_agent("要点：上海是经济中心，有外滩。")
    writer = _single_reply_agent("成稿完成。")

    # 主管的"大脑"：第一步调 research，第二步调 write，第三步收尾
    supervisor_model = ScriptedModel([
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="c1", name="research", arguments={"topic": "上海"})],
            finish_reason="tool_calls"),
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="c2", name="write", arguments={"topic": "上海介绍"})],
            finish_reason="tool_calls"),
        ChatResponse(content="已交付：一篇上海介绍。", finish_reason="stop"),
    ])

    super_agent = build_supervisor(supervisor_model, researcher, writer)
    reply = super_agent.run("帮我写一篇上海介绍")
    assert "已交付" in reply.text


def test_make_handoff_结构化交接单() -> None:
    """交接单应包含角色/任务/结论三个核心字段，可被下一个 Agent 干净取用。"""
    note = make_handoff("Researcher", "上海经济", "GDP 全国前列，有外滩。")
    assert "交接单" in note
    assert "角色: Researcher" in note
    assert "任务: 上海经济" in note
    assert "结论: GDP 全国前列" in note


def test_make_handoff_可附加数据字段() -> None:
    note = make_handoff("Researcher", "年假", "每年 15 天", ("来源", "员工手册.pdf"))
    assert "来源: 员工手册.pdf" in note


def test_wrap_结构化交接() -> None:
    """默认 structured=True：包装的子 Agent 返回结构化交接单。"""
    researcher = _single_reply_agent("调研结果：公司年假 15 天。")
    tool = wrap_agent_as_tool(researcher, "research", "调研", "topic")
    catalog = ToolCatalog()
    catalog.register(tool)
    result = catalog.execute("research", {"topic": "年假"})
    assert "交接单" in str(result)
    assert "调研结果：公司年假 15 天。" in str(result)


def test_wrap_非结构化退化() -> None:
    """structured=False 时退化为一句话结论。"""
    researcher = _single_reply_agent("结论：支持。")
    tool = wrap_agent_as_tool(researcher, "research", "调研", "topic", structured=False)
    catalog = ToolCatalog()
    catalog.register(tool)
    result = catalog.execute("research", {"topic": "问题"})
    assert str(result) == "结论：支持。"



# ---- 容错降级：子 Agent 失败 → 重试 → 换备用 → 降级，不整体崩 ----

def _boom_agent() -> AgentLoop:
    """造一个一跑就抛运行时错误的子 Agent。"""
    import warden_agent.model.model as M

    class _Boom:
        def chat(self, request: M.ChatRequest) -> M.ChatResponse:
            raise RuntimeError("子 Agent 内部崩溃")

    return AgentLoop(model=_Boom(), catalog=ToolCatalog())


def test_子agent失败_默认降级不崩() -> None:
    tool = wrap_agent_as_tool(_boom_agent(), "research", "调研", "topic", retries=1)
    catalog = ToolCatalog()
    catalog.register(tool)
    result = str(catalog.execute("research", {"topic": "x"}))
    assert "[降级]" in result  # 返回降级交接单,不是抛异常
    assert "子 Agent" in result or "失败" in result


def test_子agent失败_换备用专员() -> None:
    tool = wrap_agent_as_tool(
        _boom_agent(), "research", "调研", "topic",
        retries=1, fallback=_single_reply_agent("备用专员顶上：结论B"),
    )
    catalog = ToolCatalog()
    catalog.register(tool)
    result = str(catalog.execute("research", {"topic": "x"}))
    assert "备用" in result
    assert "结论B" in result


def test_子agent失败_关闭降级则抛错() -> None:
    tool = wrap_agent_as_tool(_boom_agent(), "research", "调研", "topic",
                              retries=0, degrade=False)
    catalog = ToolCatalog()
    catalog.register(tool)
    try:
        catalog.execute("research", {"topic": "x"})
        assert False, "应抛出异常"
    except RuntimeError:
        pass


# ---- 共享工作记忆：研究员写入 → 写手读到 ----
def test_多agent共享workingmemory() -> None:
    from warden_agent.memory import (
        InMemoryMemoryStore,
        MemoryScope,
        MemoryService,
    )
    from warden_agent.multiagent.supervisor import share_memory

    researcher = _single_reply_agent("调研完成")
    writer = _single_reply_agent("成稿完成")
    svc = MemoryService(InMemoryMemoryStore())
    share_memory(svc, researcher, writer, scope=MemoryScope.WORKSPACE)

    # 研究员写入一条工作记忆
    researcher.catalog.execute("memory.remember",
                               {"key": "调研发现", "text": "年假是15天"})
    # 写手（另一个 AgentLoop，同一 WORKSPACE）能读到
    out = writer.catalog.execute("memory.recall", {"key": "调研发现"})
    assert "年假是15天" in str(out)
    # 写手的 recall_context 也能按需命中
    assert "年假" in writer._recall_context("我关心年假")


# ---- 并行/串行分派器 ----
def test_分派器_串行保序() -> None:
    from warden_agent.multiagent.dispatch import Dispatcher

    d = Dispatcher()
    steps = [("s1", _single_reply_agent("第一步")),
             ("s2", _single_reply_agent("第二步"))]
    res = d.run_sequential(steps, initial="启动")
    assert res.order == ["s1", "s2"]
    assert res.outputs["s1"] == "第一步"


def test_分派器_并行真并行() -> None:
    import time

    from warden_agent.multiagent.dispatch import DispatchedTask, Dispatcher

    class _Slow:
        def __init__(self, text: str, delay: float):
            self.text, self.delay = text, delay
        def chat(self, request):
            time.sleep(self.delay)
            return ChatResponse(content=self.text, finish_reason="stop")

    a = AgentLoop(model=_Slow("A", 0.4), catalog=ToolCatalog())
    b = AgentLoop(model=_Slow("B", 0.4), catalog=ToolCatalog())
    c = AgentLoop(model=_Slow("C", 0.4), catalog=ToolCatalog())

    d = Dispatcher()
    t0 = time.time()
    res = d.run_parallel([
        DispatchedTask("a", a, "t1"), DispatchedTask("b", b, "t2"),
        DispatchedTask("c", c, "t3"),
    ])
    elapsed = time.time() - t0
    assert set(res.outputs) == {"a", "b", "c"}
    # 3 个 0.4s 的任务若串行约 1.2s,真并行应明显 < 1.0s
    assert elapsed < 1.0, f"应当真并行(实际 {elapsed:.2f}s)"
