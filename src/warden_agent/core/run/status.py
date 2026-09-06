"""Agent 运行状态机。


状态转换只能通过这里定义的行为方法进行，禁止任意跳转。
"""
from __future__ import annotations

from enum import Enum, auto


class RunStatus(Enum):
    """一次 Agent 任务（Run）可能处于的状态。"""

    PENDING = auto()            # 已创建，还没开始
    QUEUED = auto()             # 已进入队列
    RUNNING = auto()            # 正在执行（模型调用 / 工具执行）
    SUSPENDING = auto()         # 请求暂停（过渡态，不允许直接用 SUSPENDED）
    SUSPENDED = auto()          # 已暂停
    WAITING_INTERACTION = auto()  # 等待用户回复
    WAITING_APPROVAL = auto()     # 等待用户审批高危动作
    COMPLETING = auto()         # 正在收尾（过渡态）
    COMPLETED = auto()          # 正常完成
    FAILED = auto()             # 执行失败
    CANCELLED = auto()          # 主动取消
    TIMED_OUT = auto()          # 超时


class IllegalStateTransition(Exception):
    """非法状态转换：例如从 PENDING 直接跳到 SUSPENDED。"""

    def __init__(self, current: RunStatus, target: RunStatus) -> None:
        self.current = current
        self.target = target
        super().__init__(f"非法状态转换: {current.name} -> {target.name}")


class AgentRun:
    """一次 Agent 任务及其状态。状态变化必须经过命名行为。"""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.status = RunStatus.PENDING

    # ---- 受控状态转换 ----

    def mark_queued(self) -> None:
        self._transition(RunStatus.QUEUED, (RunStatus.PENDING,))

    def start(self) -> None:
        self._transition(RunStatus.RUNNING, (RunStatus.QUEUED,))

    def request_suspend(self) -> None:
        self._transition(RunStatus.SUSPENDING, (RunStatus.RUNNING,))

    def suspend(self) -> None:
        self._transition(RunStatus.SUSPENDED, (RunStatus.SUSPENDING,))

    def resume(self) -> None:
        """回到运行：可来自暂停，也可来自等待审批 / 等待交互（审批通过、用户回复后）。"""
        self._transition(RunStatus.RUNNING,
                         (RunStatus.SUSPENDED,
                          RunStatus.WAITING_APPROVAL,
                          RunStatus.WAITING_INTERACTION))

    def wait_for_interaction(self) -> None:
        self._transition(RunStatus.WAITING_INTERACTION, (RunStatus.RUNNING,))

    def wait_for_approval(self) -> None:
        self._transition(RunStatus.WAITING_APPROVAL, (RunStatus.RUNNING,))

    def begin_completing(self) -> None:
        self._transition(RunStatus.COMPLETING, (RunStatus.RUNNING,))

    def complete(self) -> None:
        self._transition(RunStatus.COMPLETED, (RunStatus.COMPLETING,))

    def restart(self) -> None:
        """终态 -> PENDING：同一个 run 开启新一轮执行周期（多轮对话里收到新消息时）。"""
        self._transition(RunStatus.PENDING, self._terminal())

    def fail(self) -> None:
        self._transition(RunStatus.FAILED, self._any_non_terminal())

    def cancel(self) -> None:
        self._transition(RunStatus.CANCELLED, self._any_non_terminal())

    def timeout(self) -> None:
        self._transition(RunStatus.TIMED_OUT, self._any_non_terminal())

    # ---- 内部工具 ----

    @staticmethod
    def _terminal() -> tuple[RunStatus, ...]:
        return (RunStatus.COMPLETED, RunStatus.FAILED,
                RunStatus.CANCELLED, RunStatus.TIMED_OUT)

    def _any_non_terminal(self) -> tuple[RunStatus, ...]:
        return tuple(s for s in RunStatus if s not in AgentRun._terminal())

    def _transition(self, target: RunStatus, allowed: tuple[RunStatus, ...]) -> None:
        if self.status not in allowed:
            raise IllegalStateTransition(self.status, target)
        self.status = target

    def is_terminal(self) -> bool:
        return self.status in AgentRun._terminal()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"AgentRun(run_id={self.run_id!r}, status={self.status.name})"
