"""LISTEN/NOTIFY 事件总线测试：把延迟从"轮询间隔"降到"通知到达"，且**不牺牲正确性**。

设计前提（测试围绕它展开）：
  通知只是提示，正确性仍然靠"落表 + 读表"。所以既要证明**变快了**，
  也要证明**丢通知不会丢事件**（否则就是把延迟换成了不可靠）。

两个容易写错、这里刻意避开的点：
  - **run_id 必须每次唯一**：真库是跨测试持久的，固定 id 会读到上一次遗留的事件；
  - **延迟要从"发布"量到"被唤醒"**：从 poll 开始量会把准备工作（例如 sleep）也算进去。

没有 PostgreSQL 时整体跳过（CI 里已起 PG service）。
"""

from __future__ import annotations

import contextlib
import os
import secrets
import threading
import time

import pytest

from warden_agent.web.coordination import (
    NOTIFY_CHANNEL,
    PostgresNotifyEventBus,
    SqlEventBus,
    coordination_for,
)

try:
    import psycopg
except ImportError:  # pragma: no cover - 没装 psycopg
    psycopg = None  # type: ignore[assignment]


def _pg_params() -> dict[str, object]:
    return {
        "host": os.environ.get("WARDEN_TEST_PG_HOST", "localhost"),
        "dbname": os.environ.get("WARDEN_TEST_PG_DB", "warden"),
        "user": os.environ.get("WARDEN_TEST_PG_USER", "postgres"),
        "password": os.environ.get("WARDEN_TEST_PG_PASSWORD", ""),
    }


def _pg_available() -> bool:
    if psycopg is None:
        return False
    try:
        conn = psycopg.connect(connect_timeout=2, **_pg_params())  # type: ignore[arg-type]
        conn.close()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _pg_available(),
    reason="需要可用的 PostgreSQL 才会运行（起法见 test_postgres_integration.py）",
)


def _rid() -> str:
    """每次唯一的 run_id（真库跨测试持久，固定 id 会串到上一次的数据）。"""
    return "notify-" + secrets.token_hex(6)


def _store():
    from warden_agent.store.postgres import PostgresStore

    return PostgresStore(**_pg_params())  # type: ignore[arg-type]


@pytest.fixture
def make_bus():
    """造一条通知总线，收尾时关掉**它和它背后的 store**。

    必须显式关：不关的话连接会在 GC 时抛 unraisable 警告，
    而本项目的 `filterwarnings = ["error"]` 会把警告当成失败。
    """
    created: list[object] = []

    def _make(**kw) -> PostgresNotifyEventBus:
        store = _store()
        bus = PostgresNotifyEventBus(store, **kw)
        created.extend([bus, store])
        return bus

    yield _make
    for obj in reversed(created):
        with contextlib.suppress(Exception):
            obj.close()


# ---------------------------------------------------------------------------
# 一、频道名不要漂移（SQL 里是字面量，常量只作事实源）
# ---------------------------------------------------------------------------


def test_监听与通知语句用的频道名和常量一致() -> None:
    """LISTEN/NOTIFY 不支持参数占位符，所以语句里是字面量；这里防止两处写岔。"""
    import inspect

    from warden_agent.web import coordination

    source = inspect.getsource(coordination.PostgresNotifyEventBus)
    assert f'"LISTEN {NOTIFY_CHANNEL}"' in source
    assert f'"NOTIFY {NOTIFY_CHANNEL}"' in source


# ---------------------------------------------------------------------------
# 二、正确性：事件不丢、按序、增量正确
# ---------------------------------------------------------------------------


def test_发布后能读到事件(make_bus) -> None:
    bus = make_bus()
    rid = _rid()
    bus.publish(rid, {"type": "delta", "text": "你好"})
    got = bus.poll(rid, 0, timeout=2.0)
    assert len(got) == 1
    assert got[0][1]["text"] == "你好" and got[0][0] > 0


def test_通知丢了也不会丢事件_退回轮询仍然拿得到(make_bus) -> None:
    """**关键**：通知只是提示。模拟"通知在订阅前就发出去了"（订阅端没听到），
    订阅仍然必须靠读表拿到事件。"""
    bus = make_bus()
    rid = _rid()
    bus._store.append_event(rid, '{"type":"delta","text":"只写表没喊"}')  # 模拟通知丢失
    got = bus.poll(rid, 0, timeout=2.0)
    assert len(got) == 1 and got[0][1]["text"] == "只写表没喊"


def test_按序且能增量读(make_bus) -> None:
    bus = make_bus()
    rid = _rid()
    for i in range(3):
        bus.publish(rid, {"n": i})
    first = bus.poll(rid, 0, timeout=2.0)
    assert [ev["n"] for _, ev in first] == [0, 1, 2]
    assert bus.poll(rid, first[-1][0], timeout=0.3) == []       # 没有新的
    bus.publish(rid, {"n": 3})
    rest = bus.poll(rid, first[-1][0], timeout=2.0)
    assert [ev["n"] for _, ev in rest] == [3]                    # 只拿增量


def test_坏JSON不会拖垮订阅(make_bus) -> None:
    bus = make_bus()
    rid = _rid()
    bus._store.append_event(rid, "这不是 JSON")
    bus.publish(rid, {"n": 1})
    got = bus.poll(rid, 0, timeout=2.0)
    assert [ev["n"] for _, ev in got] == [1]                     # 坏行跳过，好的照出


def test_另一个连接发布也能唤醒订阅(make_bus) -> None:
    """真实多副本形态：A 订阅、B（另一条 store 连接）发布 → A 应被唤醒并读到。"""
    bus = make_bus()
    rid = _rid()
    publisher = _store()          # 模拟另一个副本
    try:
        result: list[list[tuple[int, dict]]] = []
        listening = threading.Event()

        def subscribe() -> None:
            listening.set()
            result.append(bus.poll(rid, 0, timeout=5.0))

        t = threading.Thread(target=subscribe)
        t.start()
        listening.wait(1.0)
        time.sleep(0.3)                                  # 确保 LISTEN 已生效
        SqlEventBus(publisher).publish(rid, {"from": "replica-B"})
        t.join(5.0)
        assert result and result[0], "另一个连接发布后订阅端没拿到事件"
        assert result[0][0][1]["from"] == "replica-B"
    finally:
        publisher.close()


# ---------------------------------------------------------------------------
# 三、性能主张：从"发布"到"被唤醒"确实快于轮询间隔
# ---------------------------------------------------------------------------


def test_通知唤醒的延迟明显低于轮询间隔(make_bus) -> None:
    """这是"用 LISTEN/NOTIFY"的**全部理由**，所以要有数字而不是口号。

    量的是**发布 → 订阅端被唤醒**的耗时（不是 poll 的总时长——那里面包含准备时间；
    我第一版就是从 poll 开始量的，把 sleep 的 300ms 也算进去，得出"313ms 没变快"的假结论）。
    """
    bus = make_bus(poll_interval=0.25)
    rid = _rid()
    woke: list[float] = []
    ready = threading.Event()

    def subscribe() -> None:
        ready.set()
        bus.poll(rid, 0, timeout=5.0)
        woke.append(time.monotonic())

    t = threading.Thread(target=subscribe)
    t.start()
    ready.wait(1.0)
    time.sleep(0.3)                     # 让它进入"等通知"状态
    published_at = time.monotonic()
    bus.publish(rid, {"n": 1})
    t.join(5.0)

    assert woke, "订阅端没被唤醒"
    latency = woke[0] - published_at
    assert latency < 0.25, (
        f"从发布到被唤醒用了 {latency*1000:.0f}ms，不低于轮询间隔 250ms——那用 notify 就没意义了"
    )


# ---------------------------------------------------------------------------
# 四、装配：不满足条件要回落并告警，而不是假装用了通知
# ---------------------------------------------------------------------------


def test_装配_notify_模式对Postgres生效() -> None:
    store = _store()
    try:
        _idem, bus, _rl = coordination_for(store, shared=True, event_bus="notify")
        assert isinstance(bus, PostgresNotifyEventBus)
        if hasattr(bus, "close"):
            bus.close()
    finally:
        store.close()


def test_装配_poll_模式仍是轮询() -> None:
    store = _store()
    try:
        _idem, bus, _rl = coordination_for(store, shared=True, event_bus="poll")
        assert type(bus) is SqlEventBus
    finally:
        store.close()


def test_存储不支持监听连接时回落并告警(caplog: pytest.LogCaptureFixture) -> None:
    """能力不够要**明说**：假装用了通知，运维就会按"低延迟"去预期。"""
    class _NoNewConn:      # 具备共享状态方法，但没有 new_connection（例如 SQLite）
        def get_idempotent(self, key: str):  # noqa: ANN201
            return None

        def save_idempotent(self, key: str, payload: str) -> None:
            return None

        def append_event(self, run_id: str, payload: str) -> int:
            return 1

        def list_events_after(self, run_id: str, after_seq: int, limit: int = 200):  # noqa: ANN201
            return []

        def hit_rate_limit(self, bucket_key: str, window_seconds: int, now: float):  # noqa: ANN201
            return 1, now

    with caplog.at_level("WARNING"):
        _idem, bus, _rl = coordination_for(_NoNewConn(), shared=True, event_bus="notify")
    assert type(bus) is SqlEventBus
    assert any("notify" in r.message or "LISTEN" in r.message for r in caplog.records)
