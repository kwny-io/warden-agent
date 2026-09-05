"""Checkpoint / Attempt / 完成门禁 测试。"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from warden_agent.core.run.status import AgentRun, RunStatus
from warden_agent.runtime.checkpoint import (
    AttemptExecutor,
    Checkpoint,
    CheckpointManager,
    CompletionGuard,
    CompletionGuardError,
    InMemoryCheckpointStore,
    RetryPolicy,
    SqliteCheckpointStore,
)
from warden_agent.store.sqlite import SqliteStore


# ---- Checkpoint ----
def test_capture_and_restore_roundtrip() -> None:
    store = InMemoryCheckpointStore()
    mgr = CheckpointManager(store)
    run = AgentRun("r1")
    run.mark_queued()
    run.start()
    cp = mgr.capture(run, iteration=3, step="tool_exec", usage_tokens=120)
    assert cp.iteration == 3
    assert cp.status == RunStatus.RUNNING

    # 模拟重启：新 manager 从同一底层 store 恢复
    run2 = AgentRun("r1")
    restored = CheckpointManager(InMemoryCheckpointStore())  # 独立存储
    assert restored.restore(run2) is None  # 无共享存储

    restored2 = CheckpointManager(store)  # 复用同一 store
    cp2 = restored2.restore(run2)
    assert cp2 is not None
    assert cp2.iteration == 3
    assert run2.status == RunStatus.RUNNING


def test_checkpoint_serializable() -> None:
    cp = Checkpoint("r1", RunStatus.WAITING_APPROVAL, 5, "awaiting_approval", 10)
    d = cp.to_dict()
    cp2 = Checkpoint.from_dict(d)
    assert cp2.status == RunStatus.WAITING_APPROVAL
    assert cp2.iteration == 5


def test_checkpoint_persisted_to_sqlite() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sqlite = SqliteStore(Path(tmp) / "cp.db")
        mgr = CheckpointManager(SqliteCheckpointStore(sqlite))
        run = AgentRun("r-cp")
        run.mark_queued()
        run.start()
        run.wait_for_approval()
        mgr.capture(run, iteration=2, step="awaiting_approval")

        # 新连接读回（模拟重启）
        sqlite2 = SqliteStore(Path(tmp) / "cp.db")
        restored = CheckpointManager(SqliteCheckpointStore(sqlite2)).restore(AgentRun("r-cp"))
        assert restored is not None
        assert restored.step == "awaiting_approval"
        assert restored.status == RunStatus.WAITING_APPROVAL
        sqlite.close()
        sqlite2.close()


# ---- Attempt + RetryPolicy ----
def test_retry_on_transient_error() -> None:
    calls = {"n": 0}

    def flaky() -> int:
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("transient")
        return 42

    executor = AttemptExecutor(RetryPolicy(max_attempts=5, retry_on_errors=(TimeoutError,)))
    assert executor.run("step", flaky) == 42
    assert calls["n"] == 3


def test_no_retry_on_logic_error() -> None:
    calls = {"n": 0}

    def always_bug() -> int:
        calls["n"] += 1
        raise ValueError("deterministic bug")

    executor = AttemptExecutor(RetryPolicy(max_attempts=5, retry_on_errors=(TimeoutError,)))
    with pytest.raises(ValueError):
        executor.run("step", always_bug)
    assert calls["n"] == 1  # 逻辑错误不重试

def test_pure_tool_retries_more() -> None:
    calls = {"n": 0}

    def pure_fn() -> str:
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("sometime")
        return "ok"

    # is_pure=True：普通 RuntimeError 也允许重试（无副作用，安全）
    executor = AttemptExecutor(RetryPolicy(max_attempts=4, retryable_pure=True))
    assert executor.run("step", pure_fn, is_pure=True) == "ok"
    assert calls["n"] == 2


# ---- CompletionGuard ----
def test_completion_guard_blocks_pending_tools() -> None:
    run = AgentRun("r1")
    guard = CompletionGuard()
    with pytest.raises(CompletionGuardError):
        guard.validate(run, pending_tools=1, has_final_content=True)


def test_completion_guard_requires_content() -> None:
    run = AgentRun("r1")
    guard = CompletionGuard()
    with pytest.raises(CompletionGuardError):
        guard.validate(run, pending_tools=0, has_final_content=False)


def test_completion_guard_passes_when_clean() -> None:
    run = AgentRun("r1")
    guard = CompletionGuard()
    guard.validate(run, pending_tools=0, has_final_content=True)  # 不抛
