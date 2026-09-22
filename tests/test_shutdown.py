"""优雅停机：收到 SIGTERM 后要把资源**关掉**，而不是靠进程退出来"回收"。

此前 `build_app` 造的 FastAPI 应用**没有 lifespan**：uvicorn 会等在飞请求排空，
但我们持有的连接（主存储 / 事件总线 / 记忆库）没有任何地方关闭——
单副本影响有限，重启与滚动升级时更容易暴露（PG 侧留僵尸连接、文件锁场景更明显）。

这里直接驱动 `app.router.lifespan_context`（不需要额外依赖，也不依赖 ASGI 服务器），
验证"停机时确实关了"。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from tests.conftest import ScriptedModel, weather_tool

from warden_agent.memory.store import SqliteMemoryStore
from warden_agent.model.model import ChatResponse
from warden_agent.policy.policy import PolicyEngine
from warden_agent.store.sqlite import SqliteStore
from warden_agent.web.server import build_app


class _FakeBus:
    """最小事件总线替身：只记录 close 有没有被调用。"""

    def __init__(self) -> None:
        self.closed = False

    def publish(self, run_id: str, event: dict) -> None:  # type: ignore[type-arg]
        return None

    def poll(self, run_id: str, after_seq: int, timeout: float) -> list:  # type: ignore[type-arg]
        return []

    def close(self) -> None:
        self.closed = True


class _ExplodingBus:
    """close 会抛异常的总线：验证"关一个失败不影响关其余的"。"""

    def publish(self, run_id: str, event: dict) -> None:  # type: ignore[type-arg]
        return None

    def poll(self, run_id: str, after_seq: int, timeout: float) -> list:  # type: ignore[type-arg]
        return []

    def close(self) -> None:
        raise RuntimeError("模拟：事件总线关闭失败")


def _app(tmp_path: Path, *, bus: object, store: SqliteStore, mem: SqliteMemoryStore):  # type: ignore[no-untyped-def]
    return build_app(
        model=ScriptedModel([ChatResponse(content="好", finish_reason="stop")]),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=store,
        memory=True,
        memory_repository=mem,
        event_bus=bus,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_停机时关闭存储与事件总线(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "t.db")
    mem = SqliteMemoryStore(tmp_path / "m.db")
    bus = _FakeBus()
    app = _app(tmp_path, bus=bus, store=store, mem=mem)

    async with app.router.lifespan_context(app):
        # 服务中：存储可用
        assert store.load_run("no-such-run") is None

    assert bus.closed is True, "停机必须关闭事件总线（它可能持有独立的 LISTEN 连接）"
    # 主存储连接已关闭：再操作会抛 ProgrammingError（而不是静默可用）
    with pytest.raises(sqlite3.ProgrammingError):
        store.load_run("no-such-run")


@pytest.mark.asyncio
async def test_停机时关闭记忆库(tmp_path: Path) -> None:
    from warden_agent.memory import MemoryScope

    store = SqliteStore(tmp_path / "t.db")
    mem = SqliteMemoryStore(tmp_path / "m.db")
    app = _app(tmp_path, bus=_FakeBus(), store=store, mem=mem)

    async with app.router.lifespan_context(app):
        mem.search(MemoryScope.USER)  # 服务中可用

    with pytest.raises(sqlite3.ProgrammingError):
        mem.search(MemoryScope.USER)


@pytest.mark.asyncio
async def test_一个资源关失败不影响关闭其余的(tmp_path: Path) -> None:
    """停机路径里最忌讳"第一个 close 抛了，后面全不关"——连接就从这里漏出去。"""
    store = SqliteStore(tmp_path / "t.db")
    mem = SqliteMemoryStore(tmp_path / "m.db")
    app = _app(tmp_path, bus=_ExplodingBus(), store=store, mem=mem)

    async with app.router.lifespan_context(app):
        assert store.load_run("no-such-run") is None

    # 事件总线炸了，但主存储仍然被关掉
    with pytest.raises(sqlite3.ProgrammingError):
        store.load_run("no-such-run")


@pytest.mark.asyncio
async def test_正常服务不受lifespan影响(tmp_path: Path) -> None:
    """加了 lifespan 不能改坏正常路径：请求照旧、health 照旧。"""
    import httpx

    store = SqliteStore(tmp_path / "t.db")
    mem = SqliteMemoryStore(tmp_path / "m.db")
    app = _app(tmp_path, bus=_FakeBus(), store=store, mem=mem)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            assert (await c.get("/health/live")).status_code == 200
            r = await c.post("/chat/run-1", json={"text": "hi"})
            assert r.status_code == 200
