"""T2 HTTP contract 测试：幂等(Idempotency-Key)、统一版本头、统一错误码。

用 httpx ASGI transport 驱动真实 FastAPI 应用，不真开端口。
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
from warden_agent.web.server import build_app


def _client(script=None):
    store = SqliteStore(Path(tempfile.mkdtemp()) / "t.db")
    s = script or [ChatResponse(content="你好！", finish_reason="stop")]
    app = build_app(
        model=ScriptedModel(s), catalog=weather_tool(),
        policy=PolicyEngine(), store=store,
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

@pytest.mark.asyncio
async def test_所有响应都带版本头() -> None:
    async with _client() as c:
        r = await c.get("/health/live")
        assert r.status_code == 200
        assert r.headers.get("X-Warden-Api-Version") == "1.0"


@pytest.mark.asyncio
async def test_health_ready_也带版本头() -> None:
    async with _client() as c:
        r = await c.get("/health/ready")
        assert r.headers.get("X-Warden-Api-Version") == "1.0"


@pytest.mark.asyncio
async def test_相同_idempotency_key_重复POST返回同一结果() -> None:
    async with _client() as c:
        headers = {"Idempotency-Key": "idem-001"}
        r1 = await c.post("/chat/run-x", json={"text": "hi"}, headers=headers)
        r2 = await c.post("/chat/run-x", json={"text": "hi"}, headers=headers)
        assert r1.status_code == r2.status_code == 200
        assert r1.json() == r2.json()
        assert r1.json()["run_id"] == r2.json()["run_id"]


@pytest.mark.asyncio
async def test_不同_idempotency_key_run不同_各自独立() -> None:
    script = [ChatResponse(content="a", finish_reason="stop"),
              ChatResponse(content="b", finish_reason="stop")]
    async with _client(script) as c:
        r1 = await c.post("/chat/run-y1", json={"text": "hi"},
                          headers={"Idempotency-Key": "k1"})
        r2 = await c.post("/chat/run-y2", json={"text": "hi"},
                          headers={"Idempotency-Key": "k2"})
        assert r1.status_code == r2.status_code == 200
        assert r1.json()["run_id"] == "run-y1"
        assert r2.json()["run_id"] == "run-y2"


@pytest.mark.asyncio
async def test_run已完成_同key仍返回同一结果_不再重跑() -> None:
    """幂等：run 已完成后再用同 key 请求，命中缓存返回原始结果，不会对已结束会话重跑。"""
    async with _client() as c:
        headers = {"Idempotency-Key": "idem-final"}
        r1 = await c.post("/chat/run-f", json={"text": "hi"}, headers=headers)
        r2 = await c.post("/chat/run-f", json={"text": "hi"}, headers=headers)
        assert r1.status_code == r2.status_code == 200
        assert r1.json() == r2.json()
        assert r1.json()["kind"] == "final"


@pytest.mark.asyncio
async def test_错误走统一problem_json含errorCode() -> None:
    """开启认证后，无凭据访问业务接口 → 401 + errorCode=AUTHENTICATION_REQUIRED。"""
    from warden_agent.web.auth import TrustedCaller

    store = SqliteStore(Path(tempfile.mkdtemp()) / "t.db")
    api_keys = {"secret-key": TrustedCaller("tenant", "service", "svc", "cli")}
    app = build_app(
        model=ScriptedModel([ChatResponse(content="hi", finish_reason="stop")]),
        catalog=weather_tool(), policy=PolicyEngine(), store=store,
        api_keys=api_keys,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        r = await c.post("/chat/run-a", json={"text": "hi"})  # 不带 key
        assert r.status_code == 401
        body = r.json()
        assert body["errorCode"] == "AUTHENTICATION_REQUIRED"
        assert body["type"].startswith("urn:warden:problem:")
        assert "correlationId" in body
