"""验证认知能力真的接进了**会话路径**（HTTP / CLI），而不是只在 demo 的 AgentLoop 里。

背景：`AgentLoop`（demo / 评测 / 多 Agent 用）有阶段规划、意图路由、记忆按需取用、
上下文裁剪；会话侧 `AgentSession`（产品路径）另有一套循环，这四件事一件都没有，
所以 README 里"会思考的认知循环"在产品路径上当时是不成立的。

修法是把这四步抽到 `loop/cognition.py`，**两套循环共用同一份实现**。
这组测试钉住：

  1. 记忆按需取用：相关的记忆被注入请求；
  2. 阶段规划：复杂任务的阶段目标被注入请求；
  3. 意图路由：疑似误调的调用**不被执行**，而是提示模型；
  4. 上下文裁剪：超长历史被裁剪成摘要 + 最近窗口；
  5. **注入不污染存档**：记忆/计划是"每次请求临时注入"，不写进持久化的对话历史。

第 5 条容易漏：若不成立，恢复会话时注入内容会反复叠加。
"""

from __future__ import annotations

from typing import Any

from warden_agent.loop.intent import ToolIntentRouter
from warden_agent.model.model import AgentChatModel, ChatRequest, ChatResponse, ToolCall
from warden_agent.policy.policy import PolicyEngine
from warden_agent.runtime.session import AgentSession
from warden_agent.tool.catalog import ToolCatalog, function_tool


class CapturingModel(AgentChatModel):
    """记录每次请求的模型 —— 用来断言"到底给模型喂了什么"。"""

    def __init__(self, responses: list[ChatResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        return self._responses[min(len(self.requests) - 1, len(self._responses) - 1)]

    def systems(self, index: int = 0) -> list[str]:
        """第 index 次请求里的 system 消息内容。"""
        return [m.content for m in self.requests[index].messages if m.role == "system"]


class _Content:
    def __init__(self, text: str) -> None:
        self.text = text


class _Item:
    def __init__(self, key: str, text: str) -> None:
        self.key = key
        self.content = _Content(text)


class _StubMemory:
    """最小记忆桩：recall() 返回固定条目。"""

    def __init__(self, items: list[_Item]) -> None:
        self._items = items

    def recall(self, scope: Any, owner: Any = None) -> list[_Item]:
        # owner 是会话传下来的"归属者"；这个桩不做过筛，直接返回固定条目
        return self._items


class _StubPlanner:
    """最小规划器桩：始终判定为复杂任务，返回固定的阶段目标。"""

    def build(self, user_text: str) -> Any:
        return type("Plan", (), {"is_complex": True, "steps": ["一", "二"]})()

    def context(self, plan: Any, current: int) -> str:
        return "[阶段计划] 当前阶段：先检索资料"


class _Counter:
    """记录工具被真正执行了几次。"""

    def __init__(self) -> None:
        self.n = 0


def _session(
    model: AgentChatModel,
    *,
    catalog: ToolCatalog | None = None,
    memory: Any = None,
    planner: Any = None,
    intent: Any = None,
    max_context_chars: int = 0,
) -> AgentSession:
    from warden_agent.agent import InMemoryRunStore

    return AgentSession(
        run_id="run-cog",
        model=model,
        catalog=catalog or ToolCatalog(),
        policy_engine=PolicyEngine(),
        store=InMemoryRunStore(),
        memory=memory,
        memory_scope="session",
        planner=planner,
        intent=intent,
        max_context_chars=max_context_chars,
    )


# ---------- 1. 记忆按需取用 ----------


def test_会话路径会把相关记忆注入请求() -> None:
    model = CapturingModel([ChatResponse(content="好的", finish_reason="stop")])
    mem = _StubMemory([_Item("city", "用户常问上海天气")])
    _session(model, memory=mem).start("上海天气怎么样")

    assert any("用户常问上海天气" in s for s in model.systems()), (
        f"相关记忆应被注入，实际 system 消息：{model.systems()}"
    )


def test_不相关的记忆不注入() -> None:
    """按需取用：关键词不重叠的记忆不该塞进去（省 token、不干扰）。"""
    model = CapturingModel([ChatResponse(content="好的", finish_reason="stop")])
    mem = _StubMemory([_Item("food", "用户喜欢美式咖啡")])
    _session(model, memory=mem).start("今天天气如何")

    assert not any("美式咖啡" in s for s in model.systems())


# ---------- 2. 阶段规划 ----------


def test_会话路径会注入阶段计划() -> None:
    model = CapturingModel([ChatResponse(content="好的", finish_reason="stop")])
    _session(model, planner=_StubPlanner()).start("帮我把这个项目上线前的风险梳理一遍")

    assert any("阶段计划" in s for s in model.systems()), (
        f"复杂任务应注入阶段目标，实际：{model.systems()}"
    )


# ---------- 3. 意图路由（调用前校验） ----------


def _weather_catalog(counter: _Counter) -> ToolCatalog:
    @function_tool(
        "weather.get",
        "获取某个城市的实时天气与气温",
        {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
        pure=True,
    )
    def get_weather(city: str) -> str:
        counter.n += 1
        return f"{city}: 晴, 25度"

    catalog = ToolCatalog()
    catalog.register(get_weather)
    return catalog


def test_会话路径的意图路由会拦下明显误调() -> None:
    """产品路径上，"该不该调"的校验必须真的生效 —— 这是本轮接线的核心。"""
    counter = _Counter()
    catalog = _weather_catalog(counter)
    model = CapturingModel([
        ChatResponse(content=None, finish_reason="tool_calls",
                     tool_calls=[ToolCall(id="1", name="weather.get",
                                          arguments={"city": "公司"})]),
        ChatResponse(content="年假是 5 天", finish_reason="stop"),
    ])
    sess = _session(model, catalog=catalog, intent=ToolIntentRouter(catalog))
    sess.start("公司年假有几天")

    assert counter.n == 0, "被判定为误调的调用不应真的执行"
    assert any(m.role == "tool" and "意图提示" in (m.content or "")
               for m in sess.messages), "应把意图提示喂回模型"


def test_没有意图路由时该调用会被执行_形成对照() -> None:
    """对照组：不接 intent 时同样的输入会真的执行 —— 说明上面那条测的是有效果的。"""
    counter = _Counter()
    catalog = _weather_catalog(counter)
    model = CapturingModel([
        ChatResponse(content=None, finish_reason="tool_calls",
                     tool_calls=[ToolCall(id="1", name="weather.get",
                                          arguments={"city": "公司"})]),
        ChatResponse(content="年假是 5 天", finish_reason="stop"),
    ])
    _session(model, catalog=catalog).start("公司年假有几天")

    assert counter.n == 1, "没有意图路由时应照常执行"


# ---------- 4. 上下文裁剪 ----------


def test_会话路径会裁剪超长上下文() -> None:
    model = CapturingModel([ChatResponse(content="好的", finish_reason="stop")])
    sess = _session(model, max_context_chars=200)
    # 先灌一段很长的历史（走 store + messages）
    from warden_agent.model.model import Message

    for i in range(12):
        msg = Message(role="user", content=f"第{i}轮：" + "内容" * 30)
        sess.messages.append(msg)
    sess.start("现在总结一下")

    sent = model.requests[0].messages
    assert len(sent) < len(sess.messages), "发出去的上下文应比存档短"
    assert any("早期对话摘要" in (m.content or "") for m in sent), "被裁掉的部分应留下摘要"


# ---------- 5. 注入不污染存档 ----------


def test_注入的记忆与计划不写进存档() -> None:
    """它们是"每次请求临时注入"的；写进存档会导致恢复会话时反复叠加。"""
    model = CapturingModel([ChatResponse(content="好的", finish_reason="stop")])
    mem = _StubMemory([_Item("city", "用户常问上海天气")])
    sess = _session(model, memory=mem, planner=_StubPlanner())
    sess.start("上海天气怎么样")

    archived = "\n".join(m.content or "" for m in sess.messages)
    assert "用户常问上海天气" not in archived
    assert "阶段计划" not in archived
    # 但确实发出去了
    assert any("用户常问上海天气" in s for s in model.systems())
