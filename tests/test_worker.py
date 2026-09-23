"""恢复 worker 与 `AgentSession.resume()` 测试：计划真正被执行，且边界不被越过。

改之前：`RecoveryController` 只输出"该怎么办"，没有任何东西去执行——重启后
一堆 run 实际上还是没人管。这里锁住接上 worker 之后的行为：

  - `resume()` 能从不完整状态接着跑，且**不会**替已完成的 Run 重跑；
  - **等人工的 Run 绝不自动续**（自动续 = 绕过审批闸门）；
  - FAILED 的 Run 可重试，且 `attempts` 真的递增并写回（重试上限才有意义）；
  - 单个 Run 失败不拖垮整轮恢复。
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest
from tests.conftest import ScriptedModel, weather_tool

from warden_agent.core.run.status import AgentRun, RunStatus
from warden_agent.model.model import AgentChatModel, ChatRequest, ChatResponse, Message, ToolCall
from warden_agent.policy.policy import Decision, PolicyEngine, PolicyResult
from warden_agent.runtime.checkpoint import Checkpoint, checkpoint_store_for
from warden_agent.runtime.locking import InProcessRunLock
from warden_agent.runtime.recovery import RecoveryController
from warden_agent.runtime.session import (
    AgentSession,
    FinalReply,
    NeedsApproval,
    RunNotResumable,
)
from warden_agent.runtime.worker import RecoveryWorker
from warden_agent.store.sqlite import SqliteStore


class BoomModel(AgentChatModel):
    """一调就崩的模型：用来制造"跑到一半进程挂了"的 RUNNING 状态。"""

    def chat(self, request: ChatRequest) -> ChatResponse:
        raise RuntimeError("模拟崩溃")


def _store() -> SqliteStore:
    return SqliteStore(Path(tempfile.mkdtemp()) / "t.db")


def _cp(store: SqliteStore):  # 返回 CheckpointStore
    cp = checkpoint_store_for(store)
    assert cp is not None
    return cp


def _session(store: SqliteStore, run_id: str, model, policy=None) -> AgentSession:
    return AgentSession(
        run_id=run_id,
        model=model,
        catalog=weather_tool(),
        policy_engine=policy or PolicyEngine(),
        store=store,
        checkpoint_store=checkpoint_store_for(store),
    )


def _ok(text: str = "完成") -> list[ChatResponse]:
    return [ChatResponse(content=text, finish_reason="stop")]


def _ask_policy() -> PolicyEngine:
    engine = PolicyEngine()
    engine.add(lambda name, args: PolicyResult(Decision.ASK, "需要人工批准"))
    return engine


def _crash_a_run(store: SqliteStore, run_id: str = "r-crash") -> None:
    """模拟"进程被硬杀"：状态与存档点都停在半途（RUNNING / model_call），无人收尾。

    ⚠️ **不能用"模型抛异常"来制造这个状态**：可捕获的异常属于"失败"，会被标记为 FAILED，
    走的是"重试 + attempts 上限"那条路（见 `test_驱动失败会被标记FAILED`），
    而不是"崩溃续跑"。硬杀是进程来不及做任何收尾——状态原样停在那里。
    """
    run = AgentRun(run_id=run_id)
    run.mark_queued()
    run.start()
    store.save_run(run)
    store.append_message(run_id, Message(role="user", content="hi"))
    cp = checkpoint_store_for(store)
    assert cp is not None
    cp.save(Checkpoint(run_id=run_id, status=RunStatus.RUNNING, iteration=0, step="model_call"))


# ---------- resume() 行为 ----------


def test_resume从崩溃处接着跑() -> None:
    store = _store()
    _crash_a_run(store)
    assert store.load_run("r-crash").status == RunStatus.RUNNING  # 确认停在 RUNNING

    # 模拟重启：同一个 run_id 造新会话，换一个能正常工作的模型
    outcome = _session(store, "r-crash", ScriptedModel(_ok("续跑完成"))).resume()
    assert isinstance(outcome, FinalReply)
    assert outcome.text == "续跑完成"
    assert store.load_run("r-crash").status == RunStatus.COMPLETED


def test_驱动失败会被标记FAILED而非停在RUNNING() -> None:
    """驱动期抛异常 = "失败"，必须落到 FAILED 状态。

    否则它会停在 RUNNING，被恢复计划当成"可续跑"，而 `resume()` 只在 FAILED 分支给
    `attempts` +1 —— 于是重试上限形同虚设，同一个坏 Run 被**无限重试**。
    """
    store = _store()
    sess = _session(store, "r-boom", BoomModel())
    with pytest.raises(RuntimeError, match="模拟崩溃"):
        sess.start("hi")
    assert store.load_run("r-boom").status == RunStatus.FAILED


def test_失败run重试时attempts递增_重试上限才有意义() -> None:
    store = _store()
    sess = _session(store, "r-retry", BoomModel())
    with pytest.raises(RuntimeError):
        sess.start("hi")
    cp = _cp(store)
    first = cp.load("r-retry")
    assert first is not None and first.attempts == 1

    # FAILED → resume() 是"重试"：attempts +1（仍失败，所以再次 FAILED）
    with pytest.raises(RuntimeError):
        _session(store, "r-retry", BoomModel()).resume()
    again = cp.load("r-retry")
    assert again is not None and again.attempts == 2, "重试必须使 attempts 递增"
    assert store.load_run("r-retry").status == RunStatus.FAILED


def test_完成态不可续跑() -> None:
    """已验证完成的 Run 再跑一遍 = 重复执行，必须拦住。"""
    store = _store()
    _session(store, "r-done", ScriptedModel(_ok())).start("hi")
    assert store.load_run("r-done").status == RunStatus.COMPLETED
    with pytest.raises(RunNotResumable, match="终态"):
        _session(store, "r-done", ScriptedModel(_ok())).resume()


def test_等待审批的run不自动续跑() -> None:
    """自动续跑等待审批的 Run = 绕过人工闸门，必须拦住。"""
    store = _store()
    script = [
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="c1", name="weather.get", arguments={"city": "上海"})],
            finish_reason="tool_calls"),
    ]
    outcome = _session(store, "r-ask", ScriptedModel(script), _ask_policy()).start("查天气")
    assert isinstance(outcome, NeedsApproval)

    fresh = _session(store, "r-ask", ScriptedModel(_ok()), _ask_policy())
    with pytest.raises(RunNotResumable, match="等人工"):
        fresh.resume()
    # 状态没被改动，仍在等人工
    assert store.load_run("r-ask").status == RunStatus.WAITING_APPROVAL


def test_failed可重试且attempts递增() -> None:
    store = _store()
    cp_store = _cp(store)
    run = AgentRun("r-fail", user_id="u")
    run.mark_queued()
    run.start()
    run.fail()
    store.save_run(run)
    store.append_message("r-fail", Message(role="user", content="hi"))
    cp_store.save(Checkpoint(
        run_id="r-fail", status=RunStatus.FAILED, iteration=0, step="done", attempts=1
    ))

    outcome = _session(store, "r-fail", ScriptedModel(_ok("重试成功"))).resume()
    assert isinstance(outcome, FinalReply)
    assert outcome.text == "重试成功"
    assert store.load_run("r-fail").status == RunStatus.COMPLETED
    # attempts 递增并随存档点写回（这是重试上限唯一的依据）
    reloaded = cp_store.load("r-fail")
    assert reloaded is not None and reloaded.attempts == 2


def test_resume从存档迭代继续_不重跑已完成迭代() -> None:
    """存档点记着"跑到第几轮"（iteration），resume 必须从那里接着跑，
    而不是无视它从 0 重来（修复前：iteration/step 存了但没人消费）。"""
    store = _store()
    cp_store = _cp(store)
    run = AgentRun("r-iter")
    run.mark_queued()
    run.start()
    store.save_run(run)
    store.append_message("r-iter", Message(role="user", content="hi"))
    # 进程在第 5 轮 model_call 前被硬杀
    cp_store.save(Checkpoint(
        run_id="r-iter", status=RunStatus.RUNNING, iteration=5, step="model_call"
    ))

    model = ScriptedModel(_ok("续跑完成"))
    sess = _session(store, "r-iter", model)
    seen: list[tuple[str, int]] = []
    orig = sess._checkpoint  # noqa: SLF001

    def spy(step: str, iteration: int, *, retryable: bool = True) -> None:
        seen.append((step, iteration))
        orig(step, iteration, retryable=retryable)

    sess._checkpoint = spy  # type: ignore[method-assign]  # noqa: SLF001
    outcome = sess.resume()

    assert isinstance(outcome, FinalReply)
    # 关键：第 5 轮之前的迭代（< 5）一次都没再执行
    assert all(it >= 5 for _, it in seen), seen
    assert seen[0] == ("model_call", 5)
    assert model.calls == 1  # 只发生一次模型调用，没有从 0 重放


def test_resume_tool_exec步从下一轮继续() -> None:
    """step=tool_exec 表示该轮模型调用已完成、正处在工具执行中：从下一轮继续。"""
    store = _store()
    cp_store = _cp(store)
    run = AgentRun("r-tool")
    run.mark_queued()
    run.start()
    store.save_run(run)
    store.append_message("r-tool", Message(role="user", content="hi"))
    cp_store.save(Checkpoint(
        run_id="r-tool", status=RunStatus.RUNNING, iteration=3, step="tool_exec"
    ))

    model = ScriptedModel(_ok("续跑完成"))
    sess = _session(store, "r-tool", model)
    seen: list[tuple[str, int]] = []
    orig = sess._checkpoint  # noqa: SLF001

    def spy(step: str, iteration: int, *, retryable: bool = True) -> None:
        seen.append((step, iteration))
        orig(step, iteration, retryable=retryable)

    sess._checkpoint = spy  # type: ignore[method-assign]  # noqa: SLF001
    sess.resume()
    assert seen[0] == ("model_call", 4)  # 该轮模型调用不重跑


def test_resume用存档点状态校正run_不绕过审批闸门() -> None:
    """run 表里是旧 RUNNING、存档点却是 WAITING_APPROVAL 时，会话必须按存档点
    判"等人"——否则会按旧状态自动续跑，绕过人工审批闸门。
    （修复前：restore() 只在测试里被引用，session 完全忽略存档点状态。）"""
    store = _store()
    cp_store = _cp(store)
    run = AgentRun("r-diverge")
    run.mark_queued()
    run.start()  # 库里的 run 停在 RUNNING（与存档点分叉）
    store.save_run(run)
    cp_store.save(Checkpoint(
        run_id="r-diverge", status=RunStatus.WAITING_APPROVAL,
        iteration=2, step="awaiting_approval",
    ))

    # 恢复控制器按存档点把它归入"等人"
    assert RecoveryController(cp_store).plan().action_for("r-diverge") == "await_human"

    sess = _session(store, "r-diverge", ScriptedModel(_ok()))
    with pytest.raises(RunNotResumable, match="等人工"):
        sess.resume()
    # 分歧被解决：run 状态被校正为存档点的状态并写回
    assert sess.status() == RunStatus.WAITING_APPROVAL
    assert store.load_run("r-diverge").status == RunStatus.WAITING_APPROVAL


def test_控制器判可续的run_会话不再抛RunNotResumable() -> None:
    """反向分叉：run 表已是 COMPLETED、存档点仍是 RUNNING。控制器按存档点判"可续"，
    会话就不能因为旧终态把它拒掉（修复前：resume 抛 RunNotResumable("已是终态")）。"""
    store = _store()
    cp_store = _cp(store)
    run = AgentRun("r-resumable")
    run.mark_queued()
    run.start()
    run.begin_completing()
    run.complete()  # 库里的 run 已是 COMPLETED
    store.save_run(run)
    store.append_message("r-resumable", Message(role="user", content="hi"))
    cp_store.save(Checkpoint(
        run_id="r-resumable", status=RunStatus.RUNNING, iteration=1, step="model_call"
    ))
    assert RecoveryController(cp_store).plan().action_for("r-resumable") == "resume"

    outcome = _session(store, "r-resumable", ScriptedModel(_ok("续跑完成"))).resume()
    assert isinstance(outcome, FinalReply)
    assert outcome.text == "续跑完成"
    assert store.load_run("r-resumable").status == RunStatus.COMPLETED


# ---------- worker ----------


def test_worker续跑崩溃的run() -> None:
    store = _store()
    _crash_a_run(store)
    controller = RecoveryController(_cp(store))
    worker = RecoveryWorker(
        controller,
        lambda rid: _session(store, rid, ScriptedModel(_ok("worker 续跑"))),
    )
    actions = worker.run_once()
    assert [a.action for a in actions] == ["resumed"]
    assert store.load_run("r-crash").status == RunStatus.COMPLETED


def test_worker不接手等人工的run() -> None:
    store = _store()
    script = [
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="c1", name="weather.get", arguments={"city": "上海"})],
            finish_reason="tool_calls"),
    ]
    _session(store, "r-ask", ScriptedModel(script), _ask_policy()).start("查天气")

    controller = RecoveryController(_cp(store))
    worker = RecoveryWorker(
        controller, lambda rid: _session(store, rid, ScriptedModel(_ok()), _ask_policy())
    )
    actions = worker.run_once()
    assert [a.action for a in actions] == ["await_human"]
    # 关键：没有被自动批准、也没有被自动执行
    assert store.load_run("r-ask").status == RunStatus.WAITING_APPROVAL


def test_worker重试超上限则跳过() -> None:
    """控制器把超上限的 FAILED 判为终态；worker 自身还有一道上限（纵深防御）。"""
    store = _store()
    cp_store = _cp(store)
    cp_store.save(Checkpoint(
        run_id="r-max", status=RunStatus.FAILED, iteration=0, step="done", attempts=3
    ))
    # 控制器上限放宽到 10 → 计划把它归入"该重试"；worker 自己上限 3 → 应拒绝执行
    controller = RecoveryController(cp_store, max_attempts_per_run=10)
    worker = RecoveryWorker(
        controller,
        lambda rid: pytest.fail("超上限不应调用会话"),
        max_attempts_per_run=3,
    )
    actions = worker.run_once()
    assert actions[0].action == "skipped"
    assert "重试上限" in actions[0].detail


def test_控制器默认把超上限的failed判为终态() -> None:
    store = _store()
    cp_store = _cp(store)
    cp_store.save(Checkpoint(
        run_id="r-max", status=RunStatus.FAILED, iteration=0, step="done", attempts=5
    ))
    controller = RecoveryController(cp_store, max_attempts_per_run=3)
    worker = RecoveryWorker(controller, lambda rid: pytest.fail("终态不该被续跑"))
    actions = worker.run_once()
    assert actions[0].action == "skipped"
    assert actions[0].detail == "FAILED"


def test_worker重试未超上限则执行() -> None:
    store = _store()
    cp_store = _cp(store)
    run = AgentRun("r-retry", user_id="u")
    run.mark_queued()
    run.start()
    run.fail()
    store.save_run(run)
    cp_store.save(Checkpoint(
        run_id="r-retry", status=RunStatus.FAILED, iteration=0, step="done", attempts=1
    ))
    controller = RecoveryController(cp_store, max_attempts_per_run=3)
    worker = RecoveryWorker(
        controller, lambda rid: _session(store, rid, ScriptedModel(_ok("重试好了")))
    )
    actions = worker.run_once()
    assert actions[0].action == "retried"
    assert store.load_run("r-retry").status == RunStatus.COMPLETED
    reloaded = cp_store.load("r-retry")
    assert reloaded is not None and reloaded.attempts == 2


def test_worker租约丢失时_不报成功而是aborted() -> None:
    """
    驱动期间续租失败 = 我们不再独占该 run（可能已被别的副本接管）。
    此时绝不能把结果报成 resumed——否则两边都以为是自己驱动的。
    """
    class _RenewFailsLock(InProcessRunLock):
        def renew(self, run_id: str, owner: str, ttl_seconds: int | None = None) -> bool:
            return False

    class _SlowSession:
        def __init__(self, run_id: str) -> None:
            self.run_id = run_id

        def resume(self):  # noqa: ANN201
            time.sleep(0.8)  # 比心跳间隔（ttl/3 ≈ 0.33s）长，触发一次续租失败
            return FinalReply(text="done", messages=[])

    store = _store()
    _crash_a_run(store)
    controller = RecoveryController(_cp(store))
    worker = RecoveryWorker(
        controller, _SlowSession,
        lock=_RenewFailsLock(ttl_seconds=1),
        lock_ttl_seconds=1,
    )
    actions = worker.run_once()
    assert [a.action for a in actions] == ["aborted"]
    assert "租约" in actions[0].detail


def test_worker单个run失败不影响整轮() -> None:
    store = _store()
    _crash_a_run(store, "r-good")
    _crash_a_run(store, "r-bad")

    def factory(rid: str) -> AgentSession:
        if rid == "r-bad":
            raise RuntimeError("造会话失败")
        return _session(store, rid, ScriptedModel(_ok("good")))

    actions = RecoveryWorker(RecoveryController(_cp(store)), factory).run_once()
    by_run = {a.run_id: a.action for a in actions}
    assert by_run["r-good"] == "resumed"
    assert by_run["r-bad"] == "failed"
    assert store.load_run("r-good").status == RunStatus.COMPLETED


# ---------- CLI ----------


def test_cli_recover_apply(capsys, tmp_path: Path) -> None:
    from warden_agent import cli

    db = str(tmp_path / "apply.db")
    store = SqliteStore(db)
    _crash_a_run(store)

    cli.main(["recover", "--db", db, "--apply"])
    out = capsys.readouterr().out
    assert "执行一轮恢复" in out
    assert "r-crash" in out
    # 真的被续跑了（默认装配用离线假模型）
    assert SqliteStore(db).load_run("r-crash").status == RunStatus.COMPLETED
