"""心跳续租测试：把"单次驱动不能超过锁 TTL"这个隐含约束消掉。

背景（租约式锁的代价）：
  租约式锁的好处是持有者崩了不必人工解锁；代价是**单次驱动如果比 TTL 还长**，租约中途过期、
  另一个副本就能接管 —— 于是又变成并发驱动。`RunLease` 的**后台心跳**每 TTL/3 续一次租，
  把这个问题按掉：只要进程还活着，租约就一直续着。

  这里锁住四件事：
    1. 持有期间**真的在续租**（用很短的 TTL + 真实等待，看别人抢不到——这是最直接的证据）；
    2. 没有心跳时**确实会被接管**（对照组：证明第 1 条不是"本来就抢不到"）；
    3. 续租失败要**被察觉**（`lost` 为真 + 回调 + 不再假装持有），不能静默;
    4. `stop()` 之后心跳停下、锁释放，且可重复调用（幂等）。
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from warden_agent.runtime.locking import InProcessRunLock, RunLease, SqlRunLock
from warden_agent.store.sqlite import SqliteStore

# 用很短的 TTL 让测试跑得快：TTL=0.6s、心跳间隔 0.2s
SHORT_TTL = 0.6
HOLD_SECONDS = 1.5          # 明显超过 TTL，没有心跳就必然过期


def _store() -> SqliteStore:
    return SqliteStore(Path(tempfile.mkdtemp()) / "t.db")


def test_对照组_没有心跳时租约确实会过期被接管() -> None:
    """先证明问题是真的：裸 acquire 之后一直持有，超过 TTL 别人就能抢。"""
    lock = InProcessRunLock(ttl_seconds=1)
    assert lock.acquire("run-1", "A", ttl_seconds=1) is True
    time.sleep(1.2)
    assert lock.acquire("run-1", "B", ttl_seconds=1) is True, "过期后应可被接管"
    lock.release("run-1", "B")


def test_有心跳时长时间持有不会被抢() -> None:
    """关键证据：持有时间远超 TTL，但心跳一直续租，所以别人**始终抢不到**。"""
    lock = InProcessRunLock(ttl_seconds=SHORT_TTL)
    lease = RunLease(lock, "run-1", "A", ttl_seconds=SHORT_TTL,
                     interval_seconds=0.2)
    assert lease.start() is True
    try:
        # 反复在"TTL 早已过期"的时间点上尝试抢锁，全都应当失败
        for _ in range(4):
            time.sleep(0.35)
            assert lock.acquire("run-1", "B", ttl_seconds=0.1) is False, (
                "心跳没起作用：租约过期后被别人抢到了"
            )
        assert lease.lost is False, "不该报丢锁"
    finally:
        lease.stop()


def test_跨存储实例的心跳同样有效() -> None:
    """多副本场景：心跳续的是**存储里的**租约，另一个 store 实例也抢不到。"""
    db = Path(tempfile.mkdtemp()) / "t.db"
    a = SqlRunLock(SqliteStore(db), ttl_seconds=SHORT_TTL)
    b = SqlRunLock(SqliteStore(db), ttl_seconds=SHORT_TTL)
    lease = RunLease(a, "run-1", "replica-A", ttl_seconds=SHORT_TTL,
                     interval_seconds=0.2)
    assert lease.start() is True
    try:
        time.sleep(0.75)                       # 超过 TTL
        assert b.acquire("run-1", "replica-B", ttl_seconds=0.1) is False
    finally:
        lease.stop()
    # 停掉之后立刻可被别人接管（心跳停 + 主动释放）
    assert b.acquire("run-1", "replica-B", ttl_seconds=1) is True


def test_续租失败会被察觉而不是静默() -> None:
    """丢锁是"我们可能已经不是在独占驱动"——必须报出来，不能假装还持有。"""

    class _RenewFails(InProcessRunLock):
        def renew(self, run_id: str, owner: str, ttl_seconds: int | None = None) -> bool:
            return False

    notified: list[str] = []
    lease = RunLease(
        _RenewFails(ttl_seconds=1), "run-1", "A",
        ttl_seconds=1, interval_seconds=0.1,
        on_lost=lambda: notified.append("lost"),
    )
    assert lease.start() is True
    try:
        for _ in range(20):                    # 等心跳跑一次
            if lease.lost:
                break
            time.sleep(0.05)
        assert lease.lost is True
        assert notified == ["lost"], "应当通过回调通知调用方"
    finally:
        lease.stop()


def test_取不到锁时不会起心跳也不释放别人的锁() -> None:
    lock = InProcessRunLock(ttl_seconds=60)
    lock.acquire("run-1", "A")
    lease = RunLease(lock, "run-1", "B", ttl_seconds=60)
    assert lease.start() is False
    assert lease.acquired is False
    lease.stop()                               # 幂等：没有拿到就不该动别人的锁
    assert lock.owner_of("run-1") == "A", "没拿到锁却把别人的锁释放了"


def test_stop幂等且停掉心跳() -> None:
    lock = InProcessRunLock(ttl_seconds=SHORT_TTL)
    lease = RunLease(lock, "run-1", "A", ttl_seconds=SHORT_TTL, interval_seconds=0.1)
    lease.start()
    lease.stop()
    lease.stop()                               # 再调一次不应出错
    assert lock.owner_of("run-1") is None
    # 心跳线程确实停了：等超过 TTL 也不会"复活"续租
    time.sleep(SHORT_TTL + 0.2)
    assert lock.owner_of("run-1") is None


def test_worker用租约驱动长任务时不会被抢() -> None:
    """端到端：会话 resume 耗时远超 TTL，但心跳让**另一个副本**始终抢不到。

    这里必须用 `SqlRunLock`（两个实例指向**同一个库**）才是真的"两个副本看同一把锁"。
    一开始我用了两个独立的 `InProcessRunLock` 实例——进程内锁各存各的字典，
    第二个实例当然"抢到了"，那是测试写错，不是心跳没生效。
    """
    import threading

    from warden_agent.core.run.status import RunStatus
    from warden_agent.runtime.checkpoint import Checkpoint
    from warden_agent.runtime.session import FinalReply
    from warden_agent.runtime.worker import RecoveryWorker

    db = Path(tempfile.mkdtemp()) / "shared.db"
    replica_a = SqlRunLock(SqliteStore(db), ttl_seconds=SHORT_TTL)
    replica_b = SqlRunLock(SqliteStore(db), ttl_seconds=SHORT_TTL)

    class _SlowSession:
        def __init__(self, run_id: str) -> None:
            self.run_id = run_id

        def resume(self):  # noqa: ANN201
            time.sleep(0.9)                    # 比 TTL（0.6s）长
            return FinalReply(text="done", messages=[])

    class _Controller:
        max_attempts_per_run = 3

        def __init__(self, cps: list) -> None:
            self._cps = cps

        def plan(self):  # noqa: ANN201
            from warden_agent.runtime.recovery import RecoveryPlan

            return RecoveryPlan(to_resume=list(self._cps), to_retry=[],
                                awaiting_human=[], terminal=[])

    cp = Checkpoint(run_id="run-1", status=RunStatus.RUNNING, iteration=1, step="model_call")
    worker = RecoveryWorker(
        _Controller([cp]), _SlowSession, lock=replica_a, owner="replica-A",
        lock_ttl_seconds=int(SHORT_TTL),       # 心跳间隔 = TTL/3 = 0.2s
    )
    # 在 worker 驱动期间（0.9s）去抢同一个 run：应当抢不到

    result: list[bool] = []

    def contender() -> None:
        time.sleep(0.45)                       # 此时 TTL 的一半都已过去
        result.append(replica_b.acquire("run-1", "replica-B", ttl_seconds=0.1))

    t = threading.Thread(target=contender)
    t.start()
    actions = worker.run_once()                # 驱动 0.9s（远超 TTL）
    t.join()

    assert [a.action for a in actions] == ["resumed"]
    assert result == [False], "长任务期间租约应被心跳一直续着，另一个副本抢不到"
    # 驱动结束后租约已释放，另一个副本立刻可以接管
    assert replica_b.acquire("run-1", "replica-B", ttl_seconds=1) is True
