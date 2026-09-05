"""阶段13 产品级 HTTP 服务测试：认证 / 审计 / 健康检查。

用 httpx 异步 ASGI transport 走一遍真实 API，覆盖：
  - 认证：无 key 401、错 key 401、对 key 200、公开路径（健康/文档）免认证。
  - problem+json：401 返回 RFC7807 结构 + X-Correlation-Id。
  - 审计：认证后每请求记一条（含调用者/操作/结果状态）；可查询。
  - 健康：/health/live 恒 200；/health/ready 探存储，正常 200、故障 503。
"""

import tempfile
from pathlib import Path

import httpx
import pytest
from tests.conftest import ScriptedModel, weather_tool

from warden_agent.model.model import ChatResponse
from warden_agent.policy.policy import PolicyEngine
from warden_agent.store.sqlite import SqliteStore
from warden_agent.web.audit import InMemoryAuditStore
from warden_agent.web.auth import TrustedCaller
from warden_agent.web.server import build_app

# 一份固定的调用者身份映射（test key → TrustedCaller）
CALLERS = {
    "k-admin": TrustedCaller("tenant-a", "user", "alice"),
    "k-svc": TrustedCaller("tenant-a", "service", "bot-1"),
}


def _store(db_path: Path | None = None) -> SqliteStore:
    return SqliteStore(db_path or Path(tempfile.mkdtemp()) / "t.db")


async def _client(app, **kw):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test", **kw)


def _auth_cfg(*, audit: bool = True):
    return dict(
        api_keys=CALLERS,
        audit_store=InMemoryAuditStore() if audit else None,
    )


# ---------- 认证 ----------


@pytest.mark.asyncio
async def test_无认证访问业务接口_返回401_problem_json() -> None:
    app = build_app(
        model=ScriptedModel([ChatResponse(content="hi", finish_reason="stop")]),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=_store(),
        **_auth_cfg(),
    )
    async with await _client(app) as client:
        r = await client.get("/status/run-1")
        assert r.status_code == 401
        assert r.headers["content-type"].startswith("application/problem+json")
        assert r.headers["X-Correlation-Id"]
        body = r.json()
        assert body["errorCode"] == "AUTHENTICATION_REQUIRED"
        assert body["status"] == 401


@pytest.mark.asyncio
async def test_带合法key_可访问() -> None:
    app = build_app(
        model=ScriptedModel([ChatResponse(content="hi", finish_reason="stop")]),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=_store(),
        **_auth_cfg(),
    )
    async with await _client(app) as client:
        r = await client.get("/status/run-s", headers={"Authorization": "Bearer k-admin"})
        assert r.status_code == 200
        assert r.headers["X-Correlation-Id"]


@pytest.mark.asyncio
async def test_错误key_返回401() -> None:
    app = build_app(
        model=ScriptedModel([ChatResponse(content="hi", finish_reason="stop")]),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=_store(),
        **_auth_cfg(),
    )
    async with await _client(app) as client:
        r = await client.get("/status/run-x", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_公开路径_免认证() -> None:
    app = build_app(
        model=ScriptedModel([ChatResponse(content="hi", finish_reason="stop")]),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=_store(),
        **_auth_cfg(),
    )
    async with await _client(app) as client:
        # 健康检查是负载均衡探活必经之路，必须免认证
        assert (await client.get("/health/live")).status_code == 200
        assert (await client.get("/health/ready")).status_code == 200


@pytest.mark.asyncio
async def test_未开启认证_保持开放() -> None:
    """api_keys=None（默认）= 本地开发开放，不鉴权（向后兼容旧行为）。"""
    app = build_app(
        model=ScriptedModel([ChatResponse(content="hi", finish_reason="stop")]),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=_store(),
    )
    async with await _client(app) as client:
        assert (await client.get("/status/run-o")).status_code == 200


# ---------- 审计 ----------


@pytest.mark.asyncio
async def test_审计记录_每请求一条并含调用者与操作() -> None:
    audit = InMemoryAuditStore()
    app = build_app(
        model=ScriptedModel([ChatResponse(content="hi", finish_reason="stop")]),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=_store(),
        api_keys=CALLERS,
        audit_store=audit,
    )
    async with await _client(app) as client:
        await client.get("/status/run-a", headers={"Authorization": "Bearer k-admin"})
        await client.post(
            "/chat/run-b", json={"text": "hi"}, headers={"Authorization": "Bearer k-svc"}
        )
    records = audit.query()
    assert len(records) == 2
    # 按操作区分：一个 QUERY，一个 SUBMIT_INPUT
    ops = {r.operation for r in records}
    assert ops == {"QUERY", "SUBMIT_INPUT"}
    by_svc = [r for r in records if r.principal_id == "bot-1"]
    assert by_svc and by_svc[0].operation == "SUBMIT_INPUT"
    by_alice = [r for r in records if r.principal_id == "alice"]
    assert by_alice and by_alice[0].operation == "QUERY"
    assert all(r.correlation_id and r.tenant_id == "tenant-a" for r in records)


@pytest.mark.asyncio
async def test_审计可经API查询() -> None:
    audit = InMemoryAuditStore()
    app = build_app(
        model=ScriptedModel([ChatResponse(content="hi", finish_reason="stop")]),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=_store(),
        api_keys=CALLERS,
        audit_store=audit,
    )
    async with await _client(app) as client:
        await client.get("/status/run-q", headers={"Authorization": "Bearer k-admin"})
        r = await client.get("/audit", headers={"Authorization": "Bearer k-admin"})
        assert r.status_code == 200
        data = r.json()
        # 只有上一条 /status 请求的审计可见（/audit 这条正在被读，自己的记录还没落）
        assert len(data) == 1
        assert data[-1]["path"] == "/status/run-q"


@pytest.mark.asyncio
async def test_未开启审计_查询返回404() -> None:
    app = build_app(
        model=ScriptedModel([ChatResponse(content="hi", finish_reason="stop")]),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=_store(),
        api_keys=CALLERS,
        audit_store=None,
    )
    async with await _client(app) as client:
        r = await client.get("/audit", headers={"Authorization": "Bearer k-admin"})
        assert r.status_code == 404


# ---------- 健康检查 ----------


@pytest.mark.asyncio
async def test_liveness_恒200() -> None:
    app = build_app(
        model=ScriptedModel([ChatResponse(content="hi", finish_reason="stop")]),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=_store(),
    )
    async with await _client(app) as client:
        r = await client.get("/health/live")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_readiness_正常200() -> None:
    app = build_app(
        model=ScriptedModel([ChatResponse(content="hi", finish_reason="stop")]),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=_store(),
    )
    async with await _client(app) as client:
        r = await client.get("/health/ready")
        assert r.status_code == 200
        assert r.json()["checks"].get("sqlitestore") == "ok"


def test_readiness_存储故障_返回degraded() -> None:
    """探针层面：存储 ping 抛异常时，readiness 应整体 degraded（健康模块单测）。"""
    from warden_agent.web.health import readiness

    class BrokenStore:
        def ping(self) -> None:
            raise RuntimeError("db down")

    r = readiness(BrokenStore())
    assert r.status == "degraded"
    assert r.checks["brokenstore"] == "unreachable"
