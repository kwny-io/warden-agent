"""OTLP 链路导出测试（零依赖实现，用 httpx.MockTransport 造假收集器，不碰网络）。"""

from __future__ import annotations

import json

import httpx
import pytest

from warden_agent.core import otel


def test_配置解析_基地址与traces地址与头() -> None:
    # 都没配 → 不导出
    assert otel.otlp_config_from_env({}) is None

    # 只配基地址 → 自动补 /v1/traces
    cfg = otel.otlp_config_from_env({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"})
    assert cfg is not None
    assert cfg.endpoint == "http://collector:4318/v1/traces"
    assert cfg.service_name == "warden-agent"

    # traces 专用地址优先于基地址
    cfg = otel.otlp_config_from_env({
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://base:4318",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://traces:4318/v1/traces",
        "OTEL_SERVICE_NAME": "warden-test",
        "OTEL_EXPORTER_OTLP_HEADERS": "x-auth=token1, x-tenant=acme",
    })
    assert cfg is not None
    assert cfg.endpoint == "http://traces:4318/v1/traces"
    assert cfg.service_name == "warden-test"
    assert dict(cfg.headers) == {"x-auth": "token1", "x-tenant": "acme"}


def test_导出器把spanPOST到收集器() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cfg = otel.OtlpConfig(
        endpoint="http://collector:4318/v1/traces",
        service_name="warden-test",
        headers=(("x-auth", "tok"),),
    )
    exporter = otel.OtlpExporter(cfg, client=client)
    exporter.submit(otel._Span(
        trace_id="a" * 32, span_id="b" * 16, parent_span_id=None,
        name="http.request", start_ns=1000, end_ns=2000,
        attributes=(("method", "GET"), ("path", "/x")),
    ))
    exporter.close()  # 冲刷并停止后台线程

    body = captured["body"]
    assert captured["url"] == "http://collector:4318/v1/traces"
    resource = body["resourceSpans"][0]["resource"]["attributes"][0]
    assert resource["value"]["stringValue"] == "warden-test"
    spans = body["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert len(spans) == 1
    span = spans[0]
    assert span["traceId"] == "a" * 32
    assert span["spanId"] == "b" * 16
    assert span["name"] == "http.request"
    assert span["startTimeUnixNano"] == "1000"       # 字符串，避免 64 位精度丢失
    assert span["endTimeUnixNano"] == "2000"
    attrs = {a["key"]: a["value"]["stringValue"] for a in span["attributes"]}
    assert attrs == {"method": "GET", "path": "/x"}
    assert "parentSpanId" not in span                # 根 span 不带 parent
    assert captured["headers"].get("x-auth") == "tok"


def test_收集器不可达时不抛异常只计数() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    exporter = otel.OtlpExporter(otel.OtlpConfig(endpoint="http://x/v1/traces"), client=client)
    exporter.submit(otel._Span("a" * 32, "b" * 16, None, "s", 1, 2, ()))
    exporter.close()
    assert exporter.failed >= 1, "失败要被计数，但不能抛异常"


def test_未配置OTLP时record_span是空操作(monkeypatch: pytest.MonkeyPatch) -> None:
    otel.reset_for_tests()
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    try:
        otel.record_span(trace_id="a" * 32, span_id="b" * 16, parent_span_id=None,
                         name="x", start_ns=1, end_ns=2)
        # 单例应为 None（未开启）
        assert otel._exporter_for(None) is None
    finally:
        otel.reset_for_tests()


def test_tracing会把span交给导出器(monkeypatch: pytest.MonkeyPatch) -> None:
    """接线回归：span 结束时 tracing 必须调用 otel.record_span（带正确的链 id）。"""
    captured: list[dict] = []

    def fake_record(**kwargs) -> None:
        captured.append(kwargs)

    monkeypatch.setattr(otel, "record_span", fake_record)

    from warden_agent.core import tracing

    with tracing.span("unit.op", attributes={"k": "v"}) as ctx:
        assert ctx is not None
        inside_trace, inside_span = ctx.trace_id, ctx.span_id

    assert len(captured) == 1
    rec = captured[0]
    assert rec["name"] == "unit.op"
    assert rec["trace_id"] == inside_trace
    assert rec["span_id"] == inside_span
    assert rec["end_ns"] >= rec["start_ns"]
    assert dict(rec["attributes"]) == {"k": "v"}
