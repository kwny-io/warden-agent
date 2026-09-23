"""存储保留策略与归属过滤回归：归属过滤下推到 SQL、限流清扫按窗口判断、时间归一化。

覆盖任务里的三处存储健壮性缺口：
  - list_runs(owner=...) 不能再"先 LIMIT 再应用层过滤"（会返回少于 n 条）；
  - purge_stale_rate_limits 不能只比固定 max_age（可能误删仍活跃的窗口）；
  - 保留/过期比较前把阈值归一化成 UTC-aware（naive datetime 会误判）。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from warden_agent.core.run.status import AgentRun
from warden_agent.credential.vault import StoredLease
from warden_agent.model.model import Message
from warden_agent.store.codec import normalize_utc_iso
from warden_agent.store.sqlite import SqliteStore

POSTGRES_PY = (
    Path(__file__).resolve().parents[1] / "src" / "warden_agent" / "store" / "postgres.py"
)


def _seed(store: SqliteStore, run_id: str, owner: str) -> None:
    run = AgentRun(run_id, user_id=owner)
    run.mark_queued()
    store.save_run(run)
    store.append_message(run_id, Message(role="user", content=f"{owner}:{run_id}"))


# ---------- P3：owner 过滤必须在 LIMIT 之前 ----------


def test_owner过滤下推到SQL_不受LIMIT截断(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "t.db")
    try:
        # alice 的消息先写（id 小），bob 的 5 条后写（id 大）→ 旧实现取 top-3 全是 bob
        for i in range(5):
            _seed(store, f"alice-{i}", "alice")
        for i in range(5):
            _seed(store, f"bob-{i}", "bob")

        res = store.list_runs(limit=3, owner="alice")
        assert len(res) == 3, "owner 名下还有 5 条，过滤后不该少于 limit"
        assert all(r["user_id"] == "alice" for r in res)

        # 不带 owner 时仍是最新 3 条（bob 的）
        assert {r["user_id"] for r in store.list_runs(limit=3)} == {"bob"}
    finally:
        store.close()


# ---------- P5：限流清扫按窗口判断 + 索引 ----------


def test_限流清扫不删仍可能活跃的窗口(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "t.db")
    try:
        now = 1_000_000.0
        # 窗口 1000s、50s 前开始 → 到 now+950 才结束，仍活跃。
        # 若只比固定 max_age（阈值 = now-10），旧实现会因 window_start < 阈值 而误删。
        store.hit_rate_limit("active", 1000, now=now - 50)
        assert store.purge_stale_rate_limits(before_epoch=now - 10) == 0
        assert store.hit_rate_limit("active", 1000, now=now)[0] == 2  # 还在累加，行确实还在
    finally:
        store.close()


def test_限流清扫删掉确实结束的窗口(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "t.db")
    try:
        now = 1_000_000.0
        store.hit_rate_limit("stale", 60, now=now - 100000)  # 窗口早已结束
        assert store.purge_stale_rate_limits(before_epoch=now) == 1
    finally:
        store.close()


def test_保留扫描索引已建立(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "t.db")
    try:
        names = {
            r[0] for r in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        assert {"idx_idempotency_created", "idx_rate_limits_window"} <= names
    finally:
        store.close()

    # PG 侧静态确认索引 DDL 确实存在（本地跑不了真库）
    source = POSTGRES_PY.read_text(encoding="utf-8")
    assert "idx_idempotency_created" in source
    assert "idx_rate_limits_window" in source


# ---------- P6：保留/过期比较前归一化为 UTC-aware ----------


def test_时间归一化把naive当UTC并换算偏移() -> None:
    assert normalize_utc_iso(dt.datetime(2026, 1, 1, 12, 0, 0)) == "2026-01-01T12:00:00+00:00"
    assert normalize_utc_iso("2026-01-01T12:00:00") == "2026-01-01T12:00:00+00:00"
    aware = dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.UTC)
    assert normalize_utc_iso(aware) == "2026-01-01T12:00:00+00:00"
    plus8 = dt.datetime(2026, 1, 1, 20, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    assert normalize_utc_iso(plus8) == "2026-01-01T12:00:00+00:00"


def test_幂等清扫把naive阈值归一化(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "t.db")
    try:
        store.save_idempotent("k", "payload")  # created_at = 现在（UTC-aware）
        past = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)).replace(tzinfo=None)
        assert store.purge_expired_idempotency(past.isoformat()) == 0
        future = (dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).replace(tzinfo=None)
        assert store.purge_expired_idempotency(future.isoformat()) == 1
    finally:
        store.close()


def test_租约清扫把naive_now归一化(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "t.db")
    try:
        now = dt.datetime.now(dt.UTC)
        store.save_credential_lease(StoredLease(
            scope="s", lease_id="l", name="n",
            issued_at=now, expires_at=now + dt.timedelta(hours=1),
        ))
        naive_now = now.replace(tzinfo=None)
        assert store.purge_expired_credential_leases("s", naive_now) == 0
        naive_future = (now + dt.timedelta(hours=2)).replace(tzinfo=None)
        assert store.purge_expired_credential_leases("s", naive_future) == 1
    finally:
        store.close()
