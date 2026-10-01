"""日志规范化：统一配置输出格式与级别。


原则：
  1. 统一用标准 logging，不 print。
  2. **一条日志一行，机器可读**：生产（容器）默认输出 JSON，一行一个对象，可以
     直接被 Loki/ELK/CloudWatch 采集成字段；开发机（有终端）默认输出人眼看的文本。
  3. 关键事件（审批、DENY、完成、失败）用 info/warning 级别，方便追踪。
  4. 绝不把 API Key、完整 Prompt 写进日志（安全）——并在**格式化这一层**再兜一次底：
     消息与异常栈都会过 `core.redaction.redact_text`，免得某个调用点忘了脱敏。


## 为什么日志格式要分两档（而不是"统一 JSON"）

结构化日志的价值全在采集端：没有字段就没法按 `trace_id` 串一次请求、没法按 `level`
做告警、也没法按 `session_id` 找一次会话。但把 JSON 强塞给人看是自伤——排查问题的人
得先在脑子里反序列化。所以默认值是**看环境**：stdout 是终端（交互开发）给文本，
不是终端（容器/管道/CI 重定向）给 JSON。这样"上线忘了配"的默认行为是安全的，
而本地开发也不用先 export 一堆变量。


## 为什么 `trace_id` 从 contextvar 取，而不是拼进消息

原来 `core/tracing.py` 只把 `span name=... trace_id=...` 拼进**那一行自己的消息**里，
于是同一次请求里其它模块打的日志完全没有 trace_id —— 想在 Loki 里捞一次请求的全貌
就得靠时间窗猜。改成格式化层直接读 `tracing.current()`（ContextVar，异步/线程池各自
隔离），**span 内任何一行日志自动带上 trace_id/span_id**，且消息本身不再被污染。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any

# 统一的文本格式：时间 + 级别 + 模块 + 消息
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

#: 合法的 `WARDEN_LOG_FORMAT` 取值
LOG_FORMATS = ("text", "json")

#: 这些是 logging 自己塞进 record 的标准属性；剩下的当成"调用点自定义字段"输出。
_STANDARD_ATTRS = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
        "levelname", "levelno", "lineno", "message", "module", "msecs", "msg", "name",
        "pathname", "process", "processName", "relativeCreated", "stack_info",
        "taskName", "thread", "threadName",
    }
)


def resolve_log_format(env: dict[str, str] | None = None, *, isatty: bool | None = None) -> str:
    """决定用哪一档格式。

    显式配了 `WARDEN_LOG_FORMAT` 就用它（大小写与空白归一）；**写错直接报错**——
    静默按默认值跑会让人以为自己配的格式生效了，等到出事去查日志才发现采集端一无所获。
    没配时看 stdout 是不是终端：是终端给 text（人看），不是给 json（机器看）。
    """
    source = os.environ if env is None else env
    raw = str(source.get("WARDEN_LOG_FORMAT", "") or "").strip().lower()
    if raw:
        if raw not in LOG_FORMATS:
            raise ValueError(
                f"WARDEN_LOG_FORMAT 取值非法：{raw!r}（合法值：{' / '.join(LOG_FORMATS)}）"
            )
        return raw
    if isatty is None:
        isatty = bool(getattr(sys.stdout, "isatty", lambda: False)())
    return "text" if isatty else "json"


def _redact(text: str) -> str:
    """格式化层兜底脱敏。导入失败不能让日志本身挂掉。"""
    try:
        from warden_agent.core.redaction import redact_text

        return redact_text(text)
    except Exception:  # pragma: no cover - 只在循环导入/极端环境下触发
        return text


def _trace_fields() -> dict[str, str]:
    """当前 span 的 trace_id/span_id（没有就返回空）。"""
    try:
        from warden_agent.core.tracing import current

        ctx = current()
    except Exception:  # pragma: no cover
        return {}
    if ctx is None:
        return {}
    return {"trace_id": ctx.trace_id, "span_id": ctx.span_id}


class JsonFormatter(logging.Formatter):
    """一行一个 JSON 对象。

    字段：`ts`（ISO8601 UTC，毫秒）、`level`、`logger`、`msg`、`trace_id`/`span_id`
    （在 span 内时），以及调用点通过 `extra=` 传进来的自定义字段（如 `session_id`）。
    异常栈放在 `exc` 字段里（同样是**一个**对象里的一个字符串，不会破坏"一行一条"）。
    """

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003 - 覆写标准接口
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": _redact(record.getMessage()),
        }
        payload.update(_trace_fields())
        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key.startswith("_") or key in payload:
                continue
            payload[key] = _redact(value) if isinstance(value, str) else value
        if record.exc_info:
            payload["exc"] = _redact(self.formatException(record.exc_info))
        if record.stack_info:
            payload["stack"] = _redact(record.stack_info)
        # ensure_ascii=False：中文别被转成 \uXXXX（采集端能处理 UTF-8，人也看得懂）
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def setup_logging(
    level: int = logging.INFO,
    *,
    fmt: str | None = None,
    stream: Any = None,
) -> None:
    """配置根 logger。应用启动时调用一次即可。

    `fmt` 留空 = 按 `WARDEN_LOG_FORMAT` / 是否终端自动决定（见 `resolve_log_format`）。
    """
    chosen = resolve_log_format() if fmt is None else fmt
    if chosen not in LOG_FORMATS:
        raise ValueError(f"未知日志格式：{chosen!r}（合法值：{' / '.join(LOG_FORMATS)}）")
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonFormatter() if chosen == "json" else logging.Formatter(_FORMAT))
    root = logging.getLogger()
    root.handlers.clear()  # 避免重复添加
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    """取一个子 logger（带模块名）。"""
    return logging.getLogger(name)
