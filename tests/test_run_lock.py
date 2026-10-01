"""Run 级锁测试：多副本下同一个 run **只能被一方驱动**。

为什么需要这道闸门：
  协调状态（幂等 / 事件流 / 限流计数）早已能共享，但"谁在驱动这个 run"一直没有闸门。
  同一个 `run_id` 若同时出现在两个副本的恢复计划里，两边都会去写状态与消息，
  结果是**后写覆盖前写**——历史分叉或丢失，而且不报错。此前项目里对这件事的说法是
  "建议做会话粘性"，等于把正确性交给部署方自觉。

  这里锁住三件事：
    1. 锁本身语义正确（互斥、过期可接管、只持有者能续/释放）；
    2. **跨 store 实例**（模拟两个副本）真的互斥——这条是关键，进程内锁做不到；
    3. `RecoveryWorker` 接上锁之后，抢不到的那个副本如实记为 `held_by_other` 并跳过。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from warden_agent.runtime.locking import (
    InProcessRunLock,
    SqlRunLock,
    new_owner_id,
    run_lock_for,
)
from warden_agent.runtime.worker import RecoveryWorker
from warden_agent.store.sqlite import SqliteStore


class _Clock:
    """可手动推进的时钟（租约过期要确定性地测）。"""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# 一、锁语义（进程内实现，等价于单副本默认行为）
# ---------------------------------------------------------------------------


def test_同一时刻只有一方能持有() -> None:
    lock = InProcessRunLock(ttl_seconds=60, clock=_Clock())
    assert lock.acquire("run-1", "replica-A") is True
    assert lock.acquire("run-1", "replica-B") is False
    assert lock.owner_of("run-1") == "replica-A"


def test_同一持有者重复取锁是幂等的() -> None:
    """同 owner 再取一次应当成功（否则自己重入就被自己挡住）。"""
    clock = _Clock()
    lock = InProcessRunLock(ttl_seconds=60, clock=clock)
    assert lock.acquire("run-1", "A") is True
    clock.advance(10)
    assert lock.acquire("run-1", "A") is True
    assert lock.owner_of("run-1") == "A"


def test_租约过期后可被接管() -> None:
    """持有者崩了不能让 run 永久锁死——这是选"租约"而不是硬锁的原因。"""
    clock = _Clock()
    lock = InProcessRunLock(ttl_seconds=60, clock=clock)
    assert lock.acquire("run-1", "A") is True
    clock.advance(61)
    assert lock.owner_of("run-1") is None
    assert lock.acquire("run-1", "B") is True
    assert lock.owner_of("run-1") == "B"


def test_只有持有者能续租() -> None:
    clock = _Clock()
    lock = InProcessRunLock(ttl_seconds=60, clock=clock)
    lock.acquire("run-1", "A")
    assert lock.renew("run-1", "A") is True
    assert lock.renew("run-1", "B") is False
    # 续租确实延后了过期时间
    clock.advance(50)
    assert lock.owner_of("run-1") == "A"


def test_过期后不能续租() -> None:
    clock = _Clock()
    lock = InProcessRunLock(ttl_seconds=60, clock=clock)
    lock.acquire("run-1", "A")
    clock.advance(61)
    assert lock.renew("run-1", "A") is False


def test_只能释放自己的锁() -> None:
    """防误删：别人刚接管的锁不能被原持有者释放掉。"""
    lock = InProcessRunLock(ttl_seconds=60, clock=_Clock())
    lock.acquire("run-1", "A")
    lock.release("run-1", "B")          # 不是持有者 → 不生效
    assert lock.owner_of("run-1") == "A"
    lock.release("run-1", "A")
    assert lock.owner_of("run-1") is None


def test_owner_id包含主机与进程信息() -> None:
    """格式 = 主机名:pid:8 位十六进制随机后缀，三段各自有用途，都要掐住。

    主机名定位机器、pid 定位进程、随机后缀保证"同一进程重启前后不撞"。
    后缀必须断言**长度与字符集**：`token_hex(5)`（长度变了）、`token_hex(None)`（直接抛错）
    这类改动"看起来还能用"，只有掐住长度/字符集才守得住；而随机后缀正是
    "同一进程重启后 owner 撞车 → 误判成自己还持有锁"的唯一防线。
    """
    owner = new_owner_id()
    host, pid, suffix = owner.rsplit(":", 2)
    assert host and host != "None", f"主机名不能退化：{owner!r}"
    assert pid == str(os.getpid()), f"pid 段必须是本进程：{owner!r}"
    assert len(suffix) == 8, f"随机后缀应是 token_hex(4) = 8 位，实际={suffix!r}"
    assert all(c in "0123456789abcdef" for c in suffix), f"后缀必须是十六进制：{suffix!r}"
    assert new_owner_id() != owner       # 不应重复


def test_边界_恰好到期这一刻就能被接管且不能续租() -> None:
    """`expires_at > now` 与 `expires_at <= now` 用不用等号，决定"恰好到期"这一瞬的归属：
    写松一点就是"多占一个 TTL"（另一个副本以为没人持有——并发驱动），
    写紧一点就是"还活着却被判定过期"。

    **关键在顺序**：这里故意不先调 `owner_of`。实现里 `owner_of` 会顺手删掉过期条目，
    先调它就把"已过期但条目还在"这个状态抹掉了，等号写错也照样通过——突变体正是靠这个活下来的。
    """
    clock = _Clock()
    lock = InProcessRunLock(ttl_seconds=60, clock=clock)
    assert lock.acquire("run-1", "A") is True

    clock.advance(60)                                  # 恰好到期（不早不晚）
    assert lock.acquire("run-1", "B") is True, "到期这一刻就该能接管（`> now` 才算还持有）"

    clock.advance(60)                                  # B 也恰好到期
    assert lock.renew("run-1", "B") is False, "到期这一刻就不该能续租（`<= now` 即已失效）"


# ---------------------------------------------------------------------------
# 二、跨 store 实例互斥（模拟两个副本 —— 这是进程内锁做不到的）
# ---------------------------------------------------------------------------


def test_两个副本共享一个库时互斥() -> None:
    """两个 SqlRunLock 挂在**同一个库**上 = 两个副本抢同一把锁。"""
    db = Path(tempfile.mkdtemp()) / "t.db"
    a = SqlRunLock(SqliteStore(db), ttl_seconds=60, clock=_Clock())
    b = SqlRunLock(SqliteStore(db), ttl_seconds=60, clock=_Clock())

    assert a.acquire("run-1", "replica-A") is True
    assert b.acquire("run-1", "replica-B") is False, "第二个副本不该抢到（会并发驱动同一个 run）"
    assert b.owner_of("run-1") == "replica-A"

    a.release("run-1", "replica-A")
    assert b.acquire("run-1", "replica-B") is True, "前一个释放后应当能接管"


def test_跨实例_过期后另一副本可接管() -> None:
    db = Path(tempfile.mkdtemp()) / "t.db"
    clock = _Clock()
    a = SqlRunLock(SqliteStore(db), ttl_seconds=60, clock=clock)
    b = SqlRunLock(SqliteStore(db), ttl_seconds=60, clock=clock)
    assert a.acquire("run-1", "A") is True
    clock.advance(61)                    # A 崩了，租约到期
    assert b.acquire("run-1", "B") is True
    assert b.owner_of("run-1") == "B"


def test_跨实例_续租与误释放的边界() -> None:
    db = Path(tempfile.mkdtemp()) / "t.db"
    a = SqlRunLock(SqliteStore(db), ttl_seconds=60, clock=_Clock())
    b = SqlRunLock(SqliteStore(db), ttl_seconds=60, clock=_Clock())
    a.acquire("run-1", "A")
    assert b.renew("run-1", "B") is False      # 非持有者不能续
    b.release("run-1", "B")                     # 非持有者释放无效
    assert b.owner_of("run-1") == "A"


# ---------------------------------------------------------------------------
# 三、装配
# ---------------------------------------------------------------------------


def test_shared为假时用进程内锁() -> None:
    assert isinstance(run_lock_for(SqliteStore(Path(tempfile.mkdtemp()) / "t.db"), shared=False),
                      InProcessRunLock)


class _LockableStore:
    """具备取锁方法的假存储：用来证明 run_lock_for **真的把 store 交下去了**。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def acquire_run_lock(self, run_id: str, owner: str, expires_at: float, now: float) -> bool:
        self.calls.append(("acquire", f"{run_id}/{owner}/{expires_at - now:.0f}"))
        return True

    def renew_run_lock(self, run_id: str, owner: str, expires_at: float, now: float) -> bool:
        self.calls.append(("renew", f"{run_id}/{owner}"))
        return True

    def release_run_lock(self, run_id: str, owner: str) -> None:
        self.calls.append(("release", f"{run_id}/{owner}"))

    def run_lock_owner(self, run_id: str, now: float) -> str | None:
        return "from-store"


def test_shared为真且存储支持时用共享锁且真的把存储交下去了() -> None:
    """只断言 `isinstance(lock, SqlRunLock)` 是不够的：`SqlRunLock(None)` 同样是实例。

    要守住的是"锁拿到的是**这个** store"——否则多副本下等于锁在一张不存在的表上，
    而且要到第一次取锁时才炸。另外 TTL 必须是"取锁时算的 now + ttl"，
    直接透传给 SQL 版才有多副本下的同一套过期语义。
    """
    store = _LockableStore()
    lock = run_lock_for(store, shared=True)
    assert isinstance(lock, SqlRunLock)
    assert lock.acquire("run-1", "A", ttl_seconds=60) is True
    assert store.calls == [("acquire", "run-1/A/60")], (
        f"取锁必须打到传进来的 store 上：{store.calls}"
    )
    assert lock.owner_of("run-1") == "from-store", "查询持有者也必须走这个 store"


def test_shared为真但存储不支持时回落并告警(caplog: pytest.LogCaptureFixture) -> None:
    """能力不够要**明说**，不能假装多副本下是安全的。

    告警是这件事唯一的可观测面，所以三个信息都要守住：
    打在哪个 logger（过滤/排查靠它）、说明里带上**存储的真实类型名**
    （否则看到告警也不知道该换哪个存储）、以及结尾给出的正确做法。
    """
    class _BareStore:  # 只有 RunStore 的一部分能力，没有取锁方法
        pass

    with caplog.at_level("WARNING"):
        lock = run_lock_for(_BareStore(), shared=True)
    assert isinstance(lock, InProcessRunLock)
    warnings = [r for r in caplog.records if "Run 锁" in r.getMessage()]
    assert warnings, f"回落必须告警，实际记录={[r.getMessage() for r in caplog.records]}"
    record = warnings[0]
    assert record.name == "warden_agent.runtime.locking", (
        f"告警要打在模块自己的 logger 上，实际={record.name}"
    )
    message = record.getMessage()
    assert message.startswith("请求共享 Run 锁，但存储 "), f"告警开头是排查契约：{message!r}"
    assert "_BareStore" in message, f"告警里要能看出实际拿到的是什么存储：{message!r}"
    assert message.endswith("请改用 SqliteStore / PostgresStore"), (
        f"告警结尾要给出正确做法：{message!r}"
    )


# ---------------------------------------------------------------------------
# 四、接进 RecoveryWorker：抢不到就不驱动
# ---------------------------------------------------------------------------


class _StubSession:
    """假会话：记录被 resume 了几次，其余一概不关心。"""

    def __init__(self, run_id: str, log: list[str]) -> None:
        self.run_id = run_id
        self._log = log

    def resume(self):  # noqa: ANN201 - 测试替身
        from warden_agent.runtime.session import FinalReply

        self._log.append(self.run_id)
        return FinalReply(text="resumed", messages=[])


class _StubController:
    """假控制器：把给定的一批 Checkpoint 当作"该续跑的"。"""

    max_attempts_per_run = 3

    def __init__(self, checkpoints: list) -> None:
        self._checkpoints = checkpoints

    def plan(self):  # noqa: ANN201 - 测试替身
        from warden_agent.runtime.recovery import RecoveryPlan

        return RecoveryPlan(
            to_resume=list(self._checkpoints), to_retry=[], awaiting_human=[], terminal=[]
        )


def _checkpoint(run_id: str):
    from warden_agent.core.run.status import RunStatus
    from warden_agent.runtime.checkpoint import Checkpoint

    return Checkpoint(run_id=run_id, status=RunStatus.RUNNING, iteration=1, step="model_call")


def test_另一个副本持有锁时本副本跳过并如实记录() -> None:
    db = Path(tempfile.mkdtemp()) / "t.db"
    clock = _Clock()
    shared_a = SqlRunLock(SqliteStore(db), ttl_seconds=300, clock=clock)
    shared_b = SqlRunLock(SqliteStore(db), ttl_seconds=300, clock=clock)

    # 副本 A 已经拿着 run-1
    assert shared_a.acquire("run-1", "replica-A") is True

    driven: list[str] = []
    worker = RecoveryWorker(
        _StubController([_checkpoint("run-1")]),
        lambda rid: _StubSession(rid, driven),
        lock=shared_b,
        owner="replica-B",
    )
    actions = worker.run_once()

    assert [a.action for a in actions] == ["held_by_other"]
    assert "replica-A" in actions[0].detail
    assert driven == [], "抢不到锁却仍然驱动了 run —— 这正是要防的并发写"


def test_抢到锁才驱动且执行完会释放() -> None:
    db = Path(tempfile.mkdtemp()) / "t.db"
    lock = SqlRunLock(SqliteStore(db), ttl_seconds=300, clock=_Clock())
    driven: list[str] = []
    worker = RecoveryWorker(
        _StubController([_checkpoint("run-1")]),
        lambda rid: _StubSession(rid, driven),
        lock=lock,
        owner="replica-A",
    )
    actions = worker.run_once()
    assert [a.action for a in actions] == ["resumed"]
    assert driven == ["run-1"]
    # 跑完要释放，否则要等租约过期才能被别人接手
    assert lock.owner_of("run-1") is None


def test_驱动失败也要释放锁() -> None:
    """失败不释放的话，这个 run 要等租约到期才可能重试——把故障放大成"卡住"。"""
    db = Path(tempfile.mkdtemp()) / "t.db"
    lock = SqlRunLock(SqliteStore(db), ttl_seconds=300, clock=_Clock())

    def boom(_run_id: str):  # noqa: ANN202
        raise RuntimeError("会话构造就炸了")

    worker = RecoveryWorker(
        _StubController([_checkpoint("run-1")]), boom, lock=lock, owner="A"
    )
    actions = worker.run_once()
    assert [a.action for a in actions] == ["failed"]
    assert lock.owner_of("run-1") is None, "失败路径必须也释放锁"
