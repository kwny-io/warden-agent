"""结构化日志的契约测试（审计发现 I1："日志非结构化，trace_id 只拼进 message"）。

这里断言的几件事，恰好是"日志在采集端有没有用"的最低门槛：
  1. 一条日志一行 JSON，字段可被解析（否则 Loki/ELK 只能当纯文本 grep）；
  2. **span 内任何模块**打的日志都带 `trace_id`（否则串不起一次请求）；
  3. 异常栈也在同一个对象里（不破坏"一行一条"）；
  4. 消息与异常栈都过一遍脱敏（免得某个调用点忘了）；
  5. 格式配置写错时**报错**而不是静默按默认值跑。
"""

from __future__ import annotations

import io
import json
import logging

import pytest

from warden_agent.core import tracing
from warden_agent.core.logging_setup import (
    JsonFormatter,
    get_logger,
    resolve_log_format,
    setup_logging,
)


def _capture(fmt: str) -> tuple[io.StringIO, logging.Logger]:
    """把根 logger 指到一个内存流上，返回流与一个子 logger。"""
    buf = io.StringIO()
    setup_logging(logging.INFO, fmt=fmt, stream=buf)
    return buf, get_logger("warden_agent.test")


def _lines(buf: io.StringIO) -> list[str]:
    return [ln for ln in buf.getvalue().splitlines() if ln.strip()]


# --------------------------------------------------------------------------
# 格式选择：显式 > 猜（是否为终端）
# --------------------------------------------------------------------------
def test_显式配置优先且大小写空白归一() -> None:
    assert resolve_log_format({"WARDEN_LOG_FORMAT": "JSON"}) == "json"
    assert resolve_log_format({"WARDEN_LOG_FORMAT": " text "}) == "text"


def test_配置写错时报错而不是静默用默认值() -> None:
    with pytest.raises(ValueError, match="WARDEN_LOG_FORMAT"):
        resolve_log_format({"WARDEN_LOG_FORMAT": "jsom"})  # 拼错


def test_没配置时看是不是终端() -> None:
    assert resolve_log_format({}, isatty=True) == "text"       # 开发机：给人看
    assert resolve_log_format({}, isatty=False) == "json"      # 容器/CI：给机器看


# --------------------------------------------------------------------------
# JSON 形态
# --------------------------------------------------------------------------
def test_一条日志一行且能被解析成对象() -> None:
    buf, log = _capture("json")
    log.info("hello %s", "world")
    lines = _lines(buf)
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["msg"] == "hello world"      # 占位符已渲染
    assert payload["level"] == "INFO"
    assert payload["logger"] == "warden_agent.test"
    assert payload["ts"].endswith("Z")          # ISO8601 UTC（毫秒）
    assert "T" in payload["ts"]


def test_调用点自定义字段会变成顶层字段() -> None:
    buf, log = _capture("json")
    log.info("run 完成", extra={"session_id": "s-1", "event": "run.finished"})
    payload = json.loads(_lines(buf)[0])
    assert payload["session_id"] == "s-1"
    assert payload["event"] == "run.finished"


def test_中文不被转义() -> None:
    """`ensure_ascii=False`：采集端能收 UTF-8，人也看得懂。"""
    buf, log = _capture("json")
    log.info("审批被拒绝：%s", "fs.delete")
    assert "审批被拒绝" in _lines(buf)[0]


def test_span内任何模块的日志都带trace_id() -> None:
    """审计原状况：只有 span 结束那一行自己有 trace_id，别的模块完全没有。"""
    buf, _ = _capture("json")
    other_module_logger = get_logger("warden_agent.some.other.module")
    with tracing.span("unit-test-span") as ctx:
        other_module_logger.info("这条来自完全无关的模块")
        payload = json.loads(_lines(buf)[0])
    assert payload["trace_id"] == ctx.trace_id
    assert payload["span_id"] == ctx.span_id


def test_span外的日志没有trace_id字段() -> None:
    buf, log = _capture("json")
    log.info("启动完成")
    assert "trace_id" not in json.loads(_lines(buf)[0])


def test_异常栈在同一个对象里且仍是一行() -> None:
    buf, log = _capture("json")
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        log.exception("处理失败")
    lines = _lines(buf)
    assert len(lines) == 1                        # 一行一条没被栈破坏
    payload = json.loads(lines[0])
    assert "RuntimeError: boom" in payload["exc"]
    assert "Traceback" in payload["exc"]


def test_消息里的密钥会被脱敏() -> None:
    buf, log = _capture("json")
    secret = "sk-ant-api03-A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"
    log.info("用这个 key 去调用：%s", secret)
    line = _lines(buf)[0]
    assert secret not in line
    assert "<redacted:" in line


def test_异常栈里的密钥也会被脱敏() -> None:
    """调用点忘了脱敏时，格式化层兜底。"""
    buf, log = _capture("json")
    secret = "sk-ant-api03-Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4"
    try:
        raise RuntimeError(f"上游返回 401，用的 key 是 {secret}")
    except RuntimeError:
        log.exception("调用失败")
    assert secret not in _lines(buf)[0]


# --------------------------------------------------------------------------
# 文本档（开发机）与接线
# --------------------------------------------------------------------------
def test_文本档保持人可读且不输出花括号() -> None:
    buf, log = _capture("text")
    log.warning("磁盘快满了")
    line = _lines(buf)[0]
    assert "WARNING" in line
    assert "warden_agent.test: 磁盘快满了" in line
    assert not line.startswith("{")


def test_重复调用不会重复挂handler() -> None:
    """`setup_logging` 会重设 handler（而不是再挂一个），所以换流后旧流不再收到。

    断言的是"不会重复输出"这个真实行为；两个 handler 都挂着的话，
    生产里会出现同一条日志被采集两遍（告警重复、计数翻倍）。
    """
    first = io.StringIO()
    second = io.StringIO()
    setup_logging(logging.INFO, fmt="json", stream=first)
    setup_logging(logging.INFO, fmt="json", stream=second)
    get_logger("warden_agent.test").info("只该出现一次")
    assert len(logging.getLogger().handlers) == 1
    assert _lines(first) == []          # 旧 handler 已被替换
    assert len(_lines(second)) == 1     # 新 handler 只输出一次


def test_未知格式在安装阶段就报错() -> None:
    with pytest.raises(ValueError, match="未知日志格式"):
        setup_logging(logging.INFO, fmt="yaml")


def test_formatter能单独使用() -> None:
    """允许别人按需只借这个 formatter（比如给 uvicorn 的 access log 换格式）。"""
    record = logging.LogRecord(
        name="warden_agent.x", level=logging.ERROR, pathname=__file__, lineno=1,
        msg="直接格式化 %s", args=("ok",), exc_info=None,
    )
    payload = json.loads(JsonFormatter().format(record))
    assert payload["msg"] == "直接格式化 ok"
    assert payload["level"] == "ERROR"
