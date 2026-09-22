"""维护清扫的后台线程与失败隔离：`sweep` / `MaintenanceSweeper` 的真实行为。

已有 `test_maintenance.py` 覆盖了单次 `sweep` 的 SQL 效果；这里补**行为契约**：
  - `sweep` 用注入的 `now` 计算阈值、把阈值真正传给存储接口；
  - 某一步失败**只记日志**，其余步骤照常执行；
  - 后台线程按间隔真正跑起来，并在 `interval_s<=0` 时**不启动**；
  - 停机 `stop()` 能把线程收掉（且未启动时也安全）。
"""

from __future__ import annotations

import datetime as dt
import time
from pathlib import Path

from warden_agent.runtime.maintenance import (
    DEFAULT_IDEMPOTENCY_TTL_S,
    DEFAULT_RATE_LIMIT_MAX_AGE_S,
    MaintenanceSweeper,
    sweep,
)
from warden_agent.store.sqlite import SqliteStore


def test_默认保留常量是明确的() -> None:
    assert DEFAULT_IDEMPOTENCY_TTL_S == 86400.0
    assert DEFAULT_RATE_LIMIT_MAX_AGE_S == 86400.0


def test_sweep按注入now换算阈值并汇总各步计数() -> None:
    seen: dict[str, object] = {}

    class _Store:
        def purge_expired_idempotency(self, before_iso: str) -> int:
            seen["idem_before_iso"] = before_iso
            return 2

        def purge_stale_rate_limits(self, before_epoch: float) -> int:
            seen["rl_before_epoch"] = before_epoch
            return 1

    class _Mem:
        def purge_expired(self, moment: dt.datetime) -> int:
            seen["mem_moment"] = moment
            return 3

    now = dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.UTC)
    counts = sweep(
        _Store(),
        idempotency_ttl_s=3600,
        rate_limit_max_age_s=7200,
        memory_repository=_Mem(),
        now=now,
    )
    assert counts == {"idempotency": 2, "rate_limits": 1, "memories": 3}
    # TTL 真的从 now 往前推：≥1 小时前的创建时间才会被删
    assert seen["idem_before_iso"] == (now - dt.timedelta(seconds=3600)).isoformat()
    assert seen["rl_before_epoch"] == (now - dt.timedelta(seconds=7200)).timestamp()
    assert seen["mem_moment"] == now


def test_某一步失败只记日志_其余照常() -> None:
    """三重取向里的"尽力而为"：幂等表清扫炸了，限流与记忆不能被带崩。"""

    class _Bad:
        def purge_expired_idempotency(self, before_iso: str) -> int:
            raise RuntimeError("模拟幂等表损坏")

        def purge_stale_rate_limits(self, before_epoch: float) -> int:
            return 4

    counts = sweep(_Bad(), idempotency_ttl_s=0.0, rate_limit_max_age_s=0.0)
    assert counts["idempotency"] == 0     # 失败步骤记 0，不抛出
    assert counts["rate_limits"] == 4     # 后续步骤照跑


def test_记忆清扫失败也不影响其它步骤() -> None:
    class _Store:
        def purge_expired_idempotency(self, before_iso: str) -> int:
            return 1

        def purge_stale_rate_limits(self, before_epoch: float) -> int:
            return 2

    class _BadMem:
        def purge_expired(self, moment: dt.datetime) -> int:
            raise ValueError("模拟记忆库损坏")

    counts = sweep(_Store(), memory_repository=_BadMem(),
                   idempotency_ttl_s=0.0, rate_limit_max_age_s=0.0)
    assert counts == {"idempotency": 1, "rate_limits": 2, "memories": 0}


def test_没有任何清扫接口的对象是安全空操作() -> None:
    assert sweep(object()) == {"idempotency": 0, "rate_limits": 0, "memories": 0}


def test_后台线程按间隔真正清扫过期行(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "t.db")
    store.save_idempotent("stale", "payload")
    # TTL=0 → 刚写入的行也算过期，后台线程一跑就该删掉。
    sweeper = MaintenanceSweeper(store, interval_s=0.01, idempotency_ttl_s=0.0)
    assert sweeper.interval == 0.01
    sweeper.start()
    deadline = time.time() + 3.0
    while store.get_idempotent("stale") is not None and time.time() < deadline:
        time.sleep(0.02)
    sweeper.stop()
    try:
        assert store.get_idempotent("stale") is None, "后台线程应按间隔执行清扫"
    finally:
        store.close()


def test_后台线程遇清扫异常不退出_still可停() -> None:
    """后台线程绝不能因一轮异常就死掉——这里让它一直抛，仍应能干净停机。"""

    class _Boom:
        def purge_expired_idempotency(self, before_iso: str) -> int:
            raise RuntimeError("boom")

    sweeper = MaintenanceSweeper(_Boom(), interval_s=0.01, idempotency_ttl_s=0.0)
    sweeper.start()
    time.sleep(0.05)
    sweeper.stop(timeout=2.0)
    assert sweeper._thread is not None and not sweeper._thread.is_alive()  # noqa: SLF001


def test_间隔为零_不启动且stop安全() -> None:
    sweeper = MaintenanceSweeper(object(), interval_s=0)
    sweeper.start()
    assert sweeper.interval == 0
    sweeper.stop()
    assert sweeper._thread is None  # noqa: SLF001
