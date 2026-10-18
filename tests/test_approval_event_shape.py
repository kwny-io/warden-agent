"""审批事件的形状必须与响应体同源——否则断线续读的卡片是空白的。

修前：`/chat` 与 `/approve` 的**响应体**里 `approval` 是完整字典
（`approval_id/tool_name/arguments/reason`），但同一个审批发到**总线**上时只有
`{"event": "needs_approval", "approval": "<tool_name 字符串>"}`。前端断线续读走的是总线
（`web/src/lib/api.ts` 把帧 `as Approval` 强转，`ChatView.tsx` 直接读 `ev.approval.tool_name`）
⇒ 工具名、参数、原因全是 `undefined`，页面不报任何错，用户只看到一张**什么都问的空白卡片**，
而且这张卡片是"run 停住等着"的唯一提示。

两个教训：
  1. 同一个领域对象的对外形状只能有**一处**定义（现在是 `ApprovalRequest.to_dict`）；
  2. TS 的 `as` 是编译期谎言——形状差异必须由服务端判据钉住，不能靠类型。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import httpx
import pytest
from tests.conftest import ScriptedModel, weather_tool

from warden_agent.model.model import ChatResponse, ToolCall
from warden_agent.policy.policy import Decision, PolicyEngine, PolicyResult
from warden_agent.runtime.session import ApprovalRequest
from warden_agent.store.sqlite import SqliteStore
from warden_agent.web.coordination import InProcessEventBus
from warden_agent.web.server import build_app

KEYS = {"approval_id", "tool_name", "arguments", "reason"}


def _ask_policy() -> PolicyEngine:
    engine = PolicyEngine()
    engine.add(lambda name, args: PolicyResult(Decision.ASK, "需要人工批准"))
    return engine


def _client(app) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _drain(bus: InProcessEventBus, run_id: str) -> list[dict]:
    seq, events = 0, []
    while True:
        items = bus.poll(run_id, seq, timeout=0.01)
        if not items:
            return events
        for s, ev in items:
            seq = s
            events.append(ev)


def test_审批形状只有一处定义() -> None:
    """`to_dict` 是唯一出口：字段名改了，这里先红。"""
    got = ApprovalRequest(
        approval_id="a1", tool_name="weather.get", arguments={"city": "上海"}, reason="要批准"
    ).to_dict()
    assert set(got) == KEYS
    args = got["arguments"]
    assert isinstance(args, dict) and args["city"] == "上海"


@pytest.mark.asyncio
async def test_chat_路径上的审批事件是完整字典() -> None:
    script = [
        ChatResponse(
            content=None,
            tool_calls=[ToolCall(id="c1", name="weather.get", arguments={"city": "上海"})],
            finish_reason="tool_calls",
        ),
    ]
    bus = InProcessEventBus()
    store = SqliteStore(Path(tempfile.mkdtemp()) / "t.db")
    app = build_app(
        model=ScriptedModel(script),
        catalog=weather_tool(),
        policy=_ask_policy(),
        store=store,
        event_bus=bus,
    )
    async with _client(app) as client:
        r = await client.post("/chat/run-shape", json={"text": "上海天气"})
    assert r.json()["kind"] == "needs_approval"

    events = [e for e in _drain(bus, "run-shape") if e.get("event") == "needs_approval"]
    assert len(events) == 1
    approval = events[0]["approval"]
    assert isinstance(approval, dict), "总线上的 approval 必须是字典，不能是 tool_name 字符串"
    assert set(approval) == KEYS
    assert approval["tool_name"] == "weather.get"
    assert approval["arguments"] == {"city": "上海"}
    store.close()


@pytest.mark.asyncio
async def test_approve_路径上的审批事件同样是完整字典() -> None:
    """两条独立的路由各自发事件，形状必须一模一样（前端只有一份渲染代码）。"""
    script = [
        ChatResponse(
            content=None,
            tool_calls=[ToolCall(id="c1", name="weather.get", arguments={"city": "上海"})],
            finish_reason="tool_calls",
        ),
        ChatResponse(
            content=None,
            tool_calls=[ToolCall(id="c2", name="weather.get", arguments={"city": "北京"})],
            finish_reason="tool_calls",
        ),
    ]
    bus = InProcessEventBus()
    store = SqliteStore(Path(tempfile.mkdtemp()) / "t.db")
    app = build_app(
        model=ScriptedModel(script),
        catalog=weather_tool(),
        policy=_ask_policy(),
        store=store,
        event_bus=bus,
    )
    async with _client(app) as client:
        assert (await client.post("/chat/run-shape2", json={"text": "上海天气"})).json()[
            "kind"
        ] == "needs_approval"
        assert (await client.post("/approve/run-shape2")).json()["kind"] == "needs_approval"

    approvals = [
        e["approval"] for e in _drain(bus, "run-shape2") if e.get("event") == "needs_approval"
    ]
    assert len(approvals) == 2
    for approval in approvals:
        assert isinstance(approval, dict)
        assert set(approval) == KEYS
    assert [a["arguments"]["city"] for a in approvals] == ["上海", "北京"]
    store.close()


def test_没有把tool_name字符串当审批发出去的地方() -> None:
    """源码级守卫：这类"少一层字段"的退化，grep 一眼就能看出。"""
    root = Path(__file__).resolve().parents[1] / "src" / "warden_agent" / "web"
    hits = [
        str(p.relative_to(root))
        for p in root.rglob("*.py")
        if "approval.tool_name}" in p.read_text(encoding="utf-8")
        or '"approval": outcome.approval.tool_name' in p.read_text(encoding="utf-8")
    ]
    assert hits == [], f"这些位置把审批投影成了 tool_name 字符串：{hits}"


@pytest.mark.asyncio
async def test_待审批队列与_chat_用的是同一份形状() -> None:
    """第三个出口：`GET /approvals`（审批队列）也必须由 `to_dict` 供字段。

    修前它是**手写字典**（run_id/approval_id/tool_name/arguments/reason 五个键），
    与 `to_dict()` 只是"恰好长得一样"：`to_dict` 加一个字段，队列这边就少一个，
    而前端两份数据喂给同一个面板（`ApprovalPanel` 用 `run_id`，`ChatView` 用其余四个）
    ——少一个字段不会报错，只会静默少显示一层信息。
    """
    script = [
        ChatResponse(
            content=None,
            tool_calls=[ToolCall(id="c1", name="weather.get", arguments={"city": "上海"})],
            finish_reason="tool_calls",
        ),
    ]
    store = SqliteStore(Path(tempfile.mkdtemp()) / "q.db")
    app = build_app(
        model=ScriptedModel(script),
        catalog=weather_tool(),
        policy=_ask_policy(),
        store=store,
        event_bus=InProcessEventBus(),
    )
    async with _client(app) as client:
        r = await client.post("/chat/run-queue", json={"text": "上海天气"})
        assert r.json()["kind"] == "needs_approval"
        q = (await client.get("/approvals")).json()

    row = next((x for x in q if x["run_id"] == "run-queue"), None)
    assert row is not None, "审批队列里没有这条挂起的 run"
    assert set(row) == KEYS | {"run_id"}, "队列形状与 to_dict 不同源（多/少字段）"
    # 值也必须一致：同一个审批在两处不能有两种画面。
    body_approval = r.json()["approval"]
    for key in KEYS:
        assert row[key] == body_approval[key], f"{key} 在队列与响应体里不一致"
    store.close()


def test_web_层不许手写审批字段() -> None:
    """源码级守卫：审批字段只允许在 `ApprovalRequest.to_dict` 里出现一次。

    第92 批只修了两个出口（`/chat`、`/approve`），第三个（`/approvals`）漏在外面——
    因为那里"只是碰巧长得一样"，没有任何判据会红。这条把它钉死。
    """
    root = Path(__file__).resolve().parents[1] / "src" / "warden_agent" / "web"
    hits = [
        str(p.relative_to(root))
        for p in root.rglob("*.py")
        if '"approval_id":' in p.read_text(encoding="utf-8")
        or "'approval_id':" in p.read_text(encoding="utf-8")
    ]
    assert hits == [], f"这些位置在 web 层手写审批字段（形状必须由 to_dict 供）：{hits}"
