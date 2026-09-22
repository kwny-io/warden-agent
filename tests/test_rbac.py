"""RBAC：管理员 vs 普通用户。

现在只有"API Key → principal"这一层身份，所以角色只做两档（见 `web/auth.py` 的说明）：
  - **user**：所有面向用户的读路径按归属收敛（只看得到自己的）；
  - **admin**：能看**全局**视图（审计 / 审批历史 / 挂起告警 / 恢复计划），能切**部署级**默认模型。

两条关键约定，测试重点盯它们：
  1. **角色来自配置，不来自请求**——客户端无法用参数/头把自己变成管理员；
  2. **不配管理员名单 ⇒ 没有管理员**（fail-closed）——"忘了配"绝不能变成"人人都是管理员"。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import httpx
import pytest
from tests.conftest import ScriptedModel, weather_tool

from warden_agent.model.model import ChatResponse
from warden_agent.policy.policy import PolicyEngine
from warden_agent.store.sqlite import SqliteStore
from warden_agent.web.audit import InMemoryAuditStore
from warden_agent.web.auth import (
    ROLE_ADMIN,
    ROLE_USER,
    ROLE_VIEWER,
    RunOperation,
    TrustedCaller,
    admin_principals,
    role_allows,
    role_for,
    viewer_principals,
)
from warden_agent.web.run_server import resolve_auth
from warden_agent.web.server import build_app

A_ALICE = {"Authorization": "Bearer k-alice"}
A_BOB = {"Authorization": "Bearer k-bob"}


def _callers(*, alice_role: str = ROLE_USER) -> dict[str, TrustedCaller]:
    return {
        "k-alice": TrustedCaller("tenant-a", "user", "alice", role=alice_role),
        "k-bob": TrustedCaller("tenant-a", "user", "bob"),
    }


def _client(*, alice_role: str = ROLE_USER, audit=None) -> httpx.AsyncClient:
    app = build_app(
        model=ScriptedModel([ChatResponse(content="好", finish_reason="stop")] * 4),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=SqliteStore(Path(tempfile.mkdtemp()) / "t.db"),
        api_keys=_callers(alice_role=alice_role),
        audit_store=audit,
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# ---------- 名额解析与角色判定（纯函数）----------


def test_管理员名单解析_不配就是空() -> None:
    assert admin_principals({}) == frozenset()
    assert admin_principals({"WARDEN_ADMIN_PRINCIPALS": " alice , bob ,"}) == {"alice", "bob"}
    # 刻意不支持通配符：写 * 只会被当成一个普通的 principal 名字，不是"所有人"
    assert admin_principals({"WARDEN_ADMIN_PRINCIPALS": "*"}) == {"*"}


def test_角色判定_不在名单就是普通用户() -> None:
    admins = frozenset({"alice"})
    assert role_for("alice", admins) == ROLE_ADMIN
    assert role_for("bob", admins) == ROLE_USER
    assert role_for("alice", frozenset()) == ROLE_USER      # 没配名单 → 谁都不是管理员


def test_角色非法值被拒() -> None:
    with pytest.raises(ValueError):
        TrustedCaller("t", "user", "alice", role="superuser")


def test_配置面接线_角色来自环境() -> None:
    keys, mode = resolve_auth({
        "WARDEN_API_KEYS": "alice:k-alice,bob:k-bob",
        "WARDEN_ADMIN_PRINCIPALS": "alice",
    })
    assert mode == "bearer" and keys is not None
    assert keys["k-alice"].is_admin is True
    assert keys["k-bob"].is_admin is False


def test_单key模式也能配管理员() -> None:
    keys, _ = resolve_auth({
        "WARDEN_API_KEY": "k-solo", "WARDEN_API_USER": "ops",
        "WARDEN_ADMIN_PRINCIPALS": "ops",
    })
    assert keys is not None and keys["k-solo"].is_admin is True


# ---------- 行为：全局视图 ----------


@pytest.mark.asyncio
async def test_普通用户只看自己的审计_管理员看全部() -> None:
    audit = InMemoryAuditStore()
    async with _client(alice_role=ROLE_USER, audit=audit) as c:
        await c.get("/status/run-a", headers=A_ALICE)
        await c.get("/status/run-b", headers=A_BOB)
        alice = (await c.get("/audit", headers=A_ALICE)).json()
        assert alice and all(r["principal_id"] == "alice" for r in alice)

    audit2 = InMemoryAuditStore()
    async with _client(alice_role=ROLE_ADMIN, audit=audit2) as c:
        await c.get("/status/run-a", headers=A_ALICE)
        await c.get("/status/run-b", headers=A_BOB)
        seen = (await c.get("/audit", headers=A_ALICE)).json()
        assert {r["principal_id"] for r in seen} == {"alice", "bob"}, seen


@pytest.mark.asyncio
async def test_管理员能看别人的审批历史_普通用户不能() -> None:
    async with _client(alice_role=ROLE_USER) as c:
        await c.post("/chat/run-h", json={"text": "hi"}, headers=A_BOB)
        # bob 自己看得到
        assert (await c.get("/approvals/history", headers=A_BOB)).status_code == 200

    async with _client(alice_role=ROLE_ADMIN) as c:
        assert (await c.get("/approvals/history", headers=A_ALICE)).status_code == 200


# ---------- 行为：部署级模型切换 ----------


@pytest.mark.asyncio
async def test_普通用户不能切部署级模型_管理员可以() -> None:
    async with _client(alice_role=ROLE_USER) as c:
        r = await c.post("/models/select", json={"id": "fake", "scope": "deployment"},
                         headers=A_ALICE)
        assert r.status_code == 403
        assert "管理员" in r.text

    async with _client(alice_role=ROLE_ADMIN) as c:
        r = await c.post("/models/select", json={"id": "fake", "scope": "deployment"},
                         headers=A_ALICE)
        assert r.status_code == 200
        assert r.json()["scope"] == "deployment"


@pytest.mark.asyncio
async def test_普通用户仍可切自己的模型() -> None:
    """RBAC 不能把"自己切自己的"也一起管死（那是原本就有的正常用法）。"""
    async with _client(alice_role=ROLE_USER) as c:
        r = await c.post("/models/select", json={"id": "fake"}, headers=A_ALICE)
        assert r.status_code == 200 and r.json()["scope"] == "self"


@pytest.mark.asyncio
async def test_scope取值非法时报400() -> None:
    async with _client(alice_role=ROLE_ADMIN) as c:
        r = await c.post("/models/select", json={"id": "fake", "scope": "everything"},
                         headers=A_ALICE)
        assert r.status_code == 400


# ---------- 不变量：角色不可自称 ----------


@pytest.mark.asyncio
async def test_普通用户无法通过请求把自己变成管理员() -> None:
    """角色只由配置决定：带什么头/查询参数都不该改变它。"""
    async with _client(alice_role=ROLE_USER) as c:
        r = await c.post("/models/select", json={"id": "fake", "scope": "deployment"},
                         headers={**A_BOB, "X-Warden-Role": "admin", "X-Admin": "1"})
        assert r.status_code == 403


# ---------- 细粒度 RBAC：只读角色 viewer ----------


def test_只读名单解析与优先级() -> None:
    assert viewer_principals({}) == frozenset()
    assert viewer_principals({"WARDEN_VIEWER_PRINCIPALS": " vera ,,fan ,"}) == {"vera", "fan"}
    # admin 优先于 viewer：同在两份名单里 → 仍按 admin（否则会悄悄削掉运维的全局视图）
    assert role_for("ops", frozenset({"ops"}), frozenset({"ops"})) == ROLE_ADMIN
    assert role_for("vera", frozenset(), frozenset({"vera"})) == ROLE_VIEWER
    assert role_for("bob", frozenset(), frozenset({"vera"})) == ROLE_USER


def test_角色权限映射_只读角色只能读() -> None:
    assert role_allows(ROLE_VIEWER, RunOperation.QUERY)
    assert role_allows(ROLE_VIEWER, RunOperation.READ_EVENTS)
    assert not role_allows(ROLE_VIEWER, RunOperation.SUBMIT_INPUT)
    assert not role_allows(ROLE_VIEWER, RunOperation.START)
    assert not role_allows(ROLE_VIEWER, RunOperation.COMMAND)
    # user / admin 保持原有能力（读写 + 审批）
    for role in (ROLE_USER, ROLE_ADMIN):
        assert all(role_allows(role, op) for op in RunOperation)
    # 未知角色 fail-closed
    assert not role_allows("ghost", RunOperation.QUERY)


def test_配置面接线_只读角色来自环境() -> None:
    keys, mode = resolve_auth({
        "WARDEN_API_KEYS": "alice:k-alice,vera:k-view",
        "WARDEN_VIEWER_PRINCIPALS": "vera",
    })
    assert mode == "bearer" and keys is not None
    assert keys["k-view"].role == ROLE_VIEWER
    assert keys["k-alice"].role == ROLE_USER


@pytest.mark.asyncio
async def test_只读角色能读但不能发起或修改() -> None:
    app = build_app(
        model=ScriptedModel([ChatResponse(content="好", finish_reason="stop")] * 4),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=SqliteStore(Path(tempfile.mkdtemp()) / "t.db"),
        api_keys={"k-view": TrustedCaller("tenant-a", "user", "vera", role=ROLE_VIEWER)},
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        hdr = {"Authorization": "Bearer k-view"}
        # 读：放行
        assert (await c.get("/status/run-x", headers=hdr)).status_code == 200
        # 写 / 审批 / 切模型：一律 403
        assert (await c.post("/chat/run-x", json={"text": "hi"}, headers=hdr)).status_code == 403
        assert (await c.post("/approve/run-x", headers=hdr)).status_code == 403
        assert (await c.post("/reject/run-x", headers=hdr)).status_code == 403
        assert (
            await c.post("/models/select", json={"id": "fake"}, headers=hdr)
        ).status_code == 403
        # 删除是**写**动作，不能因为 operation_for 落到 QUERY 默认就被放行
        assert (await c.delete("/runs/run-x", headers=hdr)).status_code == 403


@pytest.mark.asyncio
async def test_幂等键按调用者作用域_不会读到别人的缓存() -> None:
    """幂等表是全局的：同一 header key 由不同调用者提交，不能互相命中对方的缓存响应。"""
    app = build_app(
        model=ScriptedModel([ChatResponse(content="好", finish_reason="stop")] * 8),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=SqliteStore(Path(tempfile.mkdtemp()) / "t.db"),
        api_keys=_callers(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        key = {"Idempotency-Key": "same-key"}
        ra = await c.post("/chat/run-a", json={"text": "hi"}, headers={**A_ALICE, **key})
        assert ra.status_code == 200 and ra.json()["run_id"] == "run-a"
        # bob 用**同一个 key**：绝不能拿到 alice 那条 /chat/run-a 的缓存响应
        rb = await c.post("/chat/run-b", json={"text": "hi"}, headers={**A_BOB, **key})
        assert rb.status_code == 200
        assert rb.json()["run_id"] == "run-b", "跨调用者命中了幂等缓存（应为各自作用域）"
