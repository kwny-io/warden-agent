"""审批职责分离（SoD）、审批单绑定、审批历史脱敏 —— 三路独立审计的发现项。

## 改之前的样子（这三条都是真问题，不是"理论上"）

1. **没有职责分离**：`POST /approve/{run_id}` 只要请求方"能碰到那个 Run"就能批，
   而在多用户模式下"能碰到"就等于"发起人本人"。于是审批门禁的**第二个人根本不存在**：
   一个被攻陷或误操作的 `user` 凭证，就能 `shell.run(rm -rf …)` 之后自己点批准。
2. **审批历史原样落库**：`arguments` 直接 `json.dumps` 进 `approval_history`。
   工具参数里经常夹着凭据（`Authorization: Bearer …`、`api_key=…`），
   这等于把密钥又复制一份到更容易被导出的表里（备份、只读账号、工单附件）。
3. **不校验"批的是哪张单"**：前端看到审批单 A，期间会话又挂起了 B（先批后跑、
   又撞上下一个高危工具），按 A 的意图回包会把 B 批掉——语义错位，且事后无从分辨。

## 本文件锁住改后的行为

- 发起人不能批自己的单（403，且错误信息给出显式逃生门 `WARDEN_APPROVAL_ALLOW_SELF`）；
- admin（部署内的审批人角色）可以跨归属审批，并因此看到**全局**待审批队列；
  普通用户跨归属仍然 403（归属边界没有被这次改动放宽）；
- 请求体带 `approval_id` 时必须与当前挂起的一致（不一致 → 409）；
- 落库的参数已脱敏，且记下 `approver_id` / `approver_role`。
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import httpx
import pytest
from tests.conftest import ScriptedModel, weather_tool

from warden_agent.model.model import ChatResponse, ToolCall
from warden_agent.policy.policy import Decision, PolicyEngine, PolicyResult
from warden_agent.store.sqlite import SqliteStore
from warden_agent.web.auth import TrustedCaller
from warden_agent.web.server import build_app

#: bob = 发起人；ops = admin（审批人）；alice = 无关的普通用户
CALLERS = {
    "k-alice": TrustedCaller("tenant-a", "user", "alice"),
    "k-bob": TrustedCaller("tenant-a", "user", "bob"),
    "k-ops": TrustedCaller("tenant-a", "user", "ops", role="admin"),
}
A_ALICE = {"Authorization": "Bearer k-alice"}
A_BOB = {"Authorization": "Bearer k-bob"}
A_OPS = {"Authorization": "Bearer k-ops"}


def _ask_policy() -> PolicyEngine:
    """所有工具都要人工批准——把审批路径变成默认路径，省得测例各自造策略。"""
    engine = PolicyEngine()
    engine.add(lambda name, args: PolicyResult(Decision.ASK, "需要人工批准"))
    return engine


def _new_store() -> SqliteStore:
    return SqliteStore(Path(tempfile.mkdtemp()) / "t.db")


def _app(*, store: SqliteStore | None = None, script: list[ChatResponse] | None = None):
    return build_app(
        model=ScriptedModel(
            script
            or [
                ChatResponse(
                    content=None,
                    tool_calls=[ToolCall(id="c1", name="weather.get", arguments={"city": "上海"})],
                    finish_reason="tool_calls",
                ),
                ChatResponse(content="天晴。", finish_reason="stop"),
            ]
        ),
        catalog=weather_tool(),
        policy=_ask_policy(),
        store=store or _new_store(),
        api_keys=CALLERS,
    )


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


# ---------- 职责分离 ----------


@pytest.mark.asyncio
async def test_发起人不能批准自己的审批单() -> None:
    async with _client(_app()) as c:
        first = await c.post("/chat/run-bob", json={"text": "查天气"}, headers=A_BOB)
        assert first.json()["kind"] == "needs_approval"

        denied = await c.post("/approve/run-bob", headers=A_BOB)
        assert denied.status_code == 403
        # 错误信息必须**可行动**：告诉运维存在哪个开关，而不是只说"没权限"
        assert "WARDEN_APPROVAL_ALLOW_SELF" in denied.text

        # 审批单仍在队列里，Run 仍在等待——拒绝的是"这次决策"，不是把单弄丢了
        queue = (await c.get("/approvals", headers=A_BOB)).json()
        assert [a["run_id"] for a in queue] == ["run-bob"]
        assert (await c.get("/status/run-bob", headers=A_BOB)).json()["status"] == (
            "WAITING_APPROVAL"
        )


@pytest.mark.asyncio
async def test_拒绝自己发起的审批同样被职责分离拦下() -> None:
    """拒绝也是决策（会把"已拒绝"喂回模型改变后续行为），同样不能自决。"""
    async with _client(_app()) as c:
        await c.post("/chat/run-bob", json={"text": "查天气"}, headers=A_BOB)
        assert (await c.post("/reject/run-bob", headers=A_BOB)).status_code == 403


@pytest.mark.asyncio
async def test_单操作员开关允许自批(monkeypatch: pytest.MonkeyPatch) -> None:
    """单操作员部署的显式逃生门：开关打开后自批放行（默认是关的）。"""
    monkeypatch.setenv("WARDEN_APPROVAL_ALLOW_SELF", "1")
    async with _client(_app()) as c:
        await c.post("/chat/run-bob", json={"text": "查天气"}, headers=A_BOB)
        ok = await c.post("/approve/run-bob", headers=A_BOB)
        assert ok.status_code == 200
        assert ok.json()["kind"] == "final"


@pytest.mark.asyncio
async def test_admin可跨归属审批并记下审批人() -> None:
    """职责分离要成立，就必须存在"另一个能批的人"——admin 是部署内的审批人角色。"""
    store = _new_store()
    async with _client(_app(store=store)) as c:
        await c.post("/chat/run-bob", json={"text": "查天气"}, headers=A_BOB)
        # admin 看得到全局队列（否则只能靠别人报 run_id 给他）
        assert [a["run_id"] for a in (await c.get("/approvals", headers=A_OPS)).json()] == [
            "run-bob"
        ]
        ok = await c.post("/approve/run-bob", headers=A_OPS)
        assert ok.status_code == 200
        assert ok.json()["kind"] == "final"

    history = store.list_approval_history(limit=10)
    assert history[0]["run_id"] == "run-bob"
    assert history[0]["decision"] == "approved"
    # "谁批的"必须留痕：出事时要能回答这个问题
    assert history[0]["approver_id"] == "ops"
    assert history[0]["approver_role"] == "admin"


@pytest.mark.asyncio
async def test_普通用户仍不能批别人的单() -> None:
    """这次为 admin 开的例外**没有**顺带放宽归属边界：普通用户跨归属依旧 403。"""
    async with _client(_app()) as c:
        await c.post("/chat/run-bob", json={"text": "查天气"}, headers=A_BOB)
        assert (await c.post("/approve/run-bob", headers=A_ALICE)).status_code == 403
        # 也不能靠"看队列"绕过：alice 的队列里没有 bob 的单
        assert (await c.get("/approvals", headers=A_ALICE)).json() == []


# ---------- 审批单绑定（防语义错位 / 防重放）----------


@pytest.mark.asyncio
async def test_审批单绑定不匹配时409() -> None:
    async with _client(_app()) as c:
        await c.post("/chat/run-bob", json={"text": "查天气"}, headers=A_BOB)
        wrong = await c.post(
            "/approve/run-bob", json={"approval_id": "appr-旧的-单"}, headers=A_BOB
        )
        assert wrong.status_code == 409
        assert "审批单已变更" in wrong.text


@pytest.mark.asyncio
async def test_审批单绑定匹配时放行(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WARDEN_APPROVAL_ALLOW_SELF", "1")  # 本例只测绑定，不测 SoD
    async with _client(_app()) as c:
        await c.post("/chat/run-bob", json={"text": "查天气"}, headers=A_BOB)
        pending = (await c.get("/approvals", headers=A_BOB)).json()[0]
        ok = await c.post(
            "/approve/run-bob", json={"approval_id": pending["approval_id"]}, headers=A_BOB
        )
        assert ok.status_code == 200


@pytest.mark.asyncio
async def test_重复批准第二次409(monkeypatch: pytest.MonkeyPatch) -> None:
    """重放保护：批过的单不能再用一次（否则一次审批 = 无限次执行）。"""
    monkeypatch.setenv("WARDEN_APPROVAL_ALLOW_SELF", "1")
    async with _client(_app()) as c:
        await c.post("/chat/run-bob", json={"text": "查天气"}, headers=A_BOB)
        assert (await c.post("/approve/run-bob", headers=A_BOB)).status_code == 200
        replay = await c.post("/approve/run-bob", headers=A_BOB)
        assert replay.status_code == 409
        assert "没有等待审批的请求" in replay.text


# ---------- 落库脱敏与审批人留痕（直接查磁盘，不信 API 的回显）----------


def _raw_approval_rows(db: Path) -> list[tuple[str, str | None, str | None]]:
    conn = sqlite3.connect(db)
    try:
        return [
            (r[0], r[1], r[2])
            for r in conn.execute(
                "SELECT arguments, approver_id, approver_role FROM approval_history ORDER BY id"
            ).fetchall()
        ]
    finally:
        conn.close()


def test_审批参数落库前已脱敏() -> None:
    db = Path(tempfile.mkdtemp()) / "a.db"
    store = SqliteStore(db)
    store.record_approval_decision(
        "run-1",
        "appr-1",
        "shell.run",
        {
            "command": "curl -H 'Authorization: Bearer sk-live-TOPSECRET123456' https://x",
            "api_key": "sk-live-TOPSECRET123456",
            "path": "/tmp/report.txt",
            "nested": {"password": "hunter2", "safe": "ok"},
        },
        "approved",
        approver_id="ops",
        approver_role="admin",
    )
    raw, approver_id, approver_role = _raw_approval_rows(db)[0]

    # 1) 秘密不在盘上
    assert "TOPSECRET" not in raw
    assert "hunter2" not in raw
    # 2) 但换成了可比的指纹标记，不是把整条抹成空白
    assert "<redacted:" in raw
    # 3) 审计要用的非敏感字段照旧可读
    assert "/tmp/report.txt" in raw
    assert "safe" in raw
    assert approver_id == "ops" and approver_role == "admin"


def test_审批人身份可为空_历史行不伪造() -> None:
    """未开认证的本地模式没有 principal——留 NULL 好过编一个看起来像真的身份。"""
    db = Path(tempfile.mkdtemp()) / "b.db"
    store = SqliteStore(db)
    store.record_approval_decision("run-1", "appr-1", "weather.get", {}, "rejected")
    _, approver_id, approver_role = _raw_approval_rows(db)[0]
    assert approver_id is None and approver_role is None


def test_老库升级后补出审批人两列() -> None:
    """schema v5 迁移：老库（没有 approver_* 列）启动时补列，历史行留 NULL。"""
    db = Path(tempfile.mkdtemp()) / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY, data TEXT, status TEXT,
            updated_at TEXT, user_id TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE approval_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id      TEXT NOT NULL,
            approval_id TEXT NOT NULL,
            tool_name   TEXT NOT NULL,
            arguments   TEXT,
            decision    TEXT NOT NULL,
            created_at  TEXT NOT NULL
        );
        INSERT INTO approval_history
            (run_id, approval_id, tool_name, arguments, decision, created_at)
        VALUES ('run-old', 'appr-old', 'shell.run', '{"command": "ls"}',
                'approved', '2026-01-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    store = SqliteStore(db)  # 构造即迁移
    assert store.schema_version() == 5
    store.record_approval_decision(
        "run-new", "appr-new", "shell.run", {"command": "ls"}, "approved",
        approver_id="ops", approver_role="admin",
    )

    rows = _raw_approval_rows(db)
    assert len(rows) == 2
    assert rows[0][1] is None  # 老行不伪造审批人
    assert rows[1][1] == "ops"


def test_审批历史接口不泄露参数正文() -> None:
    """接口层与落库层是两条路：历史接口只回摘要字段（本来就设计成这样）。"""
    db = Path(tempfile.mkdtemp()) / "c.db"
    store = SqliteStore(db)
    store.record_approval_decision(
        "run-1", "appr-1", "shell.run", {"api_key": "sk-live-TOPSECRET123456"}, "approved"
    )
    row = store.list_approval_history(limit=1)[0]
    assert set(row) == {
        "run_id", "approval_id", "tool_name", "decision", "created_at",
        "approver_id", "approver_role",
    }
    assert "TOPSECRET" not in json.dumps(row, ensure_ascii=False)
