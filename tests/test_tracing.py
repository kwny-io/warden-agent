"""链路追踪：W3C traceparent 的解析/生成、上下文传播与 span 的作用域。

重点不在"能解析字符串"，而在几条**容易悄悄错**的约定：
  - 上游脏头（版本错 / 全零 id / 长度不对）不能把链路带崩，要当没有、重新起链；
  - 子 span 必须**沿用 trace_id**（换了就不是同一条链了），只换 span_id；
  - span 结束后必须把上下文还原（否则 trace 会"泄漏"到后续不相干的请求上）。
"""

from __future__ import annotations

from warden_agent.core import tracing

_ON = {}  # 空 env → enabled() 用默认值 "1"（开）
_OFF = {"WARDEN_TRACING": "0"}

# 一个合法的 traceparent（version 00 / 32位 trace / 16位 span / flags 01）
_TP = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def test_解析合法头() -> None:
    ctx = tracing.parse_traceparent(_TP)
    assert ctx is not None
    assert ctx.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert ctx.span_id == "00f067aa0ba902b7"
    assert ctx.sampled is True


def test_解析非法头一律当作没有() -> None:
    bad = [
        None,
        "",
        "garbage",
        "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7",          # 缺 flags
        "ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",       # 版本 ff 无效
        "00-" + "0" * 32 + "-00f067aa0ba902b7-01",                       # trace_id 全零
        "00-4bf92f3577b34da6a3ce929d0e0e4736-" + "0" * 16 + "-01",       # span_id 全零
        "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-zz",       # flags 非十六进制
    ]
    for value in bad:
        assert tracing.parse_traceparent(value) is None, value


def test_序列化与解析可往返() -> None:
    ctx = tracing.parse_traceparent(_TP)
    assert ctx is not None
    again = tracing.parse_traceparent(ctx.to_traceparent())
    assert again is not None
    assert (again.trace_id, again.span_id) == (ctx.trace_id, ctx.span_id)
    assert again.sampled == ctx.sampled


def test_无父时起新链_有父时沿用trace_id() -> None:
    fresh = tracing.child_span(None)
    assert len(fresh.trace_id) == 32 and len(fresh.span_id) == 16
    assert fresh.parent_span_id is None

    parent = tracing.parse_traceparent(_TP)
    assert parent is not None
    child = tracing.child_span(parent)
    assert child.trace_id == parent.trace_id          # 同一条链
    assert child.span_id != parent.span_id            # 但是新的节点
    assert child.parent_span_id == parent.span_id     # 指向父


def test_span_设置上下文并在结束后还原() -> None:
    assert tracing.current() is None
    with tracing.span("a", env=_ON) as ctx:
        assert ctx is not None
        assert tracing.current() is ctx
        assert tracing.current_traceparent() == ctx.to_traceparent()
    # 出作用域必须还原，否则 trace 会泄漏到后续请求
    assert tracing.current() is None


def test_嵌套span共享同一trace_id() -> None:
    with tracing.span("outer", env=_ON) as outer:
        assert outer is not None
        with tracing.span("inner", env=_ON) as inner:
            assert inner is not None
            assert inner.trace_id == outer.trace_id
            assert inner.parent_span_id == outer.span_id
    assert tracing.current() is None


def test_关闭追踪时不产生上下文() -> None:
    with tracing.span("a", env=_OFF) as ctx:
        assert ctx is None
        assert tracing.current() is None
        assert tracing.current_traceparent() is None


def test_服务端span接着上游的链() -> None:
    with tracing.server_span("http.request", _TP, env=_ON) as ctx:
        assert ctx is not None
        assert ctx.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"   # 沿用上游 trace_id
        assert ctx.parent_span_id == "00f067aa0ba902b7"             # 父是上游那个 span


def test_服务端span遇到脏头则起新链() -> None:
    with tracing.server_span("http.request", "not-a-valid-header", env=_ON) as ctx:
        assert ctx is not None
        assert ctx.parent_span_id is None
        assert tracing.parse_traceparent(ctx.trace_id) is None  # 至少是个新生成的有效 id
        assert len(ctx.trace_id) == 32
