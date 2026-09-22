"""跨副本协调状态测试：幂等 / 事件流 / 限流计数在共享存储下真正跨副本。

这组测试回答"水平扩展到底成不成立"：
  - 同一个 store 上起**两个 app 实例**（模拟两个副本），验证
    同一 Idempotency-Key 只执行一次、事件能被另一个副本订阅到、
    限流总额度不翻倍；
  - 对照组：`shared_state=False`（进程内）时，跨副本**不共享**（如实暴露边界）。
"""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path

import httpx
import pytest
from tests.conftest import ScriptedModel, weather_tool

from warden_agent.model.model import ChatResponse
from warden_agent.policy.policy import PolicyEngine
from warden_agent.store.sqlite import SqliteStore
from warden_agent.web.coordination import (
    InProcessEventBus,
    InProcessIdempotencyStore,
    InProcessRateLimitStore,
    SqlEventBus,
    SqlIdempotencyStore,
    SqlRateLimitStore,
    coordination_for,
)
from warden_agent.web.ratelimit import RateLimiter
from warden_agent.web.server import build_app


def _store() -> SqliteStore:
    return SqliteStore(Path(tempfile.mkdtemp()) / "shared.db")


def _app(store: SqliteStore, script=None, **kw):
    return build_app(
        model=ScriptedModel(script or [ChatResponse(content="好", finish_reason="stop")]),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=store,
        **kw,
    )


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


# ---------- 协调组件单元 ----------


def test_装配选择实现() -> None:
    store = _store()
    shared = coordination_for(store, shared=True)
    assert isinstance(shared[0], SqlIdempotencyStore)
    assert isinstance(shared[1], SqlEventBus)
    assert isinstance(shared[2], SqlRateLimitStore)

    local = coordination_for(store, shared=False)
    assert isinstance(local[0], InProcessIdempotencyStore)
    assert isinstance(local[1], InProcessEventBus)
    assert isinstance(local[2], InProcessRateLimitStore)


def test_装配在不支持共享的存储上回落并告警() -> None:
    class Bare:
        pass

    shared = coordination_for(Bare(), shared=True)  # 缺共享状态方法
    assert isinstance(shared[0], InProcessIdempotencyStore)


def test_幂等存储跨实例共享_且body往返不丢() -> None:
    store = _store()
    a, b = SqlIdempotencyStore(store), SqlIdempotencyStore(store)
    assert a.get("k") is None
    a.put("k", {"status_code": 200, "headers": {"X-A": "1"}, "body": b'{"ok":true}'})
    got = b.get("k")  # 另一个实例读得到
    assert got is not None
    assert got["status_code"] == 200
    assert got["body"] == b'{"ok":true}'  # bytes 经 base64 往返不丢


def test_事件总线跨实例共享() -> None:
    store = _store()
    a, b = SqlEventBus(store, poll_interval=0.01), SqlEventBus(store, poll_interval=0.01)
    a.publish("run-1", {"event": "final", "text": "hi"})
    a.publish("run-1", {"event": "done"})
    items = b.poll("run-1", 0, timeout=1.0)  # 另一个实例订阅到
    assert [ev["event"] for _s, ev in items] == ["final", "done"]
    # 增量：after_seq 之后没有新事件 → 超时返回空
    assert b.poll("run-1", items[-1][0], timeout=0.05) == []


def test_限流计数跨实例共享() -> None:
    store = _store()
    a, b = SqlRateLimitStore(store), SqlRateLimitStore(store)
    assert a.hit("caller:x", 60, 1000.0)[0] == 1
    assert b.hit("caller:x", 60, 1000.0)[0] == 2  # 另一个副本看到累计
    assert a.hit("caller:x", 60, 1001.0)[0] == 3
    # 窗口滚动后重置
    assert b.hit("caller:x", 60, 1070.0)[0] == 1


def test_限流计数并发不丢() -> None:
    store = _store()
    rl = SqlRateLimitStore(store)
    results: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(10):
            count, _ = rl.hit("k", 60, 1000.0)
            with lock:
                results.append(count)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == list(range(1, 51))  # 50 次命中，计数连续无丢失


# ---------- HTTP：两个副本共享一个库 ----------


@pytest.mark.asyncio
async def test_幂等跨副本_同一key只执行一次() -> None:
    store = _store()
    # 两个副本的模型故意给不同回答：若命中共享缓存，B 不会用自己的回答
    app_a = _app(store, [ChatResponse(content="A", finish_reason="stop")], shared_state=True)
    app_b = _app(store, [ChatResponse(content="B", finish_reason="stop")], shared_state=True)
    headers = {"Idempotency-Key": "idem-replica-1"}
    async with _client(app_a) as ca, _client(app_b) as cb:
        r1 = await ca.post("/chat/run-idem", json={"text": "hi"}, headers=headers)
        assert r1.status_code == 200
        assert r1.json()["text"] == "A"
        # 同一 key 打到另一个副本：命中共享缓存 → 返回 A（而非 B）
        r2 = await cb.post("/chat/run-idem", json={"text": "hi"}, headers=headers)
        assert r2.status_code == 200
        assert r2.json() == r1.json()


@pytest.mark.asyncio
async def test_幂等对照_进程内实现不跨副本() -> None:
    """shared_state=False（默认）时幂等表各存一份 —— 如实暴露单副本边界。"""
    store = _store()
    app_a = _app(store, [ChatResponse(content="A", finish_reason="stop")])  # 进程内
    app_b = _app(store, [ChatResponse(content="B", finish_reason="stop")])
    headers = {"Idempotency-Key": "idem-local-1"}
    async with _client(app_a) as ca, _client(app_b) as cb:
        r1 = await ca.post("/chat/run-l1", json={"text": "hi"}, headers=headers)
        assert r1.status_code == 200 and r1.json()["text"] == "A"
        # 副本 B 没有共享缓存 → 真的用自己的模型又执行了一次（返回 B）
        r2 = await cb.post("/chat/run-l1", json={"text": "hi"}, headers=headers)
        assert r2.status_code == 200
        assert r2.json()["text"] == "B"


@pytest.mark.asyncio
async def test_限流跨副本_总额度不翻倍() -> None:
    store = _store()
    limiter_a = RateLimiter(2, 60, store=SqlRateLimitStore(store))
    limiter_b = RateLimiter(2, 60, store=SqlRateLimitStore(store))
    app_a = _app(store, shared_state=True, rate_limiter=limiter_a)
    app_b = _app(store, shared_state=True, rate_limiter=limiter_b)
    async with _client(app_a) as ca, _client(app_b) as cb:
        assert (await ca.get("/status/a")).status_code == 200   # 全局第 1 次
        assert (await cb.get("/status/b")).status_code == 200   # 全局第 2 次
        r = await ca.get("/status/c")                           # 全局第 3 次 → 超限
        assert r.status_code == 429
        assert r.json()["errorCode"] == "RATE_LIMITED"


@pytest.mark.asyncio
async def test_事件流跨副本_另一副本能订阅到() -> None:
    store = _store()
    app_a = _app(store, shared_state=True)
    app_b = _app(store, shared_state=True)
    async with _client(app_a) as ca:
        await ca.post("/chat/run-ev", json={"text": "hi"})
    # 副本 B 订阅同一 run：事件已落共享表，应能读到 final
    async with _client(app_b) as cb:
        body = ""
        async with cb.stream("GET", "/events/run-ev") as resp:
            assert resp.headers["content-type"].startswith("text/event-stream")
            async for line in resp.aiter_lines():
                body += line + "\n"
                if '"final"' in body:
                    break
    assert '"final"' in body
    assert "好" in body


# ---------- 进程内事件总线的保留上限（可配 + 可观测）----------


def test_事件保留上限可配_且丢弃可观测() -> None:
    """`keep` 决定每个 run 保留多少条事件；超出会丢最早的，但**必须能被察觉**。"""
    bus = InProcessEventBus(keep=3)
    for i in range(5):
        bus.publish("run-1", {"n": i})

    items = bus.poll("run-1", after_seq=0, timeout=0.01)
    assert [ev["n"] for _seq, ev in items] == [2, 3, 4], "只应保留最近 3 条"
    assert bus.keep == 3
    assert bus.dropped_count("run-1") == 2, "丢弃条数要能被读到（而不是静默少几条）"


def test_事件被丢后_消费者能收到缺口提示(caplog) -> None:
    """消费者按 seq 前进时若跨过了被丢的区间，应给出"存在缺口"的日志。"""
    import logging

    bus = InProcessEventBus(keep=2)
    for i in range(4):
        bus.publish("run-2", {"n": i})       # 保留最近 2 条（seq 3、4）

    with caplog.at_level(logging.WARNING, logger="warden_agent.web.coordination"):
        got = bus.poll("run-2", after_seq=1, timeout=0.01)   # 要 seq>1，但最早只到 seq=3
    assert [seq for seq, _ev in got] == [3, 4]
    assert any("缺口" in r.message for r in caplog.records), "应提示事件流存在缺口"


def test_未超限时不丢事件也不报缺口() -> None:
    bus = InProcessEventBus(keep=10)
    for i in range(3):
        bus.publish("run-3", {"n": i})
    assert bus.dropped_count("run-3") == 0
    assert [ev["n"] for _s, ev in bus.poll("run-3", after_seq=0, timeout=0.01)] == [0, 1, 2]


def test_装配时能传入保留上限() -> None:
    _idem, bus, _rl = coordination_for(_store(), shared=False, event_keep=7)
    assert isinstance(bus, InProcessEventBus)
    assert bus.keep == 7

