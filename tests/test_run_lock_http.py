"""HTTP 路径的 Run 级锁测试：同一个会话被并发驱动时，第二个请求**被挡住**而不是一起写。

为什么这一层要单独测：
  `runtime/locking.py` 的锁语义、以及它在 `RecoveryWorker`（无人值守的自动恢复）里的接入
  已由 `tests/test_run_lock.py` 覆盖。但**HTTP 对话/审批路径**是另一条驱动 run 的路：
  `/chat/{run_id}`、`/chat/stream/{run_id}`、`/approve/{run_id}`、`/reject/{run_id}`
  都会推进会话状态。多副本下两个副本同时收到同一个 run 的 chat，没有闸门就会并发写、
  后写覆盖前写，而且不报错。

  这里锁住的行为：
    - 锁被别人持有 → **423**（不是 409：409 在本服务里已被用来表示"没有待审批的请求"）；
    - 锁释放后同一请求可以正常通过（单副本顺序请求完全不受影响）；
    - 审批路径同样受锁保护；
    - **流式**请求先取锁、整段 SSE 走完才释放（用记录锁时序的方式验证：
      `httpx.ASGITransport` 会把应用跑完才返回，观测不到"流中途"那一刻）；
    - 两个 app 共享一个存储（= 两个副本）时，一个持有、另一个被挡。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import httpx
import pytest
from tests.conftest import ScriptedModel, weather_tool

from warden_agent.model.model import ChatResponse
from warden_agent.policy.policy import PolicyEngine
from warden_agent.runtime.locking import InProcessRunLock, SqlRunLock
from warden_agent.store.sqlite import SqliteStore
from warden_agent.web.server import build_app


def _store() -> SqliteStore:
    return SqliteStore(Path(tempfile.mkdtemp()) / "t.db")


def _app(store: SqliteStore, lock=None):
    return build_app(
        model=ScriptedModel([ChatResponse(content="好", finish_reason="stop")] * 5),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=store,
        run_lock=lock,
    )


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


# ---------------------------------------------------------------------------
# 单副本：顺序请求不受影响；被占用时回 423
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_顺序对话不受锁影响() -> None:
    """单副本下每个请求各自取锁、结束即释放——行为与加锁前完全一致。"""
    app = _app(_store())
    async with _client(app) as c:
        for _ in range(3):
            r = await c.post("/chat/run-1", json={"text": "你好"})
            assert r.status_code == 200


@pytest.mark.asyncio
async def test_锁被别的副本持有时返回423() -> None:
    """模拟"另一个副本正在处理这个会话"：本副本必须拒绝，而不是跟着一起写。"""
    lock = InProcessRunLock(ttl_seconds=300)
    app = _app(_store(), lock=lock)
    # 外部（= 另一个副本）先占住 run-1
    assert lock.acquire("run-1", "other-replica") is True

    async with _client(app) as c:
        r = await c.post("/chat/run-1", json={"text": "你好"})
        assert r.status_code == 423
        # 统一 problem+json：状态码不变，但形状不再混 {"detail": ...}
        assert r.json()["errorCode"] == "RUN_LOCKED"
        assert "other-replica" in r.json()["detail"]
        # 别的 run 不受影响（锁是按 run_id 分的）
        assert (await c.post("/chat/run-2", json={"text": "你好"})).status_code == 200


@pytest.mark.asyncio
async def test_锁释放后同一请求可通过() -> None:
    lock = InProcessRunLock(ttl_seconds=300)
    app = _app(_store(), lock=lock)
    lock.acquire("run-1", "other-replica")
    async with _client(app) as c:
        assert (await c.post("/chat/run-1", json={"text": "x"})).status_code == 423
        lock.release("run-1", "other-replica")
        assert (await c.post("/chat/run-1", json={"text": "x"})).status_code == 200


@pytest.mark.asyncio
async def test_请求结束后锁被释放() -> None:
    """不释放的话这个会话就再也用不了了（要等 TTL）——所以跑完必须放。"""
    lock = InProcessRunLock(ttl_seconds=300)
    app = _app(_store(), lock=lock)
    async with _client(app) as c:
        assert (await c.post("/chat/run-1", json={"text": "x"})).status_code == 200
    assert lock.owner_of("run-1") is None


@pytest.mark.asyncio
async def test_失败的请求也会释放锁() -> None:
    """400/异常路径同样不能把锁漏掉，否则一次报错就把这个会话卡到 TTL 过期。"""
    class _Boom:
        def chat(self, request):  # noqa: ANN001, ANN201
            raise RuntimeError("模型炸了")

    lock = InProcessRunLock(ttl_seconds=300)
    app = build_app(
        model=_Boom(),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=_store(),
        run_lock=lock,
    )
    async with _client(app) as c:
        assert (await c.post("/chat/run-1", json={"text": "x"})).status_code == 400
    assert lock.owner_of("run-1") is None, "异常路径把锁漏掉了：这个会话要等 TTL 才能再用"


# ---------------------------------------------------------------------------
# 审批路径
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_审批路径也受锁保护() -> None:
    """`/approve`、`/reject` 会真正推进会话（甚至继续跑工具），同样是驱动 run。"""
    lock = InProcessRunLock(ttl_seconds=300)
    app = _app(_store(), lock=lock)
    lock.acquire("run-1", "other-replica")
    async with _client(app) as c:
        assert (await c.post("/approve/run-1")).status_code == 423
        assert (await c.post("/reject/run-1")).status_code == 423


# ---------------------------------------------------------------------------
# 流式：整段 SSE 期间持锁
# ---------------------------------------------------------------------------


class _SpyLock(InProcessRunLock):
    """记录 acquire/release 顺序的锁。

    为什么需要它：`httpx.ASGITransport` 会把 ASGI 应用**跑到完成**才把响应交回来，
    所以"流还在进行中"这个时刻在测试里根本观测不到（拿另一个 owner 去抢会发现锁已释放）。
    改用记录顺序来验证真正要保证的性质：**流式路径确实先取锁、SSE 收完之后才释放**。
    """

    def __init__(self) -> None:
        super().__init__(ttl_seconds=300)
        self.events: list[str] = []

    def acquire(self, run_id: str, owner: str, ttl_seconds: int | None = None) -> bool:
        got = super().acquire(run_id, owner, ttl_seconds)
        self.events.append("acquire" if got else "acquire-denied")
        return got

    def release(self, run_id: str, owner: str) -> None:
        self.events.append("release")
        super().release(run_id, owner)


@pytest.mark.asyncio
async def test_流式请求取锁后整段流结束才释放() -> None:
    lock = _SpyLock()
    app = _app(_store(), lock=lock)
    chunks: list[str] = []
    async with _client(app) as c, c.stream(
        "POST", "/chat/stream/run-1", json={"text": "你好"}
    ) as resp:
        assert resp.status_code == 200
        async for chunk in resp.aiter_text():
            chunks.append(chunk)

    assert chunks, "没收到任何 SSE 数据"
    assert lock.events == ["acquire", "release"], f"锁的时序不对：{lock.events}"
    assert lock.owner_of("run-1") is None


@pytest.mark.asyncio
async def test_流式请求抢不到锁时直接423() -> None:
    """取锁发生在返回 StreamingResponse 之前 —— 抢不到要在建流前就拒绝。"""
    lock = InProcessRunLock(ttl_seconds=300)
    app = _app(_store(), lock=lock)
    lock.acquire("run-1", "other-replica")
    async with _client(app) as c:
        r = await c.post("/chat/stream/run-1", json={"text": "你好"})
        assert r.status_code == 423


# ---------------------------------------------------------------------------
# 两个副本（共享存储）：一个持有、另一个被挡
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_两个副本共享库时互斥() -> None:
    """两个 app 挂在同一个库上 = 两个副本。A 在处理时 B 必须被挡住。

    这是"HTTP 路径也能安全多副本"的直接证据：锁不是进程内的，而是走存储。
    """
    db = Path(tempfile.mkdtemp()) / "t.db"
    lock_a = SqlRunLock(SqliteStore(db), ttl_seconds=300)
    lock_b = SqlRunLock(SqliteStore(db), ttl_seconds=300)
    app_b = _app(_store(), lock=lock_b)

    lock_a.acquire("run-1", "replica-A")          # A 正在处理
    async with _client(app_b) as c:
        assert (await c.post("/chat/run-1", json={"text": "x"})).status_code == 423
    lock_a.release("run-1", "replica-A")
    async with _client(app_b) as c:
        assert (await c.post("/chat/run-1", json={"text": "x"})).status_code == 200
