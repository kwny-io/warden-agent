"""Checkpoint / Attempt / 完成门禁 —— 让运行从"能续对话"升级成"能续状态"。

  - CheckpointManager + RunSnapshot ：把一次 Run 的"运行快照"存下来。
  - AttemptExecutor + RetryPolicy  ：工具执行的分次尝试与可重试策略。
  - CompletionGuard / RunFinalizer ：进 COMPLETED 前的"完成门禁"校验。

三者解决的问题各不相同：

1. Checkpoint（存档点）
   - 已有的 AgentSession 只能"恢复对话历史"。但对话历史 ≠ 运行状态——
     比如"我正在调第 3 个工具、处在第 5 次迭代"，光看对话是推不出来的。
   - Checkpoint 存的是：run_id、此刻状态、迭代编号、进行到哪一步、已耗用量。
     重启后用 Checkpoint 能精确回到"卡在哪"，而不是傻乎乎从头再跑一遍。

2. Attempt（尝试）+ RetryPolicy（重试策略）
   - 工具或模型调用会失败（网络抖动、超时、工具报错）。
   - 不是所有失败都该立刻放弃：瞬时故障应重试，确定性的逻辑错误不应重试（重试也白费）。
   - pure=True 的工具没有副作用，重试安全；有副作用的工具重试要谨慎。

3. CompletionGuard（完成门禁）
   - 不能因为模型"说了句像结论的话"就直接进 COMPLETED。
   - 完成前要校验：有没有还没执行完的工具？结果是不是满足完成条件？
     校验不过就不让进终态——这就是"完成门禁"。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from warden_agent.core.run.status import AgentRun, RunStatus


# ---------------------------------------------------------------------------
# Checkpoint
# ---------------------------------------------------------------------------
@dataclass
class Checkpoint:
    """一次运行中某个时刻的"存档点"。"""

    run_id: str
    status: RunStatus
    iteration: int
    step: str          # 进行到哪一步：init / model_call / tool_exec / awaiting_approval / done
    usage_tokens: int = 0
    attempts: int = 1  # 该 run 累计尝试圈数（失败重试会用；协调恢复靠它决定还重不重）

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status.name,
            "iteration": self.iteration,
            "step": self.step,
            "usage_tokens": self.usage_tokens,
            "attempts": self.attempts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Checkpoint:
        return cls(
            run_id=str(data.get("run_id", "")),
            status=RunStatus[str(data.get("status", "PENDING"))],
            iteration=int(data.get("iteration", 0)),
            step=str(data.get("step", "init")),
            usage_tokens=int(data.get("usage_tokens", 0)),
            attempts=int(data.get("attempts", 1)),
        )


class CheckpointManager:
    """管理一次 Run 的存档点：最新 Checkpoint 存/取/推进。"""

    def __init__(self, persistence: CheckpointStore | None = None) -> None:
        self._persistence = persistence
        self._latest: Checkpoint | None = None

    def capture(self, run: AgentRun, iteration: int, step: str,
                usage_tokens: int = 0) -> Checkpoint:
        cp = Checkpoint(run_id=run.run_id, status=run.status, iteration=iteration,
                        step=step, usage_tokens=usage_tokens)
        self._latest = cp
        if self._persistence is not None:
            self._persistence.save(cp)
        return cp

    @property
    def latest(self) -> Checkpoint | None:
        return self._latest

    def restore(self, run: AgentRun) -> Checkpoint | None:
        """从持久化里恢复最新存档点，并据此校正 Run 的状态。返回存档点或 None。"""
        cp = (self._persistence.load(run.run_id)
              if self._persistence is not None else self._latest)
        if cp is None:
            return None
        run.status = cp.status
        self._latest = cp
        return cp


class CheckpointStore:
    """Checkpoint 的持久化接口。任何存储实现它即可。"""

    def save(self, checkpoint: Checkpoint) -> None:
        raise NotImplementedError

    def load(self, run_id: str) -> Checkpoint | None:
        raise NotImplementedError

    def list(self) -> list[Checkpoint]:
        raise NotImplementedError


class InMemoryCheckpointStore(CheckpointStore):
    """进程内存档（重启丢），用于测试和不落盘的场景。"""

    def __init__(self) -> None:
        self._data: dict[str, Checkpoint] = {}

    def save(self, checkpoint: Checkpoint) -> None:
        self._data[checkpoint.run_id] = checkpoint

    def load(self, run_id: str) -> Checkpoint | None:
        return self._data.get(run_id)

    def list(self) -> list[Checkpoint]:
        return list(self._data.values())


class SqliteCheckpointStore(CheckpointStore):
    """把 Checkpoint 落进一个 SqliteStore（其 save_checkpoint / load_checkpoint）。"""

    def __init__(self, sqlite_store: Any) -> None:
        self._store = sqlite_store

    def save(self, checkpoint: Checkpoint) -> None:
        self._store.save_checkpoint(checkpoint)

    def load(self, run_id: str) -> Checkpoint | None:
        cp = self._store.load_checkpoint(run_id)
        return cp if isinstance(cp, Checkpoint) else None

    def list(self) -> list[Checkpoint]:
        cps = self._store.list_checkpoints()
        return [cp for cp in cps if isinstance(cp, Checkpoint)]


# ---------------------------------------------------------------------------
# Attempt + RetryPolicy
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Attempt:
    """一次工具/步骤的"尝试"。记录第几次、是否成功、结果/错误。"""

    attempt: int
    step: str
    success: bool
    error: str | None = None
    result: Any = None


class RetryPolicy:
    """决定"要不要重试一次失败的尝试"。

    - max_attempts      ：最多尝试几次
    - retry_on_errors   ：命中这些错误才重试（瞬时故障）
    - retryable_pure    ：pure=True 的工具额外允许重试（无副作用，安全）
    """

    def __init__(
        self,
        max_attempts: int = 3,
        retry_on_errors: tuple[type[BaseException], ...] = (TimeoutError, ConnectionError),
        retryable_pure: bool = True,
    ) -> None:
        self.max_attempts = max_attempts
        self.retry_on_errors = retry_on_errors
        self.retryable_pure = retryable_pure

    def should_retry(self, attempt: Attempt, error: BaseException, is_pure: bool) -> bool:
        """尝试失败后问它：还重试吗？"""
        if attempt.attempt >= self.max_attempts:
            return False
        return isinstance(error, self.retry_on_errors) or bool(
            self.retryable_pure and is_pure
        )


class AttemptExecutor:
    """按 RetryPolicy 执行一个"可能失败"的步骤，自动决定重试与否。"""

    def __init__(self, policy: RetryPolicy | None = None) -> None:
        self.policy = policy or RetryPolicy()
        self.attempts: list[Attempt] = []

    def run(self, step: str, fn: Callable[[], Any], is_pure: bool = False) -> Any:
        """执行 fn；失败时按策略重试。返回成功结果，或抛最后一次错误。

        fn 是一次无参可调用。内部统计重试次数。
        """
        attempt_no = 0
        while True:
            attempt_no += 1
            try:
                result = fn()
                self.attempts.append(Attempt(attempt_no, step, True, result=result))
                return result
            except BaseException as e:  # noqa: BLE001 - 要捕获所有故障以决定重试
                attempt = Attempt(attempt_no, step, False, error=repr(e))
                self.attempts.append(attempt)
                if not self.policy.should_retry(attempt, e, is_pure):
                    raise
                # 否则继续循环重试


# ---------------------------------------------------------------------------
# CompletionGuard（完成门禁）
# ---------------------------------------------------------------------------
class CompletionGuard:
    """进 COMPLETED 之前的校验。校验不过，不让 Run 草率进入终态。"""

    def validate(self, run: AgentRun, *, pending_tools: int = 0,
                 has_final_content: bool) -> None:
        """完成前检查。不满足条件抛 CompletionGuardError，阻止完成。"""
        if pending_tools > 0:
            raise CompletionGuardError(
                run.run_id,
                f"还有 {pending_tools} 个工具调用未执行，不能进入完成态",
            )
        if not has_final_content:
            raise CompletionGuardError(run.run_id, "没有产生最终回答，不能进入完成态")


class CompletionGuardError(Exception):
    """完成门禁没通过：不能进入 COMPLETED。"""

    def __init__(self, run_id: str, reason: str) -> None:
        self.run_id = run_id
        self.reason = reason
        super().__init__(f"Run {run_id} 完成校验失败: {reason}")
