"""`DELETE /runs` —— 批量删除会话（左栏「勾选模式 + 全选」的后端）。

这条路由有一个**结构性**的越权风险，必须用测试钉死它：

网关（`run_server` 的授权中间件）靠 `_extract_run_id(path)` 从**路径**里挖 run_id，
挖不到就 `owner_authorizer` 直接放行。批量删除的 id 全在**请求体**里，路径是光秃秃的
`/runs` → 网关取到 `None` → **归属授权在这条路上等于没开**。

于是端点自己那个 `for run_id in ids: _owner_of(...)` 循环是唯一的防线。下面
`test_混入他人的会话_整批403且一个都没删` 就是守它的：一旦有人为了「性能」把这轮
校验删掉，任何登录用户只要知道别人的 run_id 就能把它连库带记忆一起删掉。

另一半是**原子性**：混入一个别人的 id 时必须一个都不删。逐个删到一半失败会留下
「一部分没了、一部分还在」，而用户无从知道哪些没删——重试和对账都无从下手。
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
from warden_agent.web.auth import ROLE_VIEWER, TrustedCaller
from warden_agent.web.server import build_app

A_ALICE = {"Authorization": "Bearer k-alice"}
A_BOB = {"Authorization": "Bearer k-bob"}
A_VIEW = {"Authorization": "Bearer k-view"}


def _callers() -> dict[str, TrustedCaller]:
    return {
        "k-alice": TrustedCaller("tenant-a", "user", "alice"),
        "k-bob": TrustedCaller("tenant-a", "user", "bob"),
        # 只读账号：能看不能动
        "k-view": TrustedCaller("tenant-a", "user", "viewer1", role=ROLE_VIEWER),
    }


def _client(*, api_keys=_callers()) -> httpx.AsyncClient:
    app = build_app(
        model=ScriptedModel([ChatResponse(content="好", finish_reason="stop")] * 4),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=SqliteStore(Path(tempfile.mkdtemp()) / "t.db"),
        api_keys=api_keys,
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _del(
    c: httpx.AsyncClient, run_ids, headers=None
) -> httpx.Response:
    """发批量删除。httpx 0.28 的 `client.delete()` 不接受请求体，只能走通用 `request()`。"""
    return await c.request("DELETE", "/runs", json={"run_ids": run_ids}, headers=headers)


async def _make_runs(c: httpx.AsyncClient, name: str, ids: list[str]) -> None:
    """预创建会话并且**真的发一轮对话**——只有发过消息，`/runs` 列表里才看得见。"""
    headers = {"Authorization": f"Bearer k-{name}"}
    for run_id in ids:
        await c.post(f"/chat/{run_id}", json={"text": "hi"}, headers=headers)


async def _visible_ids(c: httpx.AsyncClient, name: str) -> set[str]:
    headers = {"Authorization": f"Bearer k-{name}"}
    return {x["run_id"] for x in (await c.get("/runs", headers=headers)).json()}


# ---------- 正常路径 ----------


@pytest.mark.asyncio
async def test_批量删除自己的会话_真的删掉且列表清空() -> None:
    async with _client() as c:
        await _make_runs(c, "alice", ["a1", "a2", "a3"])
        assert await _visible_ids(c, "alice") == {"a1", "a2", "a3"}

        r = await _del(c, ["a1", "a3"], A_ALICE)
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "deleted": ["a1", "a3"], "count": 2}

        # 不看返回值，看**实际状态**：删了的是真没了，没点名的还在
        assert await _visible_ids(c, "alice") == {"a2"}
        # 被删会话的对话内容也该没了。`GET /messages` 会顺手重建一个空会话，
        # 所以断言的是「旧对话不在」而不是「返回空数组」——空会话自带一条 system 提示。
        msgs = (await c.get("/messages/a1", headers=A_ALICE)).json()
        assert not any(m["role"] == "user" for m in msgs), msgs


@pytest.mark.asyncio
async def test_单个会话也能走批量端点() -> None:
    """前端的「单删」也统一走这条（`onDeleted` 收数组）——长度 1 不该是特例。"""
    async with _client() as c:
        await _make_runs(c, "alice", ["a1"])
        r = await _del(c, ["a1"], A_ALICE)
        assert r.status_code == 200
        assert r.json()["deleted"] == ["a1"]


@pytest.mark.asyncio
async def test_重复与空白id被去重去空() -> None:
    """界面上全选后多点几次会重复提交。重复 id 不能变成「删两次」，
    否则返回的 deleted 列表会误导调用方（以为删了 3 个，其实 2 个）。"""
    async with _client() as c:
        await _make_runs(c, "alice", ["a1", "a2"])
        r = await _del(c, ["a1", " a1 ", "", "   ", "a2", "a2"], A_ALICE)
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "deleted": ["a1", "a2"], "count": 2}


@pytest.mark.asyncio
async def test_删不存在的会话是幂等的_不报错() -> None:
    """刚建还没落库归属的会话、或已经被别处删掉的会话，不该让整批失败。"""
    async with _client() as c:
        await _make_runs(c, "alice", ["a1"])
        r = await _del(c, ["a1", "never-existed"], A_ALICE)
        assert r.status_code == 200, r.text
        assert set(r.json()["deleted"]) == {"a1", "never-existed"}


# ---------- 越权（这条路由唯一的防线）----------


@pytest.mark.asyncio
async def test_混入他人的会话_整批403且一个都没删() -> None:
    """**核心回归**：路径里没有 id，网关的归属授权在这里是空转的。

    断言三件事：
      1. 403；
      2. bob 的会话还在（没被删）；
      3. **alice 自己的 a1 也还在**——原子性。删掉一半比干脆拒绝危险得多：
         用户会以为「都失败了」，而实际上自己的数据已经没了。
    """
    async with _client() as c:
        await _make_runs(c, "alice", ["a1"])
        await _make_runs(c, "bob", ["b1"])

        r = await _del(c, ["a1", "b1"], A_ALICE)
        assert r.status_code == 403, r.text
        assert r.json()["errorCode"] == "AUTHORIZATION_DENIED"

        assert await _visible_ids(c, "bob") == {"b1"}, "别人的会话被删了"
        assert await _visible_ids(c, "alice") == {"a1"}, "自己的会话被连带删了"


@pytest.mark.asyncio
async def test_403不回显归属者是谁() -> None:
    """错误信息里的每个字都是给攻击者的情报：能回显归属者就能枚举账号。"""
    async with _client() as c:
        await _make_runs(c, "bob", ["b-x"])
        r = await _del(c, ["b-x"], A_ALICE)
        assert r.status_code == 403
        assert "bob" not in r.text, r.text


@pytest.mark.asyncio
async def test_只读角色批量删_403() -> None:
    """viewer 只有 READ 权限，`DELETE /runs…` 归 `RunOperation.COMMAND` → 403。
    注意收藏路径（`/runs` 无 id）也要被这条拦住，不能因为「没有 run_id」就漏判。"""
    async with _client() as c:
        await _make_runs(c, "alice", ["a1"])
        r = await _del(c, ["a1"], A_VIEW)
        assert r.status_code == 403
        assert await _visible_ids(c, "alice") == {"a1"}


@pytest.mark.asyncio
async def test_未认证_401() -> None:
    async with _client() as c:
        await _make_runs(c, "alice", ["a1"])
        assert (await _del(c, ["a1"])).status_code == 401


# ---------- 输入边界 ----------


@pytest.mark.asyncio
async def test_空列表_400() -> None:
    """空请求多半是前端 bug（勾选状态没同步）。静默返回 200 会掩盖它。"""
    async with _client() as c:
        r = await _del(c, [], A_ALICE)
        assert r.status_code == 400
        assert "不能为空" in r.text


@pytest.mark.asyncio
async def test_全是空白_也按空处理_400() -> None:
    async with _client() as c:
        r = await _del(c, ["", "  "], A_ALICE)
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_超过上限_400而不是硬删() -> None:
    """上限的意义是挡住「一次请求变成长事务」。超限要明确拒绝，不能悄悄截断——
    截断会让用户以为全删了，实际只删了前 200 个。"""
    from warden_agent.web.routes.runs import MAX_BATCH_DELETE

    ids = [f"r{i}" for i in range(MAX_BATCH_DELETE + 1)]
    async with _client() as c:
        r = await _del(c, ids, A_ALICE)
        assert r.status_code == 400
        assert str(MAX_BATCH_DELETE) in r.text


@pytest.mark.asyncio
async def test_缺字段_422() -> None:
    async with _client() as c:
        r = await c.request("DELETE", "/runs", json={}, headers=A_ALICE)
        assert r.status_code == 422


# ---------- 匿名开发模式（与 owner_authorizer 同一口径）----------


@pytest.mark.asyncio
async def test_未开启认证_不做归属拦截() -> None:
    """本地开发没有身份，租户边界本就无从谈起——与单删、跨「用户」读的既有行为一致。"""
    async with _client(api_keys=None) as c:
        await c.post("/chat/anon1", json={"text": "hi"})
        r = await _del(c, ["anon1"])
        assert r.status_code == 200, r.text
