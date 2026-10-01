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

import os
import socket
import tempfile
import time
from pathlib import Path

import pytest

from warden_agent.runtime.locking import (
    DEFAULT_TTL_SECONDS,
    InProcessRunLock,
    RunLease,
    SqlRunLock,
    new_owner_id,
)
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
        # 等心跳跑一次。这里必须等**回调**而不能等 `lost` 标志：实现是先置 _lost、
        # 再打点/记日志、最后才调回调，中间那个窗口里 `lost` 已经是 True 而 `notified` 还是空。
        # 所以"轮询 lost 然后立刻断言回调"的写法是**竞态**：慢文件系统上必现
        # （容器里撞到过 assert [] == ['lost']，本机 Windows 上凑巧过）。
        deadline = time.monotonic() + 5.0
        while not notified and time.monotonic() < deadline:
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
        lock_ttl_seconds=SHORT_TTL,            # 显式传 float；心跳间隔 = TTL/3 = 0.2s
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


# ---------------------------------------------------------------------------
# 以下这一组是变异测试补出来的："代码跑到了"与"结果被断言了"是两回事。
#
# 2026-09-30 的 concurrency 分片原本只有 77.2%（47 个突变体存活），而这条链上的每一环
# ——owner 身份、过期边界、心跳间隔、线程属性、丢锁指标——都直接关系到
# "同一个 run 会不会被两个副本同时驱动"。下面把每一环的契约写死。
# ---------------------------------------------------------------------------


class _SpyLock:
    """记录"传进来的参数是什么"的假锁（真实实现在容器/真库里跑，这里只查契约）。"""

    ttl_seconds = 0.6          # 故意与默认值不同：能区分"取锁的 TTL"与"回退的默认 TTL"

    def __init__(self, *, renew_result: bool = True, renew_sleep: float = 0.0) -> None:
        self.acquires: list[float | None] = []
        self.renews: list[float | None] = []
        self.released: list[str] = []
        self._renew_result = renew_result
        self._renew_sleep = renew_sleep

    def acquire(self, run_id: str, owner: str, ttl_seconds: float | None = None) -> bool:
        self.acquires.append(ttl_seconds)
        return True

    def renew(self, run_id: str, owner: str, ttl_seconds: float | None = None) -> bool:
        self.renews.append(ttl_seconds)
        if self._renew_sleep:
            time.sleep(self._renew_sleep)
        return self._renew_result

    def release(self, run_id: str, owner: str) -> None:
        self.released.append(run_id)

    def owner_of(self, run_id: str) -> str | None:
        return None


class _LockWithoutTtl:
    """连 `ttl_seconds` 属性都没有的锁：验证回退值真的取了默认常量。"""

    def acquire(self, run_id: str, owner: str, ttl_seconds: float | None = None) -> bool:
        return True

    def renew(self, run_id: str, owner: str, ttl_seconds: float | None = None) -> bool:
        return True

    def release(self, run_id: str, owner: str) -> None:  # pragma: no cover - 不被调用
        pass

    def owner_of(self, run_id: str) -> str | None:  # pragma: no cover - 不被调用
        return None


def test_owner_id里能看出主机与进程且每次不同() -> None:
    """owner 是排查"是哪台机器的哪个进程持有"的唯一手段，格式不能退化。"""
    first = new_owner_id()
    # 主机名 + pid 必须是可读的真实值（"None:123:abcd" 这种等于把排查线索丢了）
    assert first.startswith(f"{socket.gethostname()}:{os.getpid()}:")
    # 同一进程连续两次不能撞（重启前后也不该撞）
    assert new_owner_id() != first


def test_边界_到期这一刻锁就失效() -> None:
    """过期判定是 `<= now` 还是 `< now` 决定了"多持有一次 TTL"还是"正好放掉"。

    用注入时钟把时间停在**恰好到期**的那一刻：竞态类缺陷最喜欢躲在这种边界上，
    而真实计时器永远给不出"正好等于"的时机。
    """
    now = [0.0]
    lock = InProcessRunLock(ttl_seconds=10, clock=lambda: now[0])
    assert lock.acquire("run-1", "A", ttl_seconds=10) is True

    now[0] = 10.0                                    # 恰好到期
    assert lock.owner_of("run-1") is None, "到期这一刻就不该再算持有"
    assert lock.acquire("run-1", "B", ttl_seconds=10) is True, "到期即可被接管"

    now[0] = 30.0                                    # B 的租约又恰好到期
    assert lock.renew("run-1", "B", ttl_seconds=10) is False, "过期后不能续租"


def test_回退TTL取自锁而不是常量() -> None:
    """不传 TTL 时应取**锁自己**的 TTL，且心跳间隔按 TTL 的三分之一。

    取错（取成默认常量、取错键名、键名大小写搞错）会让心跳间隔与锁的 TTL 脱钩：
    锁 0.6 秒过期、心跳却按 600 秒的节奏续——静默变成"几乎不续租"。
    """
    lease = RunLease(_SpyLock(), "run-1", "A")
    assert lease._ttl == 0.6
    assert lease._interval == pytest.approx(0.2)      # 0.6 / 3


def test_锁没有ttl属性时才回退到默认值() -> None:
    lease = RunLease(_LockWithoutTtl(), "run-1", "A")
    assert lease._ttl == DEFAULT_TTL_SECONDS


def test_显式TTL与间隔优先于回退值() -> None:
    lease = RunLease(_SpyLock(), "run-1", "A", ttl_seconds=2.0, interval_seconds=0.3)
    assert lease._ttl == 2.0                          # 显式值优先
    assert lease._interval == 0.3                     # "给了就一定用"，不能被默认值顶掉


def test_start取锁带的是租约的TTL且心跳线程可辨识() -> None:
    """取锁/续租都必须带**租约的** TTL（而不是 None 让锁自己猜）。

    线程属性也是契约：`daemon=True` 写错会让一个心跳线程拖住进程退出；
    `name` 让排查时能一眼认出是哪个 run 的租约。
    """
    lock = _SpyLock()
    lease = RunLease(lock, "run-1", "A", ttl_seconds=0.6, interval_seconds=0.05)
    assert lease.start() is True
    try:
        assert lock.acquires == [0.6]
        thread = lease._thread
        assert thread is not None
        assert thread.name == "run-lease-run-1"
        assert thread.daemon is True
        deadline = time.monotonic() + 3.0
        while not lock.renews and time.monotonic() < deadline:
            time.sleep(0.01)
        assert lock.renews, "心跳没跑起来"
        assert set(lock.renews) == {0.6}, "续租必须带租约自己的 TTL"
    finally:
        lease.stop()


def test_stop不会把调用方卡住即使续租还挂在里面() -> None:
    """stop() 的 join 上限是 2 秒——这个上限保护的是"清理/退出不被拖死"。

    这里让 renew 挂在里面 8 秒，于是这个上限的**上下两边**都要断言：
      - 下边：stop() 必须**等心跳让开**（不 join 就返回，锁还在别人的心跳下；
        而且心跳线程会在释放之后又把租约续回去）；
      - 上边：不能无限等（改成 join(None) / 调大上限就不满足这个契约）。
    """
    lock = _SpyLock(renew_sleep=8.0)
    lease = RunLease(lock, "run-1", "A", ttl_seconds=0.3, interval_seconds=0.05)
    assert lease.start() is True
    time.sleep(0.3)                                   # 让心跳进到 renew 里面
    started = time.monotonic()
    lease.stop()
    elapsed = time.monotonic() - started
    assert elapsed >= 1.5, f"stop 必须等心跳让开（上限 2 秒），实际只等了 {elapsed:.2f}s"
    assert elapsed < 2.5                              # 上限 2 秒 + 余量
    assert lease.acquired is False                    # 停完就必须是"没持有"
    assert lock.released == ["run-1"]


def test_丢锁时的指标名与日志能定位到run(monkeypatch, caplog) -> None:
    """丢锁的唯一可观测面是"指标 + 日志"：名字/说明/run/owner 都是运维契约。

    指标名变了告警规则就失效；日志里少了 run/owner 就查不出是哪个 run 被抢。
    这两件事都不会让任何功能出错，所以只有显式断言才会被守住。
    """
    import warden_agent.core.metrics as metrics_mod

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(metrics_mod, "note", lambda name, desc: calls.append((name, desc)))
    notified: list[str] = []
    lease = RunLease(
        _SpyLock(renew_result=False), "run-1", "A",
        ttl_seconds=0.3, interval_seconds=0.05,
        on_lost=lambda: notified.append("lost"),
    )
    with caplog.at_level("WARNING", logger="warden_agent.runtime.locking"):
        assert lease.start() is True
        try:
            deadline = time.monotonic() + 5.0
            while not notified and time.monotonic() < deadline:
                time.sleep(0.02)
        finally:
            lease.stop()
    assert lease.lost is True
    assert calls[:1] == [
        ("warden_lock_renew_failures_total", "Run 锁续租失败次数（丢锁）")
    ]
    lost_warnings = [
        r.getMessage() for r in caplog.records
        if r.name == "warden_agent.runtime.locking" and "租约续租失败" in r.getMessage()
    ]
    lost_prefix = "Run 租约续租失败：run=run-1 owner=A —— "
    assert lost_warnings, "丢锁必须留下告警（静默丢锁 = 两个副本抢同一个 run）"
    text = lost_warnings[0]
    assert text.startswith(lost_prefix), f"告警开头是排查契约：{text!r}"
    assert text.endswith("（不会再自动续租）"), f"告警结尾要说明后续行为：{text!r}"
