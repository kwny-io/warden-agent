"""轻量链路追踪：W3C Trace Context（`traceparent`）传播 + 结构化 span 日志。

为什么要有它：
  `correlation_id` 是本服务**内部**的请求关联 id，不跨进程、不跨服务。故障排查时
  真正难回答的两个问题是——"这次请求经过了哪些步骤""我调下游的那一次，对应下游
  日志里的哪一条"。回答它们需要一条**跨服务可传递**的链路 id，这正是 W3C Trace
  Context 的定义：`traceparent: 00-<trace-id>-<span-id>-<flags>`。上游带进来、
  我们透传给下游，链路才连得起来。

刻意**不引入 opentelemetry**（本项目保持零重依赖）。这里做的是链路追踪里最要紧、
也最独立的一半：上下文的**生成 / 解析 / 透传**，加上把每个操作记成一行带
`trace_id` 的结构化日志。要把 span 送进 Jaeger/Tempo，接一个 OTel/OTLP exporter
即可——而"上下文不断"是它成立的前提，那部分在这里保证。

约定：
  - 开关 `WARDEN_TRACING`（默认开）。关掉时 `span()` 直接返回，不碰 contextvar，开销接近零。
  - 不合法的 `traceparent`（版本错 / 全零 id / 长度不对）一律**当作没有**，重新起一条新链，
    绝不因为上游脏头就把整条链路搞崩。
  - `trace_id` / `span_id` 用 `secrets` 生成（32/16 位十六进制），满足 W3C 规格。
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from warden_agent.core.settings import env_bool

_logger = logging.getLogger("warden.trace")

# W3C traceparent 的形态：版本-32位traceId-16位spanId-2位flags，全部小写十六进制
_TRACEPARENT_RE = re.compile(
    r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$"
)
_ZERO_TRACE_ID = "0" * 32
_ZERO_SPAN_ID = "0" * 16
_FLAG_SAMPLED = 0x01

# 当前请求/操作的 trace 上下文。用 ContextVar：异步/线程池下各自隔离，不串链。
_current: ContextVar[TraceContext | None] = ContextVar("warden_trace_context", default=None)


@dataclass(frozen=True)
class TraceContext:
    """一条链路上的一个节点。`trace_id` 全链共享，`span_id` 本次操作独有。"""

    trace_id: str
    span_id: str
    sampled: bool = True
    parent_span_id: str | None = None

    def to_traceparent(self) -> str:
        """序列化成可分发给下游的 `traceparent` 头值。"""
        return f"00-{self.trace_id}-{self.span_id}-{'01' if self.sampled else '00'}"


def parse_traceparent(value: str | None) -> TraceContext | None:
    """解析上游传来的 `traceparent`。任何不合法形态都返回 None（当作没有）。"""
    if not value:
        return None
    m = _TRACEPARENT_RE.match(value.strip().lower())
    if m is None:
        return None
    version, trace_id, span_id, flags = m.groups()
    if version == "ff":  # W3C 规定版本 ff 无效
        return None
    if trace_id == _ZERO_TRACE_ID or span_id == _ZERO_SPAN_ID:  # 全零 id 无效
        return None
    return TraceContext(
        trace_id=trace_id,
        span_id=span_id,
        sampled=bool(int(flags, 16) & _FLAG_SAMPLED),
    )


def child_span(parent: TraceContext | None) -> TraceContext:
    """在 parent 之下新建一个子 span：**沿用 trace_id**，换一个新的 span_id。

    parent 为 None 时起一条全新的链（新的 trace_id）。
    """
    if parent is None:
        return TraceContext(trace_id=secrets.token_hex(16), span_id=secrets.token_hex(8))
    return TraceContext(
        trace_id=parent.trace_id,
        span_id=secrets.token_hex(8),
        sampled=parent.sampled,
        parent_span_id=parent.span_id,
    )


def enabled(env: Mapping[str, str] | None = None) -> bool:
    """链路追踪是否开启（`WARDEN_TRACING`，默认开）。"""
    source = os.environ if env is None else env
    return env_bool("WARDEN_TRACING", True, source)


def current() -> TraceContext | None:
    """当前上下文里的 trace 节点（没有则 None）。"""
    return _current.get()


def current_traceparent() -> str | None:
    """当前链路的 `traceparent`，用于给下游出站请求带上——这是"链不中断"的关键一步。"""
    ctx = _current.get()
    return ctx.to_traceparent() if ctx is not None else None


@contextmanager
def _run_span(
    name: str,
    parent: TraceContext | None,
    attributes: Mapping[str, object] | None,
) -> Iterator[TraceContext]:
    """span 的公共实现：设上下文 → 计时 → 结束打一行结构化日志 → 还原上下文。"""
    ctx = child_span(parent)
    token = _current.set(ctx)
    started = time.perf_counter()
    started_ns = time.time_ns()
    try:
        yield ctx
    finally:
        duration_ms = (time.perf_counter() - started) * 1000.0
        extra = " ".join(f"{k}={v}" for k, v in (attributes or {}).items())
        _logger.info(
            "span name=%s trace_id=%s span_id=%s parent_span_id=%s duration_ms=%.1f%s",
            name,
            ctx.trace_id,
            ctx.span_id,
            ctx.parent_span_id or "-",
            duration_ms,
            f" {extra}" if extra else "",
        )
        # 导出到 OTLP 收集器（未配置时为空操作；见 core/otel.py）。
        # 放在日志之后、还原上下文之前；导出失败不影响任何东西。
        from warden_agent.core import otel

        otel.record_span(
            trace_id=ctx.trace_id,
            span_id=ctx.span_id,
            parent_span_id=ctx.parent_span_id,
            name=name,
            start_ns=started_ns,
            end_ns=time.time_ns(),
            attributes=attributes,
        )
        _current.reset(token)


@contextmanager
def span(
    name: str,
    *,
    attributes: Mapping[str, object] | None = None,
    env: Mapping[str, str] | None = None,
) -> Iterator[TraceContext | None]:
    """把一段操作记成一个 span（自动挂到当前上下文之下）。

    关闭追踪时产出 None，且不设置任何上下文——见模块顶部"开销接近零"的说明。
    """
    if not enabled(env):
        yield None
        return
    with _run_span(name, _current.get(), attributes) as ctx:
        yield ctx


@contextmanager
def server_span(
    name: str,
    traceparent: str | None,
    *,
    attributes: Mapping[str, object] | None = None,
    env: Mapping[str, str] | None = None,
) -> Iterator[TraceContext | None]:
    """服务端入口：从入站 `traceparent` 头接着上游的链往下走（没有就起新链）。"""
    if not enabled(env):
        yield None
        return
    with _run_span(name, parse_traceparent(traceparent), attributes) as ctx:
        yield ctx
