"""恢复接线测试：会话真的写存档点，恢复控制器真的读得到。

改之前：`CheckpointManager` / `RecoveryController` / `SqliteCheckpointStore` 只在测试里
出现过——产品路径（AgentSession）从不写 checkpoint，`checkpoints` 表永远是空的，
所以"跨 Run 恢复"是个空转的模块。这里锁住接上之后的行为：
  - 会话主循环在 model_call / tool_exec / awaiting_approval / done 四个点落存档；
  - 存档状态与 Run 状态一致（等待审批的 run 会被恢复控制器归入 awaiting_human）；
  - `GET /recovery/plan` 与 `warden recover` 都能把计划读出来。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import httpx
import pytest
from tests.conftest import ScriptedModel, weather_tool

from warden_agent.model.model import ChatResponse, ToolCall
from warden_agent.policy.policy import Decision, PolicyEngine, PolicyResult
from warden_agent.runtime.checkpoint import SqliteCheckpointStore
from warden_agent.runtime.recovery import RecoveryController
from warden_agent.runtime.session import AgentSession, NeedsApproval
from warden_agent.store.sqlite import SqliteStore
from warden_agent.web.server import build_app


def _store() -> SqliteStore:
    return SqliteStore(Path(tempfile.mkdtemp()) / "t.db")


def _ask_policy() -> PolicyEngine:
    engine = PolicyEngine()
    engine.add(lambda name, args: PolicyResult(Decision.ASK, "需要人工批准"))
    return engine


def _session(store: SqliteStore, script, policy=None, run_id="run-cp") -> AgentSession:
    return AgentSession(
        run_id=run_id,
        model=ScriptedModel(script),
        catalog=weather_tool(),
        policy_engine=policy or PolicyEngine(),
        store=store,
        checkpoint_store=SqliteCheckpointStore(store),
    )


# ---------- 会话写存档点 ----------


def test_完成一轮会落done存档() -> None:
    store = _store()
    sess = _session(store, [ChatResponse(content="你好", finish_reason="stop")])
    sess.start("hi")

    cps = SqliteCheckpointStore(store).list()
    assert [c.run_id for c in cps] == ["run-cp"]
    cp = cps[0]
    assert cp.step == "done"
    assert cp.status.name == "COMPLETED"


def test_工具调用轮次会落model_call与tool_exec() -> None:
    store = _store()
    script = [
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="c1", name="weather.get", arguments={"city": "上海"})],
            finish_reason="tool_calls"),
        ChatResponse(content="天晴", finish_reason="stop"),
    ]
    sess = _session(store, script, run_id="run-steps")

    seen: list[str] = []
    orig = sess._checkpoint  # noqa: SLF001

    def spy(step: str, iteration: int) -> None:
        seen.append(step)
        orig(step, iteration)

    sess._checkpoint = spy  # type: ignore[method-assign]  # noqa: SLF001
    sess.start("查天气")
    assert "model_call" in seen
    assert "tool_exec" in seen
    assert seen[-1] == "done"


def test_等待审批的会话落awaiting_human() -> None:
    store = _store()
    script = [
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="c1", name="weather.get", arguments={"city": "上海"})],
            finish_reason="tool_calls"),
    ]
    sess = _session(store, script, policy=_ask_policy())
    outcome = sess.start("查天气")
    assert isinstance(outcome, NeedsApproval)

    cps = SqliteCheckpointStore(store).list()
    assert cps[0].step == "awaiting_approval"
    assert cps[0].status.name == "WAITING_APPROVAL"

    # 恢复控制器据此归入"等人"，绝不自动续跑
    plan = RecoveryController(SqliteCheckpointStore(store)).plan()
    assert [c.run_id for c in plan.awaiting_human] == ["run-cp"]
    assert plan.to_resume == []
    assert plan.action_for("run-cp") == "await_human"


def test_恢复控制器区分续跑与终态() -> None:
    store = _store()
    # 一个正常完成（终态）
    _session(store, [ChatResponse(content="好", finish_reason="stop")],
             run_id="run-done").start("hi")
    plan = RecoveryController(SqliteCheckpointStore(store)).plan()
    assert plan.action_for("run-done") == "skip"
    assert [c.run_id for c in plan.terminal] == ["run-done"]


# ---------- HTTP 端点 ----------


@pytest.mark.asyncio
async def test_recovery_plan端点返回计划() -> None:
    store = _store()
    app = build_app(
        model=ScriptedModel([ChatResponse(content="好", finish_reason="stop")]),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=store,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/chat/run-h", json={"text": "hi"})
        r = await c.get("/recovery/plan")
        assert r.status_code == 200
        plan = r.json()
        assert [c_["run_id"] for c_ in plan["terminal"]] == ["run-h"]
        assert plan["decisions"]["run-h"] == "skip"


@pytest.mark.asyncio
async def test_recovery_plan按归属过滤() -> None:
    from warden_agent.web.auth import TrustedCaller

    store = _store()
    app = build_app(
        model=ScriptedModel([ChatResponse(content="好", finish_reason="stop")]),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=store,
        api_keys={"k-alice": TrustedCaller("t", "user", "alice")},
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/chat/run-a", json={"text": "hi"},
                     headers={"Authorization": "Bearer k-alice"})
        # 非 alice 的 key 看不到 alice 的 run
        app2 = build_app(
            model=ScriptedModel([ChatResponse(content="好", finish_reason="stop")]),
            catalog=weather_tool(), policy=PolicyEngine(), store=store,
            api_keys={"k-bob": TrustedCaller("t", "user", "bob")},
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app2), base_url="http://test"
        ) as c2:
            plan = (await c2.get("/recovery/plan",
                                 headers={"Authorization": "Bearer k-bob"})).json()
            assert plan["terminal"] == []
            assert plan["decisions"] == {}


# ---------- CLI 命令 ----------


def test_cli_recover_输出计划(capsys, tmp_path: Path) -> None:
    from warden_agent import cli

    db = str(tmp_path / "cli.db")
    store = SqliteStore(db)
    _session(store, [ChatResponse(content="好", finish_reason="stop")]).start("hi")

    cli.main(["recover", "--db", db, "--json"])
    out = capsys.readouterr().out
    assert '"run-cp"' in out
    assert '"skip"' in out


def test_cli_recover_空库不报错(capsys, tmp_path: Path) -> None:
    from warden_agent import cli

    cli.main(["recover", "--db", str(tmp_path / "empty.db")])
    assert "没有可恢复的 run" in capsys.readouterr().out
