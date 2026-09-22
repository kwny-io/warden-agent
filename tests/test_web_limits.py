"""入站请求保护测试：请求体大小上限（413）与 SSE 长连接并发上限（503）。

两个上限都是"别把服务打垮"的闸门，必须在真实 ASGI 栈上验证：
  - 超限的 body → 413（先看 Content-Length，再看 chunked 的逐字节计数）；
  - 超过并发上限的 `/chat/stream` → 503（**不排队**，立刻拒绝）；
  - 正常请求 / 未达上限的流仍工作（闸门不能误伤）。

用 httpx.AsyncClient + ASGITransport 驱动（与 tests/test_web.py 同一套约定，不真开端口）。
并发上限的"占满名额"直接通过 `app.state.sse_gate` 占位来模拟：要真开一条长连接再发第二条，
得让响应停在半途（ASGITransport 会把响应缓冲完，做不到），而那又会踩到 `session.stream`
里 owner_scope 的线程上下文问题——那是与本闸门无关的**已有问题**，不在本次改动范围。
占位法测的是同一件事：名额满时端点回 503、释放后恢复。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import httpx
import pytest
from tests.conftest import ScriptedModel, weather_tool

from warden_agent.model.model import AgentChatModel, ChatResponse
from warden_agent.policy.policy import PolicyEngine
from warden_agent.store.sqlite import SqliteStore
from warden_agent.web.server import SseConcurrencyGate, build_app


def _app(model: AgentChatModel | None = None, **kwargs: object):
    store = SqliteStore(Path(tempfile.mkdtemp()) / "t.db")
    return build_app(
        model=model
        or ScriptedModel([ChatResponse(content="你好", finish_reason="stop")]),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=store,
        **kwargs,  # type: ignore[arg-type]
    )


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


@pytest.mark.asyncio
async def test_请求体超上限返回413() -> None:
    """Content-Length 已声明且超限 → 413（problem+json，与其它错误响应一致）。"""
    app = _app(max_request_bytes=100)
    async with _client(app) as client:
        resp = await client.post("/chat/run-big", json={"text": "x" * 500})
        assert resp.status_code == 413
        body = resp.json()
        assert body["errorCode"] == "PAYLOAD_TOO_LARGE"
        assert body["status"] == 413
        assert body["correlationId"]  # 统一错误响应带 correlation id


@pytest.mark.asyncio
async def test_未超限请求照常工作() -> None:
    """闸门不能误伤：body 在上限内的普通请求照常 200。"""
    app = _app(max_request_bytes=1000)
    async with _client(app) as client:
        resp = await client.post("/chat/run-ok", json={"text": "hi"})
        assert resp.status_code == 200
        assert resp.json()["kind"] == "final"


@pytest.mark.asyncio
async def test_chunked请求体无ContentLength也拦截() -> None:
    """无 Content-Length（分块传输）时，边读边计数：超限同样 413。"""

    async def _chunks():
        yield b"x" * 300

    app = _app(max_request_bytes=100)
    async with _client(app) as client:
        resp = await client.post(
            "/chat/run-chunk",
            content=_chunks(),  # 迭代器 body → 走 chunked，不带 Content-Length
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 413
        assert resp.json()["errorCode"] == "PAYLOAD_TOO_LARGE"


@pytest.mark.asyncio
async def test_sse并发超上限立刻返回503() -> None:
    """名额占满时 `/chat/stream` 立刻 503；释放名额后同一端点照常 200。"""
    app = _app(sse_max_concurrency=2)
    gate: SseConcurrencyGate = app.state.sse_gate
    assert gate.acquire() and gate.acquire()  # 模拟已有两条长连接在飞
    async with _client(app) as client:
        resp = await client.post("/chat/stream/run-full", json={"text": "hi"})
        assert resp.status_code == 503
        assert "上限" in resp.json()["detail"]
    gate.release()
    gate.release()
    async with _client(app) as client, client.stream(
        "POST", "/chat/stream/run-ok", json={"text": "hi"}
    ) as ok:
        assert ok.status_code == 200
        body = "".join([line async for line in ok.aiter_lines()])
        assert "final" in body


def test_sse闸门计数与释放() -> None:
    """闸门语义：到上限拒绝、释放后恢复、0 = 不限、多余的 release 不把计数打成负。"""
    gate = SseConcurrencyGate(1)
    assert gate.acquire() is True
    assert gate.acquire() is False  # 上限已到
    gate.release()
    assert gate.active == 0
    assert gate.acquire() is True

    unlimited = SseConcurrencyGate(0)
    for _ in range(5):
        assert unlimited.acquire() is True  # 0 = 关闭上限

    gate.release()
    gate.release()  # 多余的 release（幂等石旁）
    assert gate.active == 0
