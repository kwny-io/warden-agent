"""T8 可观测性测试：指标注册表 + /metrics 端点。"""
from __future__ import annotations

import tempfile
from pathlib import Path

import httpx
import pytest
from tests.conftest import ScriptedModel, weather_tool

from warden_agent.core.metrics import MetricsRegistry
from warden_agent.policy.policy import PolicyEngine
from warden_agent.store.sqlite import SqliteStore
from warden_agent.web.server import build_app


def _new_registry() -> MetricsRegistry:
    return MetricsRegistry()


# ---- 指标注册表核心行为 ----
def test_counter_increments_and_renders() -> None:
    m = _new_registry()
    c = m.counter("demo_total", "演示计数器", ["kind"])
    c.inc(labels=("a",))
    c.inc(labels=("a",))
    c.inc(labels=("b",))
    text = m.render()
    assert "# HELP demo_total 演示计数器" in text
    assert "# TYPE demo_total counter" in text
    assert 'demo_total{kind="a"} 2' in text
    assert 'demo_total{kind="b"} 1' in text


def test_gauge_set_and_dec() -> None:
    m = _new_registry()
    g = m.gauge("live_total", "瞬时值", ["kind"])
    g.set(5, labels=("x",))
    assert 'live_total{kind="x"} 5' in m.render()
    g.dec(2, labels=("x",))
    assert 'live_total{kind="x"} 3' in m.render()


# ---- /metrics 端点：请求触发指标记录 ----
@pytest.mark.asyncio
async def test_metrics_endpoint_outputs_request_metrics() -> None:
    store = SqliteStore(Path(tempfile.mkdtemp()) / "m.db")
    app = build_app(model=ScriptedModel([]), catalog=weather_tool(),
                    policy=PolicyEngine(), store=store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/health/live")
        resp = await client.get("/metrics")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]
        body = resp.text
        # 请求计数指标 + 健康检查路径的 HELP/TYPE 都应在
        assert "warden_http_requests_total" in body
        assert "/health/live" in body
        assert "warden_http_request_duration_seconds" in body
    store.close()
