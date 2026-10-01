"""流式对话的「断线」行为回归：心跳保活 + 断线不丢整轮回复。

为什么单独一个文件、而且不借用 httpx：
`httpx.ASGITransport` 会把整个响应体**缓冲**完再返回，根本走不到「客户端提前断开」
这条路径 —— 而这条路径恰恰是本文件要测的东西。所以这里直接按 ASGI 协议驱动应用：
自己当 `receive`/`send`，可以在收到第 N 个 body 帧后塞一个 `http.disconnect`，
与真实中间层（nginx / CDN / 浏览器切网）触发的是同一条代码路径。

三个被锁住的不变量：
1. 客户端断线 → 本轮照常跑完、最终回答照常落库、终态事件照常进事件总线
   （断线前丢掉的只是"搬运"，不是"这一轮"）。
2. 断线后租约与 SSE 并发名额**必须**归还，否则一次刷新页面就把自己锁在门外。
3. 空闲期发 SSE 注释行心跳，否则"零字节的长连接"会被中间层当死连接掐掉。
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest
from tests.conftest import ScriptedModel, weather_tool

from warden_agent.model.model import ChatRequest, ChatResponse
from warden_agent.policy.policy import PolicyEngine
from warden_agent.store.sqlite import SqliteStore
from warden_agent.web import server as server_mod
from warden_agent.web.coordination import InProcessEventBus
from warden_agent.web.server import build_app


class _SlowModel(ScriptedModel):
    """每次调用前先睡一会儿的脚本模型：用来制造「一段时间一个字节都不吐」的空闲期。

    真实的空闲期来自慢工具（拉镜像、跑测试）或长模型调用；这里只是把它变成确定性的。
    """

    def __init__(self, script: list[ChatResponse], delay: float) -> None:
        super().__init__(script)
        self.delay = delay

    def chat(self, request: ChatRequest) -> ChatResponse:
        time.sleep(self.delay)
        return super().chat(request)


def _app(
    script: list[ChatResponse],
    *,
    bus: InProcessEventBus | None = None,
    gate_limit: int | None = None,
    delay: float = 0.0,
) -> tuple[Any, SqliteStore]:
    store = SqliteStore(Path(tempfile.mkdtemp()) / "t.db")
    model = _SlowModel(script, delay) if delay else ScriptedModel(script)
    kwargs: dict[str, Any] = {}
    if bus is not None:
        kwargs["event_bus"] = bus
    if gate_limit is not None:
        kwargs["sse_max_concurrency"] = gate_limit
    app = build_app(
        model=model,
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=store,
        **kwargs,
    )
    return app, store


async def _drive(
    app: Any,
    path: str,
    payload: dict[str, Any],
    *,
    disconnect_after: int | None = None,
    timeout: float = 15.0,
) -> tuple[int, bytes]:
    """按 ASGI 协议跑一次请求，返回 (状态码, 收到的响应体)。

    `disconnect_after=N`：收到第 N 个非空 body 帧后，让 receive 立刻返回
    `http.disconnect` —— 等价于客户端在这一刻把连接掐了。
    """
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"test"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(json.dumps(payload))).encode()),
        ],
        "client": ("127.0.0.1", 45678),
        "server": ("test", 80),
    }
    body = json.dumps(payload).encode()
    request_sent = False
    disconnected = False
    disconnects: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    status: dict[str, int] = {}
    chunks: list[bytes] = []

    async def receive() -> dict[str, Any]:
        nonlocal request_sent, disconnected
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        if disconnected:
            return {"type": "http.disconnect"}
        return await disconnects.get()

    async def send(message: dict[str, Any]) -> None:
        nonlocal disconnected
        if message["type"] == "http.response.start":
            status["code"] = int(message["status"])
            return
        chunk = message.get("body", b"")
        if not chunk:
            return
        chunks.append(chunk)
        if disconnect_after is not None and len(chunks) >= disconnect_after:
            disconnected = True
            await disconnects.put({"type": "http.disconnect"})

    await asyncio.wait_for(app(scope, receive, send), timeout=timeout)
    return status.get("code", 0), b"".join(chunks)


async def _wait_for_final(bus: InProcessEventBus, run_id: str) -> list[tuple[int, dict[str, Any]]]:
    """等后台 worker 把终态事件发进总线（断线后它还在跑，所以要轮询等）。"""
    deadline = time.monotonic() + 15.0
    items: list[tuple[int, dict[str, Any]]] = []
    while time.monotonic() < deadline:
        items = bus.poll(run_id, 0, timeout=0.05)
        if any(ev.get("event") in ("final", "error") for _, ev in items):
            return items
        await asyncio.sleep(0.02)
    return items


def _frames(raw: bytes) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in raw.decode("utf-8").split("\n\n"):
        block = block.strip()
        if not block.startswith("data: "):
            continue
        out.append(json.loads(block[len("data: "):]))
    return out


@pytest.mark.asyncio
async def test_客户端断线后整轮回复仍然落库并能从事件总线续读() -> None:
    """断线只该停掉"搬运"，不该停掉"这一轮"。

    旧实现把会话驱动直接写在 HTTP 生成器里：客户端一断线，生成器被 close，
    整轮模型调用连同已经花掉的 token 一起丢掉，用户刷新页面看到的是空白。
    """
    bus = InProcessEventBus()
    app, store = _app(
        [ChatResponse(content="断线也要把这句说完", finish_reason="stop")], bus=bus
    )

    code, raw = await _drive(
        app, "/chat/stream/run-drop", {"text": "hi"}, disconnect_after=1
    )
    assert code == 200
    seen = _frames(raw)
    # 收到过开头就断开：证明"断线"发生在流中间，而不是请求没发出去
    assert seen and seen[0]["type"] == "start"
    assert all(f["type"] != "final" for f in seen), "断线后不该还能收到终态帧"

    items = await _wait_for_final(bus, "run-drop")
    finals = [ev for _, ev in items if ev.get("event") == "final"]
    assert finals, "客户端断线后，最终结果没有进事件总线（无法续读）"
    assert finals[-1]["text"] == "断线也要把这句说完"

    # 落库是硬要求：刷新页面后靠 /messages 恢复，靠的就是这条
    msgs = store.load_messages("run-drop")
    assistant = [m for m in msgs if m.role == "assistant"]
    assert [m.content for m in assistant] == ["断线也要把这句说完"]
    run = store.load_run("run-drop")
    assert run is not None and run.status.name == "COMPLETED"


@pytest.mark.asyncio
async def test_客户端断线后租约与并发名额都归还() -> None:
    """断线不能把自己锁在门外。

    租约与 SSE 名额的释放点原来也在 HTTP 生成器的 finally 里：客户端一断线就提前
    归还，等于给"同一个 run 并发写"开门；反过来若改成断线就不归还，一次刷新页面
    就会把自己永久卡成 423/503。正确行为是"由后台 worker 在本轮真正跑完时归还"。
    """
    app, store = _app(
        [
            ChatResponse(content="第一条", finish_reason="stop"),
            ChatResponse(content="第二条", finish_reason="stop"),
        ],
        gate_limit=1,
    )

    code, _raw = await _drive(
        app, "/chat/stream/run-lease", {"text": "hi"}, disconnect_after=1
    )
    assert code == 200

    # 等后台 worker 真正跑完（落库为证），再看名额/租约有没有还回来
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if any(m.role == "assistant" for m in store.load_messages("run-lease")):
            break
        await asyncio.sleep(0.02)
    assert any(m.role == "assistant" for m in store.load_messages("run-lease"))

    # 同一个 run 再来一轮：租约没还回来会 423（Run 被占用），名额没还回来会 503
    code2, raw2 = await _drive(app, "/chat/stream/run-lease", {"text": "再来一句"})
    assert code2 == 200, f"断线后租约/并发名额没归还，状态码 {code2}"
    assert [f for f in _frames(raw2) if f["type"] == "final"]


@pytest.mark.asyncio
async def test_空闲期会发心跳保活(monkeypatch: pytest.MonkeyPatch) -> None:
    """工具执行期间几十秒一个字节都不吐，中间层会把长连接当死连接掐掉。

    所以空闲超过阈值就发一条 SSE 注释行（`: keep-alive`）——它不改变事件语义，
    只让链路知道"还活着"。这里把阈值压到 50ms，并把模型调用拖到 0.4s 来稳定复现。
    """
    monkeypatch.setattr(server_mod, "SSE_HEARTBEAT_SECONDS", 0.05)
    app, _store = _app(
        [ChatResponse(content="慢死了", finish_reason="stop")], delay=0.4
    )

    code, raw = await _drive(app, "/chat/stream/run-beat", {"text": "hi"})
    assert code == 200
    heartbeats = raw.count(b": keep-alive")
    assert heartbeats > 0, "空闲期没有发心跳，长连接会被中间层掐掉"

    frames = _frames(raw)
    assert frames[0]["type"] == "start"
    assert frames[-1]["type"] == "final" and frames[-1]["text"] == "慢死了"


@pytest.mark.asyncio
async def test_心跳注释行不会被当成事件解析() -> None:
    """心跳是注释行，前端与 /events 的解析器都必须忽略它（不能让用户看到空气泡）。"""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(server_mod, "SSE_HEARTBEAT_SECONDS", 0.02)
    try:
        app, _store = _app(
            [ChatResponse(content="ok", finish_reason="stop")], delay=0.25
        )
        _code, raw = await _drive(app, "/chat/stream/run-beat2", {"text": "hi"})
    finally:
        monkeypatch.undo()

    assert b": keep-alive" in raw
    types = [f["type"] for f in _frames(raw)]
    assert types[0] == "start"
    assert "final" in types
    assert len(types) == 2, f"心跳被当成事件解析了：{types}"
