"""跨 Run 协调恢复 —— 进程重启/崩溃后，把"手上所有 Run"各自该续的接着续。

单个 Run 的恢复已由 CheckpointManager / AgentSession 解决（读回一个 run 的存档点，
从卡住的那一步继续）。这里解决的是"一堆 run 拿到眼前怎么处理"：

  - 工作进程/Host 保持 run 队列。崩溃重启后，哪些 run 已经完事、哪些还卡在中间、
    哪些彻底失败要重试？不能傻乎乎全部从头跑，也不能漏掉该恢复的。
  - RecoveryController 就是那个"总调度"：读全部 checkpoint -> 按 state 分组 ->
    给出每类 run 的处置建议（继续 / 重试 / 跳过 / 恢复等待交互）。

设计上它**只读、只判断、不执行**——它把"该怎么办"算清楚返回，
由调用方（CLI / HTTP / 工作进程）决定真去跑。这样职责单一、可测、无副作用。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from warden_agent.core.run.status import RunStatus
from warden_agent.runtime.checkpoint import Checkpoint, CheckpointStore


@dataclass
class RecoveryPlan:
    """某组 run 的"处置计划" + attempt 编排的结果。"""

    # 每个 run 应采取的处置动作
    decisions: dict[str, str] = field(default_factory=dict)
    # 该续跑的 run（从各自 checkpoint 的 step 继续）
    to_resume: list[Checkpoint] = field(default_factory=list)
    # 该当成新任务重试的 run（已 FAILED，可安全再来）
    to_retry: list[Checkpoint] = field(default_factory=list)
    # 已完成/已终态的 run（无需处理）
    terminal: list[Checkpoint] = field(default_factory=list)
    # 卡在等待人工输入（审批/交互）的 run（不能自动续，要等人）
    awaiting_human: list[Checkpoint] = field(default_factory=list)

    def action_for(self, run_id: str) -> str | None:
        return self.decisions.get(run_id)


class RecoveryController:
    """协调恢复：把一批 checkpoint 整理成"谁该续、谁该重试、谁跳过"的计划。

    - store : 实现了 CheckpointStore 的持久化（Sqlite / InMemory / ...）
    - retry_failed : 是否把 FAILED 的 run 纳入"重试"（而不是直接判终态）
    - max_attempts_per_run : 单个 run 允许累计尝试几次（failed 重试次数上限）
      —— 防止无限循环。判 terminal 需要知道该 run 已发生过多少次尝试，
      我们根据"项目里还存不存在该 run 的 checkpoint" + attempt 计数来估计。
    """

    def __init__(
        self,
        store: CheckpointStore,
        *,
        retry_failed: bool = True,
        max_attempts_per_run: int = 3,
    ) -> None:
        self.store = store
        self.retry_failed = retry_failed
        self.max_attempts_per_run = max_attempts_per_run

    def plan(self) -> RecoveryPlan:
        """读全部 checkpoint，按状态机分组，产出处置计划。"""
        cps = self.store.list()
        plan = RecoveryPlan()

        for cp in cps:
            status = cp.status
            attempts = cp.attempts

            if status in (RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.TIMED_OUT):
                # 正常到终态：不用管
                plan.decisions[cp.run_id] = "skip"
                plan.terminal.append(cp)
                continue

            if status == RunStatus.FAILED:
                # 失败了：按策略决定重试 or 判终态
                if self.retry_failed and attempts < self.max_attempts_per_run:
                    plan.decisions[cp.run_id] = "retry"
                    plan.to_retry.append(cp)
                else:
                    plan.decisions[cp.run_id] = "skip_failed"
                    plan.terminal.append(cp)
                continue

            if status in (RunStatus.WAITING_APPROVAL, RunStatus.WAITING_INTERACTION,
                          RunStatus.SUSPENDED):
                # 卡在"等人工"的中间态：不能自动续，要人等
                plan.decisions[cp.run_id] = "await_human"
                plan.awaiting_human.append(cp)
                continue

            # 其余（PENDING / QUEUED / RUNNING / SUSPENDING / COMPLETING）：
            # 还没到终态且没在等人工 -> 从存档点继续
            plan.decisions[cp.run_id] = "resume"
            plan.to_resume.append(cp)

        return plan

    def can_resume(self, run_id: str) -> bool:
        """单个 run 是否"可安全续"（它没有中途被判定失败）。"""
        decision = self.plan().action_for(run_id)
        return decision == "resume"
