"""保留策略 / 迁移收口 / 静默失败指标 测试。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from warden_agent.core.metrics import metrics, note
from warden_agent.memory.models import MemoryContent, MemoryItem, MemoryScope, MemoryStatus, new_uid
from warden_agent.memory.store import SqliteMemoryStore
from warden_agent.runtime.maintenance import MaintenanceSweeper, sweep
from warden_agent.store.sqlite import SqliteStore

# ---------- 保留策略：事件裁剪 ----------


def test_事件按keep裁剪只留最近若干条(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "t.db")
    for i in range(5):
        store.append_event("run-1", f'{{"n":{i}}}', keep=3)
    rows = store.list_events_after("run-1", 0)
    assert len(rows) == 3, rows
    # 保留的是最近 3 条
    assert [p for _s, p in rows] == ['{"n":2}', '{"n":3}', '{"n":4}']
    store.close()


# ---------- 保留策略：清扫 ----------


def test_清扫删除过期幂等与陈旧限流(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "t.db")
    store.save_idempotent("k1", "payload")
    store.hit_rate_limit("bucket-1", 60, now=0.0)  # window_start=0（很早）

    counts = sweep(store, idempotency_ttl_s=0.0, rate_limit_max_age_s=0.0)
    assert counts["idempotency"] == 1
    assert counts["rate_limits"] == 1
    assert store.get_idempotent("k1") is None
    store.close()


def test_清扫不会误删未过期的幂等(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "t.db")
    store.save_idempotent("fresh", "payload")  # created_at = 现在
    counts = sweep(store, idempotency_ttl_s=86400.0)  # TTL 1 天 → 不该删
    assert counts["idempotency"] == 0
    assert store.get_idempotent("fresh") == "payload"
    store.close()


def test_清扫物理删除过期与墓碑记忆(tmp_path: Path) -> None:
    mem = SqliteMemoryStore(tmp_path / "m.db")
    item = MemoryItem(
        uid=new_uid(), scope=MemoryScope.USER, key="k",
        content=MemoryContent(text="过期事实"), owner="alice",
        status=MemoryStatus.TOMBSTONED,
    )
    mem.save(item)
    assert mem.purge_expired(dt.datetime.now(dt.UTC)) == 1
    assert mem.find(item.uid) is None
    mem.close()


def test_sweep对内存存储是安全空操作() -> None:
    from warden_agent.agent import InMemoryRunStore

    counts = sweep(InMemoryRunStore())
    assert counts == {"idempotency": 0, "rate_limits": 0, "memories": 0}


def test_清扫线程_间隔为零则不启动() -> None:
    from warden_agent.agent import InMemoryRunStore

    sweeper = MaintenanceSweeper(InMemoryRunStore(), interval_s=0)
    sweeper.start()
    assert sweeper.interval == 0
    sweeper.stop()  # 不启动也要能安全 stop


# ---------- 迁移收口 ----------


def test_老库打开后补列并记录schema版本(tmp_path: Path) -> None:
    """旧 schema（runs 无 updated_at/user_id）打开后应补列，并把版本记成 4。"""
    import sqlite3

    db = tmp_path / "old.db"
    raw = sqlite3.connect(str(db))
    raw.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, status TEXT NOT NULL)")
    raw.commit()
    raw.close()

    store = SqliteStore(db)
    try:
        assert store._has_column("runs", "updated_at")
        assert store._has_column("runs", "user_id")
        assert store.schema_version() == 4
    finally:
        store.close()


def test_schema版本是真实记录的() -> None:
    import tempfile

    store = SqliteStore(Path(tempfile.mkdtemp()) / "t.db")
    try:
        assert store.schema_version() == SqliteStore._SCHEMA_VERSION
    finally:
        store.close()


# ---------- 静默失败指标 ----------


def test_静默失败指标可被渲染() -> None:
    """`note()` 是各模块埋"静默失败"计数的入口；它必须出现在 /metrics 输出里。"""
    note("warden_test_silent_total", "测试用计数", 3)
    text = metrics().render()
    assert "warden_test_silent_total 3" in text


def test_真实告警规则引用的指标名都存在于代码() -> None:
    """告警规则里引用的 warden_* 指标，必须真的在代码里被定义——否则规则永远不响。"""
    from pathlib import Path as _P

    root = _P(__file__).resolve().parents[1]
    rules = (root / "deploy" / "observability" / "alerts" / "warden.rules.yml").read_text("utf-8")
    src = "\n".join(
        p.read_text(encoding="utf-8") for p in (root / "src").rglob("*.py")
    )
    for name in (
        "warden_lock_renew_failures_total",
        "warden_event_dropped_total",
        "warden_audit_write_failures_total",
        "warden_otlp_export_failures_total",
        "warden_otlp_dropped_total",
    ):
        assert name in rules, f"规则里没有 {name}"
        assert name in src, f"指标 {name} 在代码里没定义（规则永远不响）"
