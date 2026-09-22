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

# ---- 标签名/值必须**成对**输出（修过的真 bug）----
# 早先只有单标签的测试，而单标签恰好蒙对了 —— 两个及以上标签时名字和值会错位：
# `{method="path",GET="/metrics"}`（名字挤一起、值挤一起），于是任何 `sum by (path)` 都查不到数据。
def test_多标签指标的标签名与值成对() -> None:
    m = _new_registry()
    c = m.counter("req_total", "请求数", ["method", "path"])
    c.inc(labels=("GET", "/metrics"))
    c.inc(labels=("POST", "/chat/run-1"))
    text = m.render()
    assert 'req_total{method="GET",path="/metrics"} 1' in text
    assert 'req_total{method="POST",path="/chat/run-1"} 1' in text
    # 反向断言：错位的形态不该出现
    assert 'method="path"' not in text
    assert "GET=" not in text


def test_三标签也不串位() -> None:
    m = _new_registry()
    g = m.gauge("q_total", "队列", ["a", "b", "c"])
    g.set(7, labels=("1", "2", "3"))
    assert 'q_total{a="1",b="2",c="3"} 7' in m.render()


def test_只给部分标签值时按下标配对() -> None:
    m = _new_registry()
    c = m.counter("part_total", "部分标签", ["k1", "k2"])
    c.inc(labels=("v1",))
    assert 'part_total{k1="v1"} 1' in m.render()


# ---- 直方图语义：累计桶 + +Inf/_count 用总观测数（修过的真 bug）----
def test_直方图桶是累计的且不重复计数() -> None:
    m = _new_registry()
    h = m.histogram("dur_seconds", "耗时", [0.1, 0.5])
    h.observe(0.05)
    h.observe(0.2)
    h.observe(0.9)
    text = m.render()
    assert 'dur_seconds_bucket{le="0.1"} 1' in text      # 只有 0.05 落进来
    assert 'dur_seconds_bucket{le="0.5"} 2' in text      # 累计：0.05 + 0.2
    assert 'dur_seconds_bucket{le="+Inf"} 3' in text     # 全部 3 笔
    assert "dur_seconds_count 3" in text                 # **不是** 6（早先会被算两次）
    assert "dur_seconds_sum 1.15" in text


def test_直方图输出全部声明的桶_含计数为0的() -> None:
    """缺失的桶会被下游算分位数时跳过 → 分位数上偏，而且不报错。"""
    m = _new_registry()
    h = m.histogram("d2_seconds", "耗时", [0.01, 0.1, 1.0])
    h.observe(0.02)                                      # 只落进 le=0.1 与 1.0
    text = m.render()
    assert 'd2_seconds_bucket{le="0.01"} 0' in text
    assert 'd2_seconds_bucket{le="0.1"} 1' in text
    assert 'd2_seconds_bucket{le="1.0"} 1' in text
    assert "d2_seconds_count 1" in text
