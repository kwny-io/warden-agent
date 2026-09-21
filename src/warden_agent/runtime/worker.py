"""跨 Run 恢复工作进程：把 `RecoveryController` 算出来的"计划"真正执行掉。

分工（这份文件存在的理由）：

  - `RecoveryController`（`runtime/recovery.py`）**只判断**：读全部存档点，分成
    resume / retry / await_human / terminal 四类。"只判断不执行"是它的设计，
    纯读、无副作用、好测。
  - `RecoveryWorker`（这里）**负责执行**：按计划让会话从存档点继续。

崩了重启之后，Host/工作进程要做的事就是"跑一轮 worker"：该续的续上、该重试的重试
（并计数，超过上限不再重试）、等人工的不碰、终态的跳过。

安全边界（很重要）：
  - **等人工的 Run 绝不自动续**。`AgentSession.resume()` 对 WAITING_APPROVAL 等状态
    直接抛 `RunNotResumable` —— 自动续跑等于绕过人工审批闸门。
  - **已完成/已取消的 Run 不重跑**，否则是重复执行。
  - 重试次数记在 Checkpoint 上（`attempts`），到上限就判终态，防无限重试。
  - **同一 run 同一时刻只由一方驱动**：多副本下两边的恢复计划会同时包含同一个 run，
    没有闸门就会两边一起写、最后**后写覆盖前写**且不报错。所以每个 run 驱动前先抢
    Run 级锁（见 `runtime/locking.py`），抢不到记为 `held_by_other` 并跳过本轮。
    这把锁是**租约式**的：持有者崩了不必人工解锁，租约到期即可被别的副本接手。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from warden_agent.runtime.checkpoint import Checkpoint
from warden_agent.runtime.locking import (
    InProcessRunLock,
    RunLease,
    RunLock,
    new_owner_id,
)
from warden_agent.runtime.recovery import RecoveryController
from warden_agent.runtime.session import AgentSession, NeedsApproval, RunNotResumable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerAction:
    """worker 对某个 run 实际做了什么（可打印、可审计）。"""

    run_id: str
    action: str          # resumed / retried / await_human / skipped / not_resumable / failed
    detail: str = ""


class RecoveryWorker:
    """执行恢复计划的工作进程。

    - `controller`      ：RecoveryController（产出计划）
    - `session_factory` ：`run_id -> AgentSession`，由 `build_agent(...).session_factory`
                          或 `SessionRegistry.get` 提供
    - `max_attempts_per_run`：重试上限，默认沿用 controller 的配置
    - `lock` / `owner` / `lock_ttl_seconds`：Run 级锁（多副本必传 `SqlRunLock`，
      见 `runtime/locking.py`）。不传则是进程内锁，单副本行为与历史一致。

    重试计数由会话侧负责（`AgentSession.resume()` 在 FAILED 分支把 attempts +1
    并随存档点写回），所以这里只做"到上限就不再重试"的判断，不重复计数。
    """

    def __init__(
        self,
        controller: RecoveryController,
        session_factory: Callable[[str], AgentSession],
        *,
        max_attempts_per_run: int | None = None,
        lock: RunLock | None = None,
        owner: str | None = None,
        lock_ttl_seconds: int | None = None,
    ) -> None:
        self.controller = controller
        self.session_factory = session_factory
        self.max_attempts = max_attempts_per_run or controller.max_attempts_per_run
        # Run 级锁：多副本下防止同一个 run 被两个副本同时驱动（会"后写覆盖前写"）。
        # 不传 = 进程内锁，单副本行为与历史一致；多副本要传 SqlRunLock（见 runtime/locking.py）。
        self._lock: RunLock = lock if lock is not None else InProcessRunLock()
        self.owner = owner or new_owner_id()
        self.lock_ttl = lock_ttl_seconds

    # ---- 跑一轮 ----
    def run_once(self) -> list[WorkerAction]:
        """执行一轮恢复：返回每个 run 的实际处置（不抛异常，逐个隔离失败）。"""
        plan = self.controller.plan()
        actions: list[WorkerAction] = []

        for cp in plan.to_resume:
            actions.append(self._drive(cp, "resumed"))

        for cp in plan.to_retry:
            if cp.attempts >= self.max_attempts:
                actions.append(
                    WorkerAction(cp.run_id, "skipped", f"已达重试上限 {self.max_attempts}")
                )
                continue
            actions.append(self._drive(cp, "retried"))

        for cp in plan.awaiting_human:
            actions.append(WorkerAction(cp.run_id, "await_human", cp.status.name))

        for cp in plan.terminal:
            actions.append(WorkerAction(cp.run_id, "skipped", cp.status.name))

        return actions

    def run_forever(
        self, *, interval: float = 5.0, max_rounds: int | None = None
    ) -> list[WorkerAction]:
        """持续轮询（守护进程形态）。`max_rounds` 给定时跑有限轮，便于测试。"""
        rounds = 0
        all_actions: list[WorkerAction] = []
        while max_rounds is None or rounds < max_rounds:
            try:
                all_actions.extend(self.run_once())
            except Exception:  # noqa: BLE001 - 守护进程不能因为一轮失败就退出
                logger.exception("恢复轮次失败")
            rounds += 1
            if max_rounds is None or rounds < max_rounds:
                time.sleep(interval)
        return all_actions

    # ---- 内部 ----
    def _drive(self, cp: Checkpoint, kind: str) -> WorkerAction:
        """让某个 run 从存档点继续。逐个隔离：一个失败不影响其余。

        驱动前先抢 Run 级锁：多副本下同一个 run 可能同时出现在两边的恢复计划里，
        没有这把锁就会两边一起写，最后**后写覆盖前写**、且不报错。抢不到就如实记为
        `held_by_other`（说明别的副本正在处理它），本轮跳过。

        用 `RunLease` 而不是裸 acquire/release：它带**后台心跳续租**，所以"单次恢复跑得比锁
        TTL 还久"也不会让租约中途过期、被别的副本接管（否则又变成并发驱动）。
        `with` 同时保证成功 / 失败 / 抛错都会停心跳并释放——只靠租约自然过期来释放，
        会把一次失败放大成"这个 run 卡到 TTL 才可能重试"。
        """
        with RunLease(self._lock, cp.run_id, self.owner, self.lock_ttl) as lease:
            if not lease.acquired:
                holder = self._lock.owner_of(cp.run_id) or "unknown"
                return WorkerAction(
                    cp.run_id, "held_by_other", f"已被 {holder} 驱动中，本轮跳过"
                )
            return self._drive_locked(cp, kind)

    def _drive_locked(self, cp: Checkpoint, kind: str) -> WorkerAction:
        try:
            session = self.session_factory(cp.run_id)
            outcome = session.resume()
        except RunNotResumable as e:
            # 计划说能续、但会话说不行（例如状态已变）——如实记录，不强行续
            return WorkerAction(cp.run_id, "not_resumable", str(e))
        except Exception as e:  # noqa: BLE001 - 单个 run 失败不拖垮整轮恢复
            logger.warning("恢复 run=%s 失败: %s", cp.run_id, e)
            return WorkerAction(cp.run_id, "failed", str(e))

        if isinstance(outcome, NeedsApproval):
            return WorkerAction(
                cp.run_id, kind, f"又遇到审批: {outcome.approval.tool_name}"
            )
        return WorkerAction(cp.run_id, kind, outcome.text[:80])
