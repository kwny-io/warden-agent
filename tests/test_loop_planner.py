"""loop 深度③ 阶段规划测试。"""
from tests.conftest import ScriptedModel, weather_tool

from warden_agent.loop.intent import ToolIntentRouter
from warden_agent.loop.loop import AgentLoop
from warden_agent.loop.planner import build_plan, is_complex, plan_as_context
from warden_agent.model.model import ChatResponse, ToolCall

# ---- Planner 本身 ----

def test_简单任务不拆阶段() -> None:
    plan = build_plan("上海天气怎么样")
    assert plan.is_complex is False
    assert plan.steps == []


def test_复杂任务被拆成阶段() -> None:
    plan = build_plan("调研三家公司并写对比报告，给出建议")
    assert plan.is_complex is True
    assert len(plan.steps) >= 3
    assert plan.steps[0].title  # 每阶段有标题
    assert plan.steps[0].goal  # 每阶段有目标


def test_复杂度判定_命中多个信号才算复杂() -> None:
    assert is_complex("上海天气") is False
    assert is_complex("调研A调研B调研C并汇总成报告") is True


def test_plan_as_context_渐进披露当前阶段() -> None:
    plan = build_plan("调研三家公司并写对比报告")
    ctx = plan_as_context(plan, current=1)
    # 当前阶段(下标1)带目标,且带全局进度
    assert "▶" in ctx or "2/" in ctx
    assert plan.steps[1].goal in ctx


def test_非复杂任务plan_as_context_为空() -> None:
    plan = build_plan("上海天气")
    assert plan_as_context(plan, 0) == ""


# ---- Planner 接入 loop ----

def test_planner_注入规划上下文_to_loop() -> None:
    """把 planner 挂进 AgentLoop:复杂任务的系统消息里会出现规划阶段。"""
    class _FakePlanner:
        def __init__(self) -> None:
            self.plan = build_plan("调研三家公司并写报告")
        def build(self, text: str):
            return self.plan
        def context(self, plan, current: int) -> str:
            return plan_as_context(plan, current)

    model = ScriptedModel([
        ChatResponse(content="已按阶段完成报告。", finish_reason="stop"),
    ])
    agent = AgentLoop(model=model, catalog=weather_tool(), planner=_FakePlanner())
    reply = agent.run("调研三家公司并写对比报告")
    # 系统消息里应包含规划上下文
    sys_msgs = [m.content for m in reply.messages if m.role == "system"]
    assert any("任务规划" in s or "调研与资料收集" in s for s in sys_msgs)


# ---- intent 路由器 ----

def test_intent_命中触发词_放行() -> None:
    router = ToolIntentRouter(triggers={"weather.get": ["天气", "气温"]})
    verdict = router.relay("weather.get", {"description": "查天气"},
                           "今天天气如何")
    assert verdict.action == "proceed"


def test_intent_无触发信号_提示() -> None:
    router = ToolIntentRouter(triggers={"weather.get": ["天气", "气温"]})
    verdict = router.relay("weather.get", {"description": "查天气"},
                           "今天心情不错")
    assert verdict.action == "hint"
    assert "意图提示" in verdict.message


def test_intent_领域触发词_放行() -> None:
    router = ToolIntentRouter(domains={"weather": ["天气", "气温", "降雨"]})
    verdict = router.relay("weather.get", {}, "明天会降雨吗")
    assert verdict.action == "proceed"


def test_intent_无触发器默认放行() -> None:
    router = ToolIntentRouter()  # 没配任何触发词 => 不误伤,默认放行
    verdict = router.relay("anything.tool", {}, "随便什么")
    assert verdict.action == "proceed"


def test_intent_关闭探测则全部放行() -> None:
    router = ToolIntentRouter(triggers={"weather.get": ["天气"]}, enable_probe=False)
    verdict = router.relay("weather.get", {}, "今天心情不错")
    assert verdict.action == "proceed"


# ---- intent 接入 loop ----

def test_intent_误调时喂回提示而不执行() -> None:
    """模型想调 weather.get,但当前问题没有天气信号,intent 提示它,工具不被执行则报错?不——
    这里验证的是:命中 hint 时,工具没被真正执行(没有成功工具结果)。"""
    router = ToolIntentRouter(triggers={"weather.get": ["天气", "气温"]})
    model = ScriptedModel([
        # 第一次:误调 weather.get(当前话题不含天气信号)
        ChatResponse(content=None, tool_calls=[
            _tc("c1", "weather.get", {"city": "上海"})], finish_reason="tool_calls"),
        # 第二次:模型收到提示后给最终回答
        ChatResponse(content="无需查天气,已直接回答。", finish_reason="stop"),
    ])
    agent = AgentLoop(model=model, catalog=weather_tool(), intent=router)
    reply = agent.run("今天心情不错")
    assert "已直接回答" in reply.text
    # 对话里应出现"意图提示"消息
    tool_msgs = [m.content for m in reply.messages if m.role == "tool"]
    assert any("意图提示" in m for m in tool_msgs)


def _tc(cid: str, name: str, args: dict) -> ToolCall:
    return ToolCall(id=cid, name=name, arguments=args)


# ---- 深度③升级：plan_with_model（模型生成阶段 + 降级）----

def test_plan_with_model_让模型生成阶段() -> None:
    """复杂任务下，模型返回的阶段会被采用（带 title/goal）。"""
    from warden_agent.loop.planner import plan_with_model
    from warden_agent.model.model import ChatRequest, ChatResponse

    class _Model:
        def chat(self, request: ChatRequest) -> ChatResponse:
            return ChatResponse(content=(
                '{"steps":[{"title":"查资料","goal":"收集三家公司的数据"},'
                '{"title":"写报告","goal":"形成对比报告"}]}'
            ), finish_reason="stop")

    plan = plan_with_model("调研三家公司并写对比报告", _Model())
    assert plan.is_complex is True
    assert len(plan.steps) == 2
    assert plan.steps[0].title == "查资料"
    assert plan.steps[0].goal  # 目标来自模型


def test_plan_with_model_简单任务不调模型() -> None:
    """简单任务直接走基础 loop，不浪费一次模型调用去"规划"。"""
    from warden_agent.loop.planner import plan_with_model
    from warden_agent.model.model import ChatRequest, ChatResponse

    class _Model:
        def chat(self, request: ChatRequest) -> ChatResponse:
            raise AssertionError("简单任务不该触发模型规划调用")

    plan = plan_with_model("上海天气怎么样", _Model())
    assert plan.is_complex is False
    assert plan.steps == []


def test_plan_with_model_模型返回垃圾_降级回模板() -> None:
    """模型返回无法解析的内容时，回退到通用模板，不崩。"""
    from warden_agent.loop.planner import plan_with_model
    from warden_agent.model.model import ChatRequest, ChatResponse

    class _BadModel:
        def chat(self, request: ChatRequest) -> ChatResponse:
            return ChatResponse(content="我不是JSON", finish_reason="stop")

    plan = plan_with_model("调研三家公司并写报告", _BadModel())
    assert plan.is_complex is True
    # 降级到模板：阶段非空
    assert len(plan.steps) >= 2


def test_plan_with_model_模型抛异常_降级() -> None:
    from warden_agent.loop.planner import plan_with_model
    from warden_agent.model.model import ChatRequest, ChatResponse

    class _BoomModel:
        def chat(self, request: ChatRequest) -> ChatResponse:
            raise RuntimeError("模型挂了")

    plan = plan_with_model("调研三家公司并写报告", _BoomModel())
    assert plan.is_complex is True
    assert len(plan.steps) >= 2


# ---- 深度⑤升级：intent reasoner（让模型说明理由）----

def test_intent_reasoner_模型说合理则放行() -> None:
    router = ToolIntentRouter(
        triggers={"weather.get": ["天气", "气温"]},
        reasoner=lambda *a: "用户想看天气实况，调用合理",
    )
    verdict = router.relay("weather.get", {}, "帮我看看此刻的地表温度实况")
    assert verdict.action == "proceed"
    assert "模型说明理由后放行" in verdict.reason


def test_intent_reasoner_模型说不合理则提醒() -> None:
    router = ToolIntentRouter(
        triggers={"weather.get": ["天气", "气温"]},
        reasoner=lambda *a: False,
    )
    verdict = router.relay("weather.get", {}, "今天晚饭吃什么")
    assert verdict.action == "hint"
    assert "模型复核后仍不确定" in verdict.reason


def test_intent_无reasoner_退回启发式() -> None:
    router = ToolIntentRouter(triggers={"weather.get": ["天气", "气温"]})
    verdict = router.relay("weather.get", {}, "今天晚饭吃什么")
    assert verdict.action == "hint"
    assert "意图提示" in verdict.message


def test_intent_reasoner_抛异常_退回提醒不崩() -> None:
    router = ToolIntentRouter(
        triggers={"weather.get": ["天气"]},
        reasoner=lambda *a: (_ for _ in ()).throw(RuntimeError("理由模型挂了")),
    )
    verdict = router.relay("weather.get", {}, "今天晚饭吃什么")
    assert verdict.action == "hint"
