"""幂等并发 TOCTOU 回归。

原来的实现是"先查缓存 → 执行 → 再写缓存"，两个同 `Idempotency-Key` 的请求会在"查"处
都读到空，于是**各执行一次副作用**——这正是网关重试/多副本场景下幂等要防的事。
改法：执行前先**原子占位**（`reserve`），只有占到位的那个请求执行，另一个明确回 409。
"""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

import httpx
import pytest
from tests.conftest import weather_tool

from warden_agent.model.model import AgentChatModel, ChatRequest, ChatResponse
from warden_agent.policy.policy import PolicyEngine
from warden_agent.store.sqlite import SqliteStore
from warden_agent.web.coordination import InProcessIdempotencyStore
from warden_agent.web.server import build_app

# ---- 存储层：reserve 必须原子 ----


def test_占位是原子的_并发只有一个赢家() -> None:
    store = InProcessIdempotencyStore()
    wins: list[bool] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()  # 尽量让 8 个线程同时冲
        wins.append(store.reserve("k"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(wins) == 1, f"应当恰好一个占到，实际 {sum(wins)} 个"


def test_占位可释放_失败后允许重试() -> None:
    store = InProcessIdempotencyStore()
    assert store.reserve("k") is True
    assert store.reserve("k") is False
    store.release("k")                 # 执行失败 → 释放
    assert store.reserve("k") is True  # 现在又能占到了


def test_release_不会删掉已写好的响应快照() -> None:
    """释放只对"处理中"标记生效；已完成的结果不能被误删（否则幂等失效）。"""
    store = InProcessIdempotencyStore()
    store.reserve("k")
    store.put("k", {"status_code": 200, "headers": {}, "body": b'{"ok":true}'})
    store.release("k")
    assert store.get("k") is not None


# ---- HTTP 层：并发同 key 只有一个真正执行 ----


class _CountingModel(AgentChatModel):
    """慢一点、且记录被调用次数——用来暴露"执行了两次"。"""

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, request: ChatRequest) -> ChatResponse:
        self.calls += 1
        time.sleep(0.2)
        return ChatResponse(content=f"第{self.calls}次", finish_reason="stop")


@pytest.mark.asyncio
async def test_并发同key_只执行一次_另一个回409() -> None:
    model = _CountingModel()
    app = build_app(
        model=model, catalog=weather_tool(), policy=PolicyEngine(),
        store=SqliteStore(Path(tempfile.mkdtemp()) / "t.db"),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        headers = {"Idempotency-Key": "race-1"}
        r1, r2 = await _gather(
            c.post("/chat/run-race", json={"text": "hi"}, headers=headers),
            c.post("/chat/run-race", json={"text": "hi"}, headers=headers),
        )
    codes = sorted([r1.status_code, r2.status_code])
    assert codes == [200, 409], f"应一个成功、一个 409，实际 {codes}"
    assert model.calls == 1, f"副作用必须只发生一次，实际执行 {model.calls} 次"


async def _gather(*coros):  # type: ignore[no-untyped-def]
    import asyncio

    return await asyncio.gather(*coros)
