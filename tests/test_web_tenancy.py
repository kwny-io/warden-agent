"""多租户隔离测试：身份来自凭证，越权被拦。

背景（改之前的样子）：`user_id` 是客户端自带的查询参数，授权器默认空转，
所以**拿到任意一把 key 就能读写他人会话**——"多账号隔离"实际只是前端过滤器。

这里锁住改后的行为：
  - 认证模式下身份由 key 决定，`?user_id=` 被忽略（客户端自称无效）；
  - 针对他人 Run 的读/写/删/审批一律 403；
  - 列表类接口（/runs、/approvals、/approvals/history、/audit、/users）只暴露自己的数据；
  - 匿名开发模式（未开启认证）保持旧的开放行为，本地演示不受影响。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import httpx
import pytest
from tests.conftest import ScriptedModel, weather_tool

from warden_agent.model.model import ChatResponse, ToolCall
from warden_agent.policy.policy import Decision, PolicyEngine, PolicyResult
from warden_agent.store.sqlite import SqliteStore
from warden_agent.web.audit import InMemoryAuditStore
from warden_agent.web.auth import TrustedCaller
from warden_agent.web.server import build_app

# 两个用户各自一把 key：alice 与 bob
CALLERS = {
    "k-alice": TrustedCaller("tenant-a", "user", "alice"),
    "k-bob": TrustedCaller("tenant-a", "user", "bob"),
}
# bob 在另一个租户，用于验证 /audit 的租户隔离
CALLERS_X = {
    "k-alice": TrustedCaller("tenant-a", "user", "alice"),
    "k-bob": TrustedCaller("tenant-b", "user", "bob"),
}
A_ALICE = {"Authorization": "Bearer k-alice"}
A_BOB = {"Authorization": "Bearer k-bob"}


def _ask_policy() -> PolicyEngine:
    engine = PolicyEngine()
    engine.add(lambda name, args: PolicyResult(Decision.ASK, "需要人工批准"))
    return engine


def _app(*, api_keys=CALLERS, audit=None, script=None, policy=None):
    return build_app(
        model=ScriptedModel(script or [ChatResponse(content="好", finish_reason="stop")]),
        catalog=weather_tool(),
        policy=policy or PolicyEngine(),
        store=SqliteStore(Path(tempfile.mkdtemp()) / "t.db"),
        api_keys=api_keys,
        audit_store=audit,
    )


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


# ---------- 身份来自凭证 ----------


@pytest.mark.asyncio
async def test_身份由key决定_查询参数被忽略() -> None:
    """bob 传 `?user_id=alice` 也不能把自己变成 alice。"""
    async with _client(_app()) as c:
        # bob 预创建会话，却谎称属于 alice
        r = await c.post("/runs/run-b?user_id=alice", headers=A_BOB)
        assert r.status_code == 200
        assert r.json()["user_id"] == "bob"  # 归属=凭证身份，不是查询参数

        # bob 的对话列表里应有 run-b；即使他请求 `?user_id=alice` 也只看得到自己的
        runs = (await c.get("/runs?user_id=alice", headers=A_BOB)).json()
        assert [x["run_id"] for x in runs] == ["run-b"]
        assert all(x["user_id"] == "bob" for x in runs)


@pytest.mark.asyncio
async def test_首轮对话归属取凭证身份() -> None:
    async with _client(_app()) as c:
        await c.post("/chat/run-b?user_id=alice", json={"text": "hi"}, headers=A_BOB)
        runs = (await c.get("/runs", headers=A_BOB)).json()
        assert runs and runs[0]["user_id"] == "bob"


@pytest.mark.asyncio
async def test_流式对话归属也取凭证身份_查询参数被忽略() -> None:
    """回归：`/chat/stream` 曾直接用 `?user_id=` 写归属，绕过了"身份来自凭证"这条不变量
    （非流式的 `/chat` 走 `_identity` 是对的，只有流式这条漏了）。"""
    async with _client(_app()) as c:
        await c.post("/chat/stream/run-s?user_id=bob", headers=A_ALICE, json={"text": "hi"})
        # run-s 的归属必须是 alice（凭证身份）；bob 读它应 403
        assert (await c.get("/status/run-s", headers=A_BOB)).status_code == 403
        assert (await c.get("/status/run-s", headers=A_ALICE)).status_code == 200


# ---------- 越权拦截 ----------


@pytest.mark.asyncio
async def test_越权读他人会话_403() -> None:
    async with _client(_app()) as c:
        await c.post("/chat/run-bob", json={"text": "hi"}, headers=A_BOB)
        # alice 读 bob 的状态 / 消息 / 事件
        for path in ("/status/run-bob", "/messages/run-bob", "/events/run-bob"):
            r = await c.get(path, headers=A_ALICE)
            assert r.status_code == 403, f"{path} 应被拒"
            assert r.json()["errorCode"] == "AUTHORIZATION_DENIED"


@pytest.mark.asyncio
async def test_越权写他人会话_403() -> None:
    async with _client(_app()) as c:
        await c.post("/chat/run-bob", json={"text": "hi"}, headers=A_BOB)
        r = await c.post("/chat/run-bob", json={"text": "hi"}, headers=A_ALICE)
        assert r.status_code == 403


@pytest.mark.asyncio
async def test_越权删除他人会话_403() -> None:
    async with _client(_app()) as c:
        await c.post("/chat/run-bob", json={"text": "hi"}, headers=A_BOB)
        r = await c.delete("/runs/run-bob", headers=A_ALICE)
        assert r.status_code == 403
        # 未被删除：bob 自己仍能看到
        assert any(
            x["run_id"] == "run-bob"
            for x in (await c.get("/runs", headers=A_BOB)).json()
        )


@pytest.mark.asyncio
async def test_自己名下会话可正常访问() -> None:
    async with _client(_app()) as c:
        await c.post("/chat/run-a", json={"text": "hi"}, headers=A_ALICE)
        assert (await c.get("/status/run-a", headers=A_ALICE)).status_code == 200
        assert (await c.get("/messages/run-a", headers=A_ALICE)).status_code == 200


@pytest.mark.asyncio
async def test_尚不存在的会话可创建_不误拦() -> None:
    """归属校验只针对"已存在且已归属"的 Run —— 否则首次对话会被自己挡死。"""
    async with _client(_app()) as c:
        assert (
            await c.get("/status/brand-new", headers=A_ALICE)
        ).status_code == 200


# ---------- 列表类接口按身份收敛 ----------


@pytest.mark.asyncio
async def test_审批队列只列自己的() -> None:
    script = [
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="c1", name="weather.get", arguments={"city": "上海"})],
            finish_reason="tool_calls"),
    ]
    async with _client(_app(script=script, policy=_ask_policy())) as c:
        await c.post("/chat/run-bob", json={"text": "hi"}, headers=A_BOB)
        # bob 自己的队列里有，alice 的队列里没有
        bob = (await c.get("/approvals", headers=A_BOB)).json()
        alice = (await c.get("/approvals", headers=A_ALICE)).json()
        assert any(a["run_id"] == "run-bob" for a in bob)
        assert alice == []
        # alice 也不能替 bob 批准
        assert (await c.post("/approve/run-bob", headers=A_ALICE)).status_code == 403


@pytest.mark.asyncio
async def test_审批历史只返回自己的() -> None:
    script = [
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="c1", name="weather.get", arguments={"city": "上海"})],
            finish_reason="tool_calls"),
        ChatResponse(content="天晴。", finish_reason="stop"),
    ]
    async with _client(_app(script=script, policy=_ask_policy())) as c:
        await c.post("/chat/run-bob", json={"text": "hi"}, headers=A_BOB)
        await c.post("/approve/run-bob", headers=A_BOB)
        assert (await c.get("/approvals/history", headers=A_BOB)).json()  # bob 有
        assert (await c.get("/approvals/history", headers=A_ALICE)).json() == []


@pytest.mark.asyncio
async def test_审计按租户隔离() -> None:
    audit = InMemoryAuditStore()
    async with _client(_app(api_keys=CALLERS_X, audit=audit)) as c:
        await c.get("/status/run-a", headers=A_ALICE)  # tenant-a
        await c.get("/status/run-b", headers=A_BOB)    # tenant-b
        a = (await c.get("/audit", headers=A_ALICE)).json()
        b = (await c.get("/audit", headers=A_BOB)).json()
        # 只看得到自己租户的记录（/audit 自身那条尚未落库）
        assert a and all(r["tenant_id"] == "tenant-a" for r in a)
        assert b and all(r["tenant_id"] == "tenant-b" for r in b)


@pytest.mark.asyncio
async def test_审计按调用者收敛_同租户也看不到彼此() -> None:
    """回归：多用户默认共用同一租户（`WARDEN_TENANT=local`），只按 tenant 过滤
    等于把同租户别人的操作账本（run_id/身份/路径）交出去，还能枚举账号。"""
    audit = InMemoryAuditStore()
    async with _client(_app(api_keys=CALLERS, audit=audit)) as c:
        await c.get("/status/run-a", headers=A_ALICE)  # alice 与 bob 同属 tenant-a
        await c.get("/status/run-b", headers=A_BOB)
        a = (await c.get("/audit", headers=A_ALICE)).json()
        assert a, "alice 应看到自己的审计记录"
        assert all(r["principal_id"] == "alice" for r in a), a


@pytest.mark.asyncio
async def test_越权403不回显他人身份() -> None:
    """错误信息里的每个字都是给攻击者的情报：不能回显"归属者是谁"（可用于枚举账号）。"""
    async with _client(_app()) as c:
        # run id 故意不含用户名，这样"消息里出现 bob"就只可能来自归属者信息
        await c.post("/chat/run-x", json={"text": "hi"}, headers=A_BOB)
        r = await c.get("/status/run-x", headers=A_ALICE)
        assert r.status_code == 403
        assert "bob" not in r.text, r.text


@pytest.mark.asyncio
async def test_模型切换只影响调用者自己() -> None:
    """回归：`/models/select` 曾调用全局 `set_model`，任一认证用户就能把**所有人**的
    对话切成离线假模型（跨租户 DoS），或切到用自己的 key 计费。"""
    async with _client(_app()) as c:
        r = await c.post("/models/select", json={"id": "fake"}, headers=A_BOB)
        assert r.status_code == 200
        assert (await c.get("/models", headers=A_BOB)).json()["current"] == "fake"
        # alice 不受影响，仍是部署默认
        assert (await c.get("/models", headers=A_ALICE)).json()["current"] != "fake"


@pytest.mark.asyncio
async def test_用户列表只返回自己() -> None:
    async with _client(_app()) as c:
        await c.post("/users", json={"user_id": "bob"}, headers=A_BOB)
        assert (await c.get("/users", headers=A_ALICE)).json() == []
        assert [u["user_id"] for u in (await c.get("/users", headers=A_BOB)).json()] == ["bob"]


@pytest.mark.asyncio
async def test_不能替他人建档() -> None:
    async with _client(_app()) as c:
        assert (
            await c.post("/users", json={"user_id": "alice"}, headers=A_BOB)
        ).status_code == 403


# ---------- 匿名开发模式保持旧行为 ----------


@pytest.mark.asyncio
async def test_未开启认证_查询参数仍生效() -> None:
    """本地开发（api_keys=None）：前端靠 `?user_id=` 切换演示账号，不能被改坏。"""
    async with _client(_app(api_keys=None)) as c:
        r = await c.post("/runs/run-x?user_id=carol")
        assert r.status_code == 200
        assert r.json()["user_id"] == "carol"
        runs = (await c.get("/runs?user_id=carol")).json()
        assert [x["run_id"] for x in runs] == ["run-x"]


@pytest.mark.asyncio
async def test_未开启认证_不按归属拦截() -> None:
    """匿名模式不做租户边界（本就无身份），跨"用户"访问照旧放行。"""
    async with _client(_app(api_keys=None)) as c:
        await c.post("/chat/run-y?user_id=carol", json={"text": "hi"})
        assert (await c.get("/status/run-y?user_id=dave")).status_code == 200
