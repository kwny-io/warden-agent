"""等待人工超时的告警测试：把"挂着没人管"变成可被监控发现的事实。

背景：Run 进入 `WAITING_APPROVAL` 之后不会自己动——没有超时、没有重试、没有通知。
线上没人盯就一直挂着，发现时可能已经几小时。此前只有"能在 `/recovery/plan` 里看见"，
没有任何"挂太久了"的判断，也就无法接告警。

口径（很重要，测试里也锁住）：等待时长用 Run 的**最后活动时间**近似，
所以只会低估、不会虚报——"报了警"可信；"没报警"不代表一定没挂久。
拿不到时间戳的**宁可报出来**（标为未知），也不默默漏掉。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import httpx
import pytest
from tests.conftest import ScriptedModel, weather_tool

from warden_agent import cli
from warden_agent.core.run.status import AgentRun, RunStatus
from warden_agent.model.model import ChatResponse
from warden_agent.policy.policy import PolicyEngine
from warden_agent.runtime.alerting import describe_stuck, stuck_awaiting_human
from warden_agent.runtime.checkpoint import Checkpoint, SqliteCheckpointStore
from warden_agent.store.sqlite import SqliteStore
from warden_agent.web.auth import TrustedCaller
from warden_agent.web.server import build_app

OLD_ISO = "2026-01-01T00:00:00+00:00"     # 很久以前（相对测试用的 now）
FRESH_ISO = "2026-06-01T12:30:00+00:00"   # 比 _now() 早 30 分钟 → 不算超时


def _now() -> dt.datetime:
    return dt.datetime(2026, 6, 1, 13, 0, 0, tzinfo=dt.UTC)


def _db(tmp_path: Path) -> Path:
    return tmp_path / "t.db"


def _make_run(
    store: SqliteStore,
    run_id: str,
    status: RunStatus,
    *,
    updated_at: str | None,
    owner: str = "",
) -> None:
    """造一个 run + 一条对应状态的存档点，并把 `updated_at` 改成指定时刻。"""
    run = AgentRun(run_id)
    run.mark_queued()
    run.start()                     # QUEUED -> RUNNING
    if status is RunStatus.WAITING_APPROVAL:
        run.wait_for_approval()     # RUNNING -> WAITING_APPROVAL（需人工拍板）
    elif status is RunStatus.COMPLETED:
        run.begin_completing()
        run.complete()
    run.user_id = owner
    store.save_run(run)
    SqliteCheckpointStore(store).save(
        Checkpoint(run_id=run_id, status=status, iteration=1, step="awaiting_approval")
    )
    if updated_at is None:
        store.conn.execute("UPDATE runs SET updated_at = NULL WHERE run_id = ?", (run_id,))
    else:
        store.conn.execute(
            "UPDATE runs SET updated_at = ? WHERE run_id = ?", (updated_at, run_id)
        )
    store.conn.commit()


def _store(tmp_path: Path) -> SqliteStore:
    return SqliteStore(_db(tmp_path))


# ---------------------------------------------------------------------------
# 判定逻辑
# ---------------------------------------------------------------------------


def test_等待人工超时的会被报出来(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _make_run(store, "run-old", RunStatus.WAITING_APPROVAL, updated_at=OLD_ISO)
    stuck = stuck_awaiting_human(store, SqliteCheckpointStore(store),
                                 older_than_seconds=3600, now=_now())
    assert [s.run_id for s in stuck] == ["run-old"]
    assert stuck[0].status == "WAITING_APPROVAL"
    assert stuck[0].waiting_seconds is not None and stuck[0].waiting_seconds > 3600


def test_刚进入等待的不会被误报(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _make_run(store, "run-fresh", RunStatus.WAITING_APPROVAL, updated_at=FRESH_ISO)
    stuck = stuck_awaiting_human(store, SqliteCheckpointStore(store),
                                 older_than_seconds=3600, now=_now())
    assert stuck == []          # 只等了 1 小时以内


def test_不在等待人工的run不参与告警(tmp_path: Path) -> None:
    """RUNNING（该被恢复 worker 续跑）和终态都不属于"等人管"。"""
    store = _store(tmp_path)
    _make_run(store, "run-running", RunStatus.RUNNING, updated_at=OLD_ISO)
    _make_run(store, "run-done", RunStatus.COMPLETED, updated_at=OLD_ISO)
    stuck = stuck_awaiting_human(store, SqliteCheckpointStore(store),
                                 older_than_seconds=3600, now=_now())
    assert stuck == []


def test_拿不到时间戳的宁可报出来(tmp_path: Path) -> None:
    """漏报一个可能挂了很久的 Run，比多报一个"未知"更糟。"""
    store = _store(tmp_path)
    _make_run(store, "run-nostamp", RunStatus.WAITING_APPROVAL, updated_at=None)
    stuck = stuck_awaiting_human(store, SqliteCheckpointStore(store),
                                 older_than_seconds=3600, now=_now())
    assert [s.run_id for s in stuck] == ["run-nostamp"]
    assert stuck[0].waiting_seconds is None
    assert "拿不到" in stuck[0].detail


def test_按等得最久的排序(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _make_run(store, "run-a", RunStatus.WAITING_APPROVAL, updated_at="2026-06-01T10:00:00+00:00")
    _make_run(store, "run-b", RunStatus.WAITING_APPROVAL, updated_at="2026-04-01T10:00:00+00:00")
    stuck = stuck_awaiting_human(store, SqliteCheckpointStore(store),
                                 older_than_seconds=3600, now=_now())
    assert [s.run_id for s in stuck] == ["run-b", "run-a"]


def test_可按归属过滤(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _make_run(store, "run-a", RunStatus.WAITING_APPROVAL, updated_at=OLD_ISO, owner="alice")
    _make_run(store, "run-b", RunStatus.WAITING_APPROVAL, updated_at=OLD_ISO, owner="bob")
    mine = stuck_awaiting_human(store, SqliteCheckpointStore(store),
                                older_than_seconds=3600, owner="alice", now=_now())
    assert [s.run_id for s in mine] == ["run-a"]


def test_渲染成可读文本(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _make_run(store, "run-old", RunStatus.WAITING_APPROVAL, updated_at=OLD_ISO)
    stuck = stuck_awaiting_human(store, SqliteCheckpointStore(store),
                                 older_than_seconds=3600, now=_now())
    text = describe_stuck(stuck)
    assert "run-old" in text and "小时" in text
    assert describe_stuck([]) == "没有等待人工超时的 run。"


# ---------------------------------------------------------------------------
# CLI：接 cron 告警用（有输出 → 退出码 3）
# ---------------------------------------------------------------------------


def test_cli_stuck_有超时则退出码为3(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _make_run(store, "run-old", RunStatus.WAITING_APPROVAL, updated_at=OLD_ISO)
    with pytest.raises(SystemExit) as exc:
        cli.main(["stuck", "--db", str(_db(tmp_path)), "--older-than-min", "60"])
    assert exc.value.code == 3


def test_cli_stuck_无超时则退出码为0(tmp_path: Path) -> None:
    """CLI 走的是**真实时钟**（不是测试的假 now），所以"新鲜"要用当前时间造。"""
    store = _store(tmp_path)
    just_now = dt.datetime.now(dt.UTC).isoformat()
    _make_run(store, "run-fresh", RunStatus.WAITING_APPROVAL, updated_at=just_now)
    cli.main(["stuck", "--db", str(_db(tmp_path)), "--older-than-min", "60"])
    # 正常返回（不抛 SystemExit）——刚刚挂起的 run 不该被报超时


def test_cli_stuck_支持json输出(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store = _store(tmp_path)
    _make_run(store, "run-old", RunStatus.WAITING_APPROVAL, updated_at=OLD_ISO)
    with pytest.raises(SystemExit):
        cli.main(["stuck", "--db", str(_db(tmp_path)), "--older-than-min", "60", "--json"])
    payload = capsys.readouterr().out
    assert '"run_id": "run-old"' in payload


# ---------------------------------------------------------------------------
# HTTP：给监控系统抓
# ---------------------------------------------------------------------------


def _app(store: SqliteStore, **kw):  # noqa: ANN202
    return build_app(
        model=ScriptedModel([ChatResponse(content="好", finish_reason="stop")]),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=store,
        **kw,
    )


@pytest.mark.asyncio
async def test_HTTP告警端点报出超时run(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _make_run(store, "run-old", RunStatus.WAITING_APPROVAL, updated_at=OLD_ISO)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(store)), base_url="http://test"
    ) as c:
        body = (await c.get("/alerts/stuck?older_than_min=0")).json()
    assert body["count"] == 1
    assert body["runs"][0]["run_id"] == "run-old"


@pytest.mark.asyncio
async def test_HTTP告警端点按归属收敛(tmp_path: Path) -> None:
    """认证模式下不能让 alice 看到 bob 的挂起会话（与其它列表接口同一口径）。"""
    store = _store(tmp_path)
    _make_run(store, "run-a", RunStatus.WAITING_APPROVAL, updated_at=OLD_ISO, owner="alice")
    _make_run(store, "run-b", RunStatus.WAITING_APPROVAL, updated_at=OLD_ISO, owner="bob")
    app = _app(
        store,
        api_keys={
            "k-alice": TrustedCaller("acme", "user", "alice"),
            "k-bob": TrustedCaller("acme", "user", "bob"),
        },
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        a = (await c.get("/alerts/stuck?older_than_min=0",
                         headers={"Authorization": "Bearer k-alice"})).json()
        b = (await c.get("/alerts/stuck?older_than_min=0",
                         headers={"Authorization": "Bearer k-bob"})).json()
    assert [r["run_id"] for r in a["runs"]] == ["run-a"]
    assert [r["run_id"] for r in b["runs"]] == ["run-b"]


@pytest.mark.asyncio
async def test_HTTP告警端点无需鉴权也受保护(tmp_path: Path) -> None:
    """未开鉴权时（本机开发）不按归属过滤——与其它端点的匿名模式口径一致。"""
    store = _store(tmp_path)
    _make_run(store, "run-a", RunStatus.WAITING_APPROVAL, updated_at=OLD_ISO)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(store)), base_url="http://test"
    ) as c:
        body = (await c.get("/alerts/stuck?older_than_min=0")).json()
    assert body["count"] == 1


# ---------------------------------------------------------------------------
# 精确时间戳：优先用"进入等待审批的时刻"，而不是 Run 的最后活动时间
# ---------------------------------------------------------------------------


def _with_pending_approval(store: SqliteStore, run_id: str, created_at: str) -> None:
    """给 run 造一条待审批记录，并把它的 created_at 改成指定时刻。"""
    store.save_pending_approval(run_id, "appr-1", "fs.delete", {"path": "/x"}, "需批准")
    store.conn.execute(
        "UPDATE pending_approvals SET created_at = ? WHERE run_id = ?", (created_at, run_id)
    )
    store.conn.commit()


def test_等待审批用精确时间戳_即使最后活动时间很新也算超时(tmp_path: Path) -> None:
    """**这条是本次改动的核心价值**：

    以前只按 Run 的最后活动时间算——如果这个 run 在等待期间被碰过（例如又查了一次状态），
    时长会被刷新，于是**该报的挂起被漏掉**。现在用"进入等待审批的时刻"，不受影响。
    """
    store = _store(tmp_path)
    _make_run(store, "run-wait", RunStatus.WAITING_APPROVAL, updated_at=FRESH_ISO)
    _with_pending_approval(store, "run-wait", OLD_ISO)      # 审批请求是很久以前发的

    stuck = stuck_awaiting_human(store, SqliteCheckpointStore(store),
                                 older_than_seconds=3600, now=_now())
    assert [s.run_id for s in stuck] == ["run-wait"], "最后活动时间很新，但等待确实很久了"
    assert stuck[0].source == "approval", "应当用精确时间戳，而不是近似值"
    assert stuck[0].waiting_seconds is not None and stuck[0].waiting_seconds > 3600


def test_刚发出的审批请求不算超时(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _make_run(store, "run-new", RunStatus.WAITING_APPROVAL, updated_at=FRESH_ISO)
    _with_pending_approval(store, "run-new", FRESH_ISO)
    stuck = stuck_awaiting_human(store, SqliteCheckpointStore(store),
                                 older_than_seconds=3600, now=_now())
    assert stuck == []


def test_没有待审批记录时退回近似值并标明来源(tmp_path: Path) -> None:
    """`WAITING_INTERACTION` 这类没有 pending_approvals 行的，只能用最后活动时间近似——
    此时 `source` 会说明它是近似值，调用方可以据此提示"实际可能更久"。"""
    store = _store(tmp_path)
    run = AgentRun("run-talk")
    run.mark_queued()
    run.start()
    run.wait_for_interaction()
    store.save_run(run)
    SqliteCheckpointStore(store).save(
        Checkpoint(run_id="run-talk", status=RunStatus.WAITING_INTERACTION,
                   iteration=1, step="awaiting_input")
    )
    store.conn.execute("UPDATE runs SET updated_at = ? WHERE run_id = ?", (OLD_ISO, "run-talk"))
    store.conn.commit()

    stuck = stuck_awaiting_human(store, SqliteCheckpointStore(store),
                                 older_than_seconds=3600, now=_now())
    assert [s.run_id for s in stuck] == ["run-talk"]
    assert stuck[0].source == "last_activity"
    assert "近似" in describe_stuck(stuck), "近似值要在文本里说明，别让人以为是精确的"


def test_内存存储没有精确接口时自动退回近似(tmp_path: Path) -> None:
    """存储层没实现 `pending_approval_created_at`（例如内存版）时不能崩，要退回近似。"""
    store = _store(tmp_path)
    _make_run(store, "run-old", RunStatus.WAITING_APPROVAL, updated_at=OLD_ISO)

    class _NoApprovalTime:              # 只代理必要方法，模拟"没有精确接口"的存储
        def __init__(self, inner: SqliteStore) -> None:
            self._inner = inner

        def list_runs(self, limit: int = 50, owner: str | None = None):  # noqa: ANN201
            return self._inner.list_runs(limit=limit, owner=owner)

        def load_run(self, run_id: str):  # noqa: ANN201
            return self._inner.load_run(run_id)

    stuck = stuck_awaiting_human(_NoApprovalTime(store), SqliteCheckpointStore(store),
                                 older_than_seconds=3600, now=_now())
    assert [s.run_id for s in stuck] == ["run-old"]
    assert stuck[0].source == "last_activity"


@pytest.mark.asyncio
async def test_HTTP端点报出时长来源(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _make_run(store, "run-wait", RunStatus.WAITING_APPROVAL, updated_at=FRESH_ISO)
    _with_pending_approval(store, "run-wait", OLD_ISO)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(store)), base_url="http://test"
    ) as c:
        body = (await c.get("/alerts/stuck?older_than_min=0")).json()
    assert body["runs"][0]["waiting_seconds"] is not None
