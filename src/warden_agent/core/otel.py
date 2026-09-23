"""OTLP 链路导出：把 span 送到 OTLP 收集器（Jaeger / Tempo / 托管后端）。

为什么零依赖自己写：本项目坚持零重依赖（`opentelemetry-sdk` 会拖进一大串）。
OTLP/HTTP 的 JSON 协议本身很简单——一次 POST，body 是 `resourceSpans` 数组。
用已有的 httpx 直接发即可，不引入新依赖。

`core/tracing.py` 负责"链上下文不断"（W3C traceparent 生成/解析/透传），本模块
负责把每个结束的 span **导出**到收集器——两件事分开，各自可测。

设计取向与审计一致：**绝不拖垮主路径**
  - **后台批量发送**：span 只入有界队列，守护线程攒批 POST；请求路径不做网络 IO。
  - **尽力而为**：发送失败只记日志，绝不让业务请求失败；队列满则丢弃新 span 并计数告警。
  - 未配 endpoint → 整体关闭，`record_span` 直接返回，开销接近零。

诚实边界：这是"够把链送到收集器"的子集（span：trace/span/parent id、名称、起止时间、
属性、kind）；未做采样策略、metrics/logs 信号、gRPC 协议、mTLS 客户端证书等。
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from warden_agent.core.settings import env_str

logger = logging.getLogger("warden.otel")

_MAX_BATCH = 256      # 单次 POST 最多带多少 span
_QUEUE_MAX = 2048     # 有界队列：收集器不可达时不会把内存吃光
_SPAN_KIND_SERVER = 2


@dataclass(frozen=True)
class OtlpConfig:
    """OTLP 导出配置（已解析）。"""

    endpoint: str
    service_name: str = "warden-agent"
    headers: tuple[tuple[str, str], ...] = ()
    timeout_s: float = 5.0


@dataclass(frozen=True)
class _Span:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    start_ns: int
    end_ns: int
    attributes: tuple[tuple[str, str], ...]


def _parse_headers(raw: str) -> tuple[tuple[str, str], ...]:
    """解析 `k=v,k2=v2` 形态的 OTLP 头。"""
    out: list[tuple[str, str]] = []
    for item in raw.split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        key, _, value = item.partition("=")
        if key.strip():
            out.append((key.strip(), value.strip()))
    return tuple(out)


def otlp_config_from_env(env: Mapping[str, str] | None = None) -> OtlpConfig | None:
    """按环境变量解析 OTLP 配置；没配 endpoint 返回 None（=不导出）。"""
    traces = env_str("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "", env).strip()
    if not traces:
        base = env_str("OTEL_EXPORTER_OTLP_ENDPOINT", "", env).strip()
        if base:
            traces = base.rstrip("/") + "/v1/traces"
    if not traces:
        return None
    service = env_str("OTEL_SERVICE_NAME", "warden-agent", env).strip() or "warden-agent"
    return OtlpConfig(
        endpoint=traces,
        service_name=service,
        headers=_parse_headers(env_str("OTEL_EXPORTER_OTLP_HEADERS", "", env)),
    )


def _span_json(span: _Span) -> dict[str, Any]:
    item: dict[str, Any] = {
        "traceId": span.trace_id,
        "spanId": span.span_id,
        "name": span.name,
        "kind": _SPAN_KIND_SERVER,
        # 时间用字符串：OTLP JSON 的 unix nano 是 64 位整数，JSON number 会丢精度
        "startTimeUnixNano": str(span.start_ns),
        "endTimeUnixNano": str(span.end_ns),
        "attributes": [
            {"key": key, "value": {"stringValue": value}}
            for key, value in span.attributes
        ],
    }
    if span.parent_span_id:
        item["parentSpanId"] = span.parent_span_id
    return item


def _resource_spans(cfg: OtlpConfig, spans: list[_Span]) -> dict[str, Any]:
    """按 OTLP/HTTP JSON 的 schema 组装请求体。"""
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": cfg.service_name}},
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "warden-agent"},
                        "spans": [_span_json(s) for s in spans],
                    }
                ],
            }
        ]
    }


class OtlpExporter:
    """后台批量导出 span 到 OTLP 收集器。

    `client` 可注入（测试用 `httpx.MockTransport` 造一个假收集器，不碰网络）。
    """

    def __init__(self, config: OtlpConfig, *, client: Any = None) -> None:
        self._cfg = config
        self._q: queue.Queue[_Span] = queue.Queue(maxsize=_QUEUE_MAX)
        self._dropped = 0
        self._failed = 0
        # 【O12】内部计数器保护锁：`+=` 是非原子的“读-改-写”，
        # 多线程（submit 的抓取线程 + 后台导出线程）并发下会丢自增。
        # 对外暴露的 Prometheus 指标本身是锁保护的，但内部计数不能因此就不准。
        self._counter_lock = threading.Lock()
        self._stop = threading.Event()
        if client is not None:
            self._client = client
        else:
            import httpx

            self._client = httpx.Client(timeout=config.timeout_s)
        self._thread = threading.Thread(
            target=self._run, name="otlp-exporter", daemon=True
        )
        self._thread.start()

    @property
    def dropped(self) -> int:
        with self._counter_lock:
            return self._dropped

    @property
    def failed(self) -> int:
        with self._counter_lock:
            return self._failed

    def _bump(self, attr: str) -> int:
        """原子地把 `attr` 自增 1 并返回新值（见 _counter_lock 的说明）。"""
        with self._counter_lock:
            value = int(getattr(self, attr)) + 1
            setattr(self, attr, value)
            return value

    def submit(self, span: _Span) -> None:
        """入队一个 span（非阻塞）。队列满则丢弃并计数——绝不阻塞请求路径。"""
        try:
            self._q.put_nowait(span)
        except queue.Full:
            dropped = self._bump("_dropped")
            from warden_agent.core.metrics import note

            note("warden_otlp_dropped_total", "OTLP span 因队列满被丢弃的次数")
            if dropped == 1 or dropped % 100 == 0:
                logger.warning(
                    "OTLP 队列已满，丢弃 span（累计 %d 条）——收集器跟不上或不可达", dropped
                )

    def _take_batch(self, timeout: float) -> list[_Span]:
        batch: list[_Span] = []
        try:
            batch.append(self._q.get(timeout=timeout))
        except queue.Empty:
            return batch
        while len(batch) < _MAX_BATCH:
            try:
                batch.append(self._q.get_nowait())
            except queue.Empty:
                break
        return batch

    def _run(self) -> None:
        while not self._stop.is_set():
            batch = self._take_batch(timeout=1.0)
            if batch:
                self._send(batch)

    def _send(self, batch: list[_Span]) -> None:
        """POST 一批 span。失败只记日志并计数——导出绝不能影响业务。"""
        try:
            resp = self._client.post(
                self._cfg.endpoint,
                json=_resource_spans(self._cfg, batch),
                headers=dict(self._cfg.headers),
            )
            if resp.status_code >= 300:
                failed = self._bump("_failed")
                from warden_agent.core.metrics import note

                note("warden_otlp_export_failures_total", "OTLP 导出失败批次数")
                logger.warning(
                    "OTLP 导出返回 status=%s（累计失败 %d 批）", resp.status_code, failed
                )
        except Exception:  # noqa: BLE001 - 收集器不可达不能拖垮业务
            failed = self._bump("_failed")
            from warden_agent.core.metrics import note

            note("warden_otlp_export_failures_total", "OTLP 导出失败批次数")
            logger.warning(
                "OTLP 导出异常（累计失败 %d 批）——只记日志，不影响业务", failed
            )

    def close(self, timeout: float = 2.0) -> None:
        """停止后台线程，尽量把队列里剩余的 span 发掉。"""
        self._stop.set()
        self._thread.join(timeout)
        leftover: list[_Span] = []
        while True:
            try:
                leftover.append(self._q.get_nowait())
            except queue.Empty:
                break
        if leftover:
            self._send(leftover)
        with contextlib.suppress(Exception):
            self._client.close()


# ---------------------------------------------------------------------------
# 模块级单例（按环境惰性创建；每个进程一个导出器）
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_exporter: OtlpExporter | None = None
_initialized = False


def _exporter_for(env: Mapping[str, str] | None) -> OtlpExporter | None:
    global _exporter, _initialized
    with _lock:
        if not _initialized:
            _initialized = True
            cfg = otlp_config_from_env(env)
            if cfg is not None:
                try:
                    _exporter = OtlpExporter(cfg)
                    logger.info("OTLP 链路导出已开启 endpoint=%s", cfg.endpoint)
                except Exception:  # noqa: BLE001 - 起不来就退回"只打日志"
                    from warden_agent.core.metrics import note

                    note(
                        "warden_otlp_export_init_failures_total",
                        "OTLP 导出器初始化失败次数（失败则链路退回仅结构化日志）",
                    )
                    logger.warning("OTLP 导出器初始化失败，退回仅结构化日志", exc_info=True)
        return _exporter


def record_span(
    *,
    trace_id: str,
    span_id: str,
    parent_span_id: str | None,
    name: str,
    start_ns: int,
    end_ns: int,
    attributes: Mapping[str, object] | None = None,
    env: Mapping[str, str] | None = None,
) -> None:
    """把一个结束的 span 交给导出器（未配置 OTLP 时为空操作）。"""
    exporter = _exporter_for(env)
    if exporter is None:
        return
    exporter.submit(_Span(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        name=name,
        start_ns=start_ns,
        end_ns=end_ns,
        attributes=tuple((str(k), str(v)) for k, v in (attributes or {}).items()),
    ))


def shutdown() -> None:
    """关闭导出器并尽量冲刷队列（停机时调用）。"""
    global _exporter
    with _lock:
        if _exporter is not None:
            _exporter.close()
            _exporter = None


def reset_for_tests() -> None:
    """测试用：丢弃单例状态，让下次 `record_span` 重新按环境初始化。"""
    global _exporter, _initialized
    with _lock:
        if _exporter is not None:
            _exporter.close()
        _exporter = None
        _initialized = False
