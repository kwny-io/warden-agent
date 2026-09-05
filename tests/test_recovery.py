"""T6 跨 run 协调恢复测试：list_checkpoints + RecoveryController 分组。"""
from __future__ import annotations

import tempfile
from pathlib import Path

from warden_agent.core.run.status import AgentRun, RunStatus
from warden_agent.runtime.checkpoint import (
    Checkpoint,
    InMemoryCheckpointStore,
    SqliteCheckpointStore,
)
from warden_agent.runtime.recovery import RecoveryController
from warden_agent.store.sqlite import SqliteStore


def _save_sqlite_checkpoints(cps: list[Checkpoint]) -> SqliteStore:
    store = SqliteStore(Path(tempfile.mkdtemp()) / "rec.db")
    sqlite_cp = SqliteCheckpointStore(store)
    for cp in cps:
        sqlite_cp.save(cp)
    return store


# ---- SqliteStore.list_checkpoints() 枚举所有 run ----
def test_list_checkpoints_enumerates_all_runs() -> None:
    cps = [
        Checkpoint("r1", RunStatus.RUNNING, 2, "tool_exec"),
        Checkpoint("r2", RunStatus.COMPLETED, 5, "done"),
        Checkpoint("r3", RunStatus.FAILED, 1, "model_call"),
    ]
    store = _save_sqlite_checkpoints(cps)
    listed = store.list_checkpoints()
    assert len(listed) == 3
    by_id = {cp.run_id: cp for cp in listed}
    assert by_id["r1"].step == "tool_exec"
    assert by_id["r2"].status == RunStatus.COMPLETED
    assert by_id["r3"].status == RunStatus.FAILED
    store.close()


def test_list_checkpoints_empty_when_none() -> None:
    store = SqliteStore(Path(tempfile.mkdtemp()) / "empty.db")
    assert store.list_checkpoints() == []
    store.close()


# ---- SqliteCheckpointStore.list() 接上底层 ----
def test_sqlite_checkpoint_store_list() -> None:
    cps = [Checkpoint("a", RunStatus.RUNNING, 1, "init"),
           Checkpoint("b", RunStatus.WAITING_APPROVAL, 3, "awaiting_approval")]
    store = _save_sqlite_checkpoints(cps)
    got = SqliteCheckpointStore(store).list()
    assert {c.run_id for c in got} == {"a", "b"}
    store.close()


# ---- RecoveryController：跨 run 分组 ----
def _seed(inmem: InMemoryCheckpointStore, cps: list[Checkpoint]) -> None:
    for cp in cps:
        inmem.save(cp)


def test_plan_groups_terminal_resume_retry_await() -> None:
    inmem = InMemoryCheckpointStore()
    _seed(inmem, [
        Checkpoint("done", RunStatus.COMPLETED, 9, "done"),
        Checkpoint("run", RunStatus.RUNNING, 3, "tool_exec"),
        Checkpoint("fail", RunStatus.FAILED, 1, "model_call"),
        Checkpoint("wait", RunStatus.WAITING_APPROVAL, 2, "awaiting_approval"),
    ])
    plan = RecoveryController(inmem).plan()

    assert plan.action_for("done") == "skip"
    assert plan.action_for("run") == "resume"
    assert plan.action_for("fail") == "retry"
    assert plan.action_for("wait") == "await_human"

    assert [c.run_id for c in plan.to_resume] == ["run"]
    assert [c.run_id for c in plan.to_retry] == ["fail"]
    assert [c.run_id for c in plan.terminal] == ["done"]
    assert [c.run_id for c in plan.awaiting_human] == ["wait"]


def test_failed_stops_retrying_after_max_attempts() -> None:
    inmem = InMemoryCheckpointStore()
    # 该 run 已试到第 3 次，再失败就不该无限重试（attempts 记录在 checkpoint 上）
    cp = Checkpoint("f", RunStatus.FAILED, 3, "model_call", attempts=3)
    _seed(inmem, [cp])
    plan = RecoveryController(inmem, max_attempts_per_run=3).plan()
    assert plan.action_for("f") == "skip_failed"
    assert [c.run_id for c in plan.to_retry] == []


def test_failed_below_max_attempts_retries() -> None:
    inmem = InMemoryCheckpointStore()
    cp = Checkpoint("g", RunStatus.FAILED, 1, "model_call", attempts=1)
    _seed(inmem, [cp])
    plan = RecoveryController(inmem, max_attempts_per_run=3).plan()
    assert plan.action_for("g") == "retry"
    assert [c.run_id for c in plan.to_retry] == ["g"]


def test_can_resume_only_when_resume() -> None:
    inmem = InMemoryCheckpointStore()
    cps = [Checkpoint("ok", RunStatus.RUNNING, 1, "init"),
           Checkpoint("bad", RunStatus.FAILED, 1, "model_call")]
    _seed(inmem, cps)
    ctrl = RecoveryController(inmem)
    assert ctrl.can_resume("ok") is True
    assert ctrl.can_resume("bad") is False
