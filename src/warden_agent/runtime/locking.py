"""Run 级锁：多副本下防止**同一个 run 被两个副本同时驱动**。

为什么需要它（这是"支持多副本"最容易漏的一环）：
  协调状态（幂等 / 事件流 / 限流计数）已经能放进共享存储，但**"谁在驱动这个 run"没有闸门**：
  同一个 `run_id` 若被副本 A 和副本 B 同时推进，两边都写状态与消息，结果是**后写覆盖前写**——
  会话历史出现分叉或丢失，且不会报错。此前项目里对这件事的说法是"建议做会话粘性路由"，
  属于把正确性交给部署方自觉。

设计：**租约式锁**（lease），而不是"拿到就一直持有"的硬锁：
  - `acquire(run_id, owner, ttl)`：键空闲或**租约已过期**时可被接管（单条原子 UPSERT 决定归属）；
  - 持有者崩了不需要人工解锁——租约到期后别的副本自然能接手（这对"崩溃恢复"场景是必须的，
    否则一次宕机就永久锁死一个 run）；
  - `release` 只删自己的锁（owner 不匹配不动），避免误删他人刚接管的锁；
  - `renew` 只能续自己的、且未过期的锁。

两种实现（与 `web/coordination.py` 的分工一致）：
  - `InProcessRunLock`：单副本默认，行为与历史一致（仍是"锁"，只是锁在进程内）；
  - `SqlRunLock`：多副本用，复用 `RunStore` 所在的库（新增 `run_locks` 表）。

**诚实的边界**：TTL 到期而持有者仍在跑（例如一次恢复耗时超过 TTL）时，另一个副本可以接管，
于是又变成并发驱动。所以 TTL 要按"单次恢复最长耗时"设；真要严格保证，需要带心跳的续租循环
（`renew` 已提供，但没有内置后台心跳线程）。
"""

from __future__ import annotations

import logging
import os
import secrets
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

DEFAULT_TTL_SECONDS = 600

logger = logging.getLogger(__name__)


class RunLockStore(Protocol):
    """存储层要提供的原子取锁能力（由 SqliteStore / PostgresStore 结构化满足）。"""

    def acquire_run_lock(
        self, run_id: str, owner: str, expires_at: float, now: float
    ) -> bool: ...

    def renew_run_lock(
        self, run_id: str, owner: str, expires_at: float, now: float
    ) -> bool: ...

    def release_run_lock(self, run_id: str, owner: str) -> None: ...

    def run_lock_owner(self, run_id: str, now: float) -> str | None: ...


class RunLock(Protocol):
    """Run 级锁的统一接口。`owner` 标识"谁持有"，用来防误释放。"""

    def acquire(self, run_id: str, owner: str, ttl_seconds: float | None = None) -> bool: ...

    def renew(self, run_id: str, owner: str, ttl_seconds: float | None = None) -> bool: ...

    def release(self, run_id: str, owner: str) -> None: ...

    def owner_of(self, run_id: str) -> str | None: ...


def new_owner_id() -> str:
    """给"这一份进程"造一个唯一标识：主机名 + pid + 随机后缀。

    主机名是为了多机排查时能看出"是哪台机器持有的"；pid 区分同机不同进程；
    随机后缀保证同一进程重启前后不会撞成同一个 owner。
    """
    try:
        host = socket.gethostname()
    except OSError:  # pragma: no cover - 极端环境拿不到主机名
        host = "unknown-host"
    return f"{host}:{os.getpid()}:{secrets.token_hex(4)}"


@dataclass
class _Held:
    owner: str
    expires_at: float


class InProcessRunLock:
    """进程内 Run 锁（单副本默认）。

    行为与 SQL 版对齐（含过期接管、owner 校验），只是作用域限于本进程——
    **多副本下每个副本各锁各的，等于没锁**（`run_lock_for(shared=True)` 会换成 SQL 版）。
    """

    def __init__(
        self, ttl_seconds: float = DEFAULT_TTL_SECONDS, clock: Callable[[], float] | None = None
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self._clock = clock or time.time
        self._locks: dict[str, _Held] = {}
        self._mutex = threading.Lock()

    def acquire(self, run_id: str, owner: str, ttl_seconds: float | None = None) -> bool:
        ttl = ttl_seconds or self.ttl_seconds
        now = self._clock()
        with self._mutex:
            held = self._locks.get(run_id)
            if held is not None and held.expires_at > now and held.owner != owner:
                return False  # 有人在有效期内持有
            self._locks[run_id] = _Held(owner=owner, expires_at=now + ttl)
            return True

    def renew(self, run_id: str, owner: str, ttl_seconds: float | None = None) -> bool:
        ttl = ttl_seconds or self.ttl_seconds
        now = self._clock()
        with self._mutex:
            held = self._locks.get(run_id)
            if held is None or held.owner != owner or held.expires_at <= now:
                return False
            held.expires_at = now + ttl
            return True

    def release(self, run_id: str, owner: str) -> None:
        with self._mutex:
            held = self._locks.get(run_id)
            if held is not None and held.owner == owner:
                del self._locks[run_id]

    def owner_of(self, run_id: str) -> str | None:
        now = self._clock()
        with self._mutex:
            held = self._locks.get(run_id)
            if held is None:
                return None
            if held.expires_at <= now:
                del self._locks[run_id]
                return None
            return held.owner


class SqlRunLock:
    """把存储层的原子取锁包成 `RunLock`（多副本共享同一张 `run_locks` 表）。"""

    def __init__(
        self,
        store: RunLockStore,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._store = store
        self.ttl_seconds = ttl_seconds
        self._clock = clock or time.time

    def acquire(self, run_id: str, owner: str, ttl_seconds: float | None = None) -> bool:
        ttl = ttl_seconds or self.ttl_seconds
        now = self._clock()
        return self._store.acquire_run_lock(run_id, owner, now + ttl, now)

    def renew(self, run_id: str, owner: str, ttl_seconds: float | None = None) -> bool:
        ttl = ttl_seconds or self.ttl_seconds
        now = self._clock()
        return self._store.renew_run_lock(run_id, owner, now + ttl, now)

    def release(self, run_id: str, owner: str) -> None:
        self._store.release_run_lock(run_id, owner)

    def owner_of(self, run_id: str) -> str | None:
        return self._store.run_lock_owner(run_id, self._clock())


class RunLease:
    """一次 Run 锁的**持有期**：带后台心跳续租，退出时停心跳并释放。

    为什么需要心跳（这关掉了一个真实窗口）：
      锁是**租约式**的（带 TTL）——好处是持有者崩了不必人工解锁。代价是：**单次驱动如果比 TTL 还长**
      （模型+工具跑很久、SSE 流很久），租约会在中途过期，另一个副本就能接管，于是又变成并发驱动。
      心跳就是每过 TTL/3 续一次租，把"单次驱动必须短于 TTL"这个隐含约束消掉。

    两种用法：
      - `with RunLease(...) as lease:` —— 常规用法（worker 的每次驱动）；
      - 手动 `start()` / `stop()` —— **流式**要用这种：锁要在端点里取，但要到 SSE 流结束才释放。

    `lost` 是重要的：如果某次续租失败（例如锁被别人按过期规则接管了），说明**我们手里已经没有锁**，
    此时的工作可能与别人冲突。默认会打警告并通过 `on_lost` 回调通知调用方——不静默吞掉。
    """

    def __init__(
        self,
        lock: RunLock,
        run_id: str,
        owner: str,
        ttl_seconds: float | None = None,
        *,
        interval_seconds: float | None = None,
        on_lost: Callable[[], None] | None = None,
    ) -> None:
        self._lock = lock
        self.run_id = run_id
        self.owner = owner
        # 显式取 float：getattr 的返回是 Any，直接参与算术会让类型检查失效。
        # 用 float 而不是 int：亚秒级 TTL（测试里的 0.6s）被 int() 截断会变成 0，
        # 于是静默退回 fallback——既不是调用方给的值，也让 TTL/3 的心跳间隔失真。
        fallback_ttl = float(getattr(lock, "ttl_seconds", DEFAULT_TTL_SECONDS))
        self._ttl: float = float(ttl_seconds) if ttl_seconds else fallback_ttl
        # 默认按 TTL 的 1/3 续租：留出两次重试余量，避免"刚好卡在过期边上"
        self._interval: float = interval_seconds or max(0.1, self._ttl / 3.0)
        self._on_lost = on_lost
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._acquired = False
        self._lost = False

    # ---- 状态 ----
    @property
    def acquired(self) -> bool:
        return self._acquired

    @property
    def lost(self) -> bool:
        """持有期间是否丢过租约（丢过就意味着"我们可能已经不是在独占驱动这个 run"了）。"""
        return self._lost

    # ---- 生命周期 ----
    def start(self) -> bool:
        """取锁并启动心跳。返回是否拿到锁（没拿到就不会起心跳，也不会在 stop 时释放）。"""
        if not self._lock.acquire(self.run_id, self.owner, self._ttl):
            return False
        self._acquired = True
        self._thread = threading.Thread(
            target=self._beat, name=f"run-lease-{self.run_id}", daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        """停心跳并释放锁。幂等（反复调用无副作用）。"""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)   # 不让清理卡住调用方
            self._thread = None
        if self._acquired:
            self._acquired = False
            self._lock.release(self.run_id, self.owner)

    def __enter__(self) -> RunLease:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # ---- 心跳 ----
    def _beat(self) -> None:
        """后台续租。续租失败即视为丢锁：**停止续租并通知**，不假装还持有。"""
        while not self._stop.wait(self._interval):
            if self._lock.renew(self.run_id, self.owner, self._ttl):
                continue
            self._lost = True
            from warden_agent.core.metrics import note

            note("warden_lock_renew_failures_total", "Run 锁续租失败次数（丢锁）")
            logger.warning(
                "Run 租约续租失败：run=%s owner=%s —— 租约可能已被接管，"
                "本次工作不再独占该 run（不会再自动续租）",
                self.run_id, self.owner,
            )
            if self._on_lost is not None:
                self._on_lost()
            return


def run_lock_for(store: object, *, shared: bool) -> RunLock:
    """按"是否多副本"造一把 Run 锁。

    `shared=True` 要求 store 具备取锁方法（`acquire_run_lock` 等），否则回落到进程内并告警——
    与 `coordination_for` 同一个取向：**能力不够就明说，不假装多副本下是安全的**。
    """
    required = ("acquire_run_lock", "renew_run_lock", "release_run_lock", "run_lock_owner")
    if shared and all(callable(getattr(store, name, None)) for name in required):
        return SqlRunLock(store)  # type: ignore[arg-type]  # 结构化满足协议
    if shared:
        import logging

        logging.getLogger(__name__).warning(
            "请求共享 Run 锁，但存储 %s 不支持取锁方法——回落为进程内实现："
            "多副本下同一 run 仍可能被同时驱动，请改用 SqliteStore / PostgresStore",
            type(store).__name__,
        )
    return InProcessRunLock()
