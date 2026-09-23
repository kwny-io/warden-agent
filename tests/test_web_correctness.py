"""Web 层正确性回归：幂等只缓存成功、事件总线接线、5xx 状态回写、指标标签基数、SSE 闸门、指标鉴权。

每条都对应一个**线上会实际咬人**的缺陷，而不是代码风格：
  1. 幂等把 423（Run 锁）/409 也缓存 → 同 key 重试永远命中失败响应；
  2. `WARDEN_EVENT_BUS` / `WARDEN_EVENT_KEEP` 在 run_server 读了却没透传，build_app 另建总线；
  3. 兜底异常返回 500 却不回写状态 → 指标/审计按 200 记；
  4. 指标 `path` 标签用原始 URL（含 run_id）→ 时间序列基数无界；
  5. `/events` 不受 SSE 并发闸门保护 → 闸门形同虚设；
  6. `/metrics` 只要求认证，viewer 也能读运维指标。
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import httpx
import pytest
from tests.conftest import ScriptedModel, weather_tool

from warden_agent.core.metrics import metrics
from warden_agent.model.model import ChatResponse
from warden_agent.policy.policy import PolicyEngine
from warden_agent.store.sqlite import SqliteStore
from warden_agent.web.audit import InMemoryAuditStore
from warden_agent.web.auth import ROLE_ADMIN, ROLE_USER, ROLE_VIEWER, TrustedCaller
from warden_agent.web.coordination import (
    InProcessEventBus,
    InProcessIdempotencyStore,
    InProcessRateLimitStore,
)
from warden_agent.web.server import SseConcurrencyGate, build_app

A_ADMIN = {"Authorization": "Bearer k-admin"}
A_VIEW = {"Authorization": "Bearer k-view"}
A_USER = {"Authorization": "Bearer k-user"}


def _app(model: ScriptedModel | None = None, **kwargs: object):
    store = kwargs.pop("store", None) or SqliteStore(
        Path(tempfile.mkdtemp()) / "t.db"
    )
    return build_app(
        model=model or ScriptedModel([ChatResponse(content="好", finish_reason="stop")] * 4),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=store,  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


def _client(app) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


# ---- 1：幂等只缓存成功（423 不入缓存，重试能继续）----


class _FailOnceRunLock:
    """第一次取锁失败（模拟另一个副本正持有），之后放行——用来制造一个瞬时 423。"""

    def __init__(self) -> None:
        self.calls = 0

    def acquire(self, run_id: str, owner: str, ttl_seconds: int | None = None) -> bool:
        self.calls += 1
        return self.calls > 1

    def renew(self, run_id: str, owner: str, ttl_seconds: int | None = None) -> bool:
        return True

    def release(self, run_id: str, owner: str) -> None:
        return None

    def owner_of(self, run_id: str) -> str | None:
        return "holder" if self.calls <= 1 else None


@pytest.mark.asyncio
async def test_幂等不缓存423_重试可继续() -> None:
    """瞬时 423（Run 锁）不能被写进幂等表，否则同 key 重试永远卡在 423。"""
    model = ScriptedModel([ChatResponse(content="第一次成功", finish_reason="stop")])
    app = _app(model=model, run_lock=_FailOnceRunLock())
    headers = {"Idempotency-Key": "retry-423"}
    async with _client(app) as client:
        r1 = await client.post("/chat/run-lock", json={"text": "hi"}, headers=headers)
        assert r1.status_code == 423, r1.text
        # 释放后同 key 重试：必须真正执行，而不是命中那条 423 缓存
        r2 = await client.post("/chat/run-lock", json={"text": "hi"}, headers=headers)
        assert r2.status_code == 200, r2.text
        assert r2.json()["text"] == "第一次成功"
        # 再发一次：这次是 2xx，才应命中缓存（模型不再被调用）
        r3 = await client.post("/chat/run-lock", json={"text": "hi"}, headers=headers)
        assert r3.status_code == 200 and r3.json() == r2.json()
    assert model.calls == 1, "成功那次只该执行一次；423 那次不执行"


# ---- 2：run_server 把事件总线 / 保留条数透传进 build_app ----


class _FakeSweeper:
    instances: list[_FakeSweeper] = []

    def __init__(self, store, *, interval_s, memory_repository=None,
                 idempotency_ttl_s=0.0, rate_limit_max_age_s=0.0) -> None:  # type: ignore[no-untyped-def]
        self.store = store
        self.interval = interval_s
        self.memory = memory_repository
        self.started = False
        self.stopped = False
        _FakeSweeper.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self, timeout: float = 2.0) -> None:
        self.stopped = True


def test_run_server透传事件总线_日志与生效一致(tmp_path: Path, monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    """run_server 建好的总线必须原样进 build_app，否则 notify/keep 不生效、日志还在撒谎。"""
    import warden_agent.runtime.maintenance as maintenance_mod
    from warden_agent.web import run_server

    created: dict = {}
    captured: dict = {}
    sentinel_bus = InProcessEventBus(keep=9)

    monkeypatch.setattr(run_server, "load_env", lambda: None)
    monkeypatch.setattr(run_server, "setup_logging", lambda: None)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("WARDEN_DB_PATH", str(tmp_path / "srv.db"))
    monkeypatch.setenv("WARDEN_ALLOW_ANON", "1")
    monkeypatch.setenv("WARDEN_EVENT_KEEP", "9")
    monkeypatch.setattr(run_server, "coordination_for", lambda store, **kw: (
        InProcessIdempotencyStore(), sentinel_bus, InProcessRateLimitStore()
    ))
    monkeypatch.setattr(run_server, "build_app", lambda **kw: created.update(kw) or object())

    def _fake_uvicorn_run(app, **kw):  # type: ignore[no-untyped-def]
        captured["app"] = app
        captured.update(kw)

    monkeypatch.setattr(run_server.uvicorn, "run", _fake_uvicorn_run)
    monkeypatch.setattr(maintenance_mod, "MaintenanceSweeper", _FakeSweeper)
    _FakeSweeper.instances.clear()

    with caplog.at_level(logging.INFO, logger="run_server"):
        run_server.main()

    assert created["event_bus"] is sentinel_bus, "生效的总线必须是 run_server 建的那条"
    assert created["event_keep"] == 9, "WARDEN_EVENT_KEEP 必须透传"
    # 启动日志里写的总线类型 == 实际注入的那条（此前日志写 notify、实际却是默认轮询）
    assert type(sentinel_bus).__name__ in caplog.text
    assert captured["app"] is not None

    created["store"].close()
    created["memory_repository"].close()


# ---- 3：兜底异常按 5xx 记入审计 ----


class _BoomStore:
    """委托给真实存储，只在 list_runs 上爆炸——制造一个逃到网关兜底的异常。"""

    def __init__(self, inner: SqliteStore) -> None:
        self._inner = inner

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(self._inner, name)

    def list_runs(self, *a: object, **k: object):  # type: ignore[no-untyped-def]
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_兜底异常按5xx记入审计与指标() -> None:
    inner = SqliteStore(Path(tempfile.mkdtemp()) / "boom.db")
    audit = InMemoryAuditStore()
    app = _app(store=_BoomStore(inner), audit_store=audit)
    async with _client(app) as client:
        r = await client.get("/runs")
    assert r.status_code == 500
    assert r.json()["errorCode"] == "INTERNAL_ERROR"
    records = [rec for rec in audit.query() if rec.path == "/runs"]
    assert records and records[-1].status == 500, records
    # 指标里这条 5xx 必须真的被记成 5xx（而不是 200）
    assert 'warden_http_errors_total{method="GET",path="/runs"}' in metrics().render()
    inner.close()


# ---- 4：指标 path 标签用路由模板，不用原始 URL ----


@pytest.mark.asyncio
async def test_指标path标签用路由模板而非原始id() -> None:
    app = _app()
    async with _client(app) as client:
        assert (await client.get("/status/uniq-abc-123")).status_code == 200
    rendered = metrics().render()
    assert 'path="/status/{run_id}"' in rendered, "应按路由模板打标签"
    assert "/status/uniq-abc-123" not in rendered, "原始 id 不能进标签（基数无界）"


# ---- 5：/events 也受 SSE 并发闸门保护 ----


@pytest.mark.asyncio
async def test_events受SSE并发闸门保护() -> None:
    bus = InProcessEventBus()
    bus.publish("run-ev", {"event": "final", "text": "done"})
    app = _app(event_bus=bus, sse_max_concurrency=1)
    gate: SseConcurrencyGate = app.state.sse_gate
    assert gate.acquire()  # 模拟已有一条长连接在飞
    async with _client(app) as client:
        r = await client.get("/events/run-ev")
        assert r.status_code == 503
        assert "上限" in r.json()["detail"]
    gate.release()
    assert gate.active == 0
    async with _client(app) as client:
        r = await client.get("/events/run-ev")
        assert r.status_code == 200
        assert "done" in r.text
    assert gate.active == 0, "流结束后必须归还名额"


# ---- 6：/metrics 仅管理员可读 ----


@pytest.mark.asyncio
async def test_metrics仅管理员可读() -> None:
    callers = {
        "k-admin": TrustedCaller("t", "user", "admin1", role=ROLE_ADMIN),
        "k-view": TrustedCaller("t", "user", "vera", role=ROLE_VIEWER),
        "k-user": TrustedCaller("t", "user", "bob", role=ROLE_USER),
    }
    app = _app(api_keys=callers)
    async with _client(app) as client:
        assert (await client.get("/metrics")).status_code == 401  # 未认证
        assert (await client.get("/metrics", headers=A_VIEW)).status_code == 403
        assert (await client.get("/metrics", headers=A_USER)).status_code == 403
        ok = await client.get("/metrics", headers=A_ADMIN)
        assert ok.status_code == 200
        assert "warden_http_requests_total" in ok.text
