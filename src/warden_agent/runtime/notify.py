"""告警投递通道：把"需要人管"的结论**真正送出去**（webhook / Alertmanager 兼容）。

为什么需要：`runtime/alerting.py` 只会"判断"哪些 Run 挂太久了——判断出来没人知道也白搭。
项目文档自己就写着"Run 进了等待审批就一直挂着，线上没人盯等于永久卡死"。本模块补上投递：
把卡死的 Run 汇成一条告警，POST 到配置的 webhook（自建告警网关、群机器人的中转、
或 Alertmanager 的 `webhook_configs` 接收端都能收）。

取向与审计 / OTLP 一致：**投递失败绝不拖垮业务**——`send` 返回布尔，异常吞掉并记日志。
未配 `WARDEN_ALERT_WEBHOOK_URL` → `NullNotifier`（不投递），既有的 CLI/HTTP 查询行为不变。

诚实边界：这里做的是"把结构化告警 POST 出去"，不是完整的告警生命周期管理
（没有去重/抑制/静默/升级策略——那些交给 Alertmanager 那一层做）。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any, Protocol

from warden_agent.core.settings import env_str

logger = logging.getLogger("warden.notify")


class Notifier(Protocol):
    """告警投递口。实现必须是"尽力而为"的：失败返回 False，不抛。"""

    def send(self, *, title: str, summary: str, details: dict[str, Any]) -> bool: ...


class NullNotifier:
    """不投递（未配置通道时的默认）。保留既有"查询式告警"行为。"""

    requires_network = False

    def send(self, *, title: str, summary: str, details: dict[str, Any]) -> bool:
        return False


def _parse_headers(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if item and "=" in item:
            key, _, value = item.partition("=")
            if key.strip():
                out[key.strip()] = value.strip()
    return out


class WebhookNotifier:
    """把告警 POST 成 JSON 到 webhook。

    载荷形态：`{"source":"warden-agent","title":...,"summary":...,"details":{...}}`。
    接收端可以是一个通用网关，也可以包一层转成 Alertmanager 的告警格式。

    测试可注入 `transport`（httpx.BaseTransport）→ 全程离线、确定。
    """

    requires_network = True

    def __init__(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_s: float = 5.0,
        transport: Any = None,
    ) -> None:
        self.url = url
        self.headers = dict(headers or {})
        self.timeout_s = timeout_s
        self._transport = transport

    def send(self, *, title: str, summary: str, details: dict[str, Any]) -> bool:
        import httpx

        payload = {
            "source": "warden-agent",
            "title": title,
            "summary": summary,
            "details": details,
        }
        try:
            with httpx.Client(timeout=self.timeout_s, transport=self._transport) as client:
                resp = client.post(self.url, json=payload, headers=self.headers)
            ok = 200 <= resp.status_code < 300
            if not ok:
                logger.warning("告警投递返回 status=%s（url=%s）", resp.status_code, self.url)
            return ok
        except Exception:  # noqa: BLE001 - 投递失败绝不能拖垮业务
            logger.warning("告警投递失败（url=%s）——只记日志", self.url, exc_info=True)
            return False


def notifier_from_env(env: Mapping[str, str] | None = None) -> Notifier:
    """按环境造告警通道：配了 `WARDEN_ALERT_WEBHOOK_URL` 才投递，否则是空实现。"""
    url = env_str("WARDEN_ALERT_WEBHOOK_URL", "", env).strip()
    if not url:
        return NullNotifier()
    headers = _parse_headers(env_str("WARDEN_ALERT_WEBHOOK_HEADERS", "", env))
    return WebhookNotifier(url, headers=headers)


def notify_stuck(
    store: object,
    checkpoints: object,
    notifier: Notifier,
    *,
    older_than_seconds: float,
    owner: str | None = None,
) -> int:
    """把"等待人工超时"的 Run 投递出去；返回投递的 Run 数（0 = 没有，未投递）。"""
    from warden_agent.runtime.alerting import describe_stuck, stuck_awaiting_human

    runs = stuck_awaiting_human(
        store, checkpoints, older_than_seconds=older_than_seconds, owner=owner
    )
    if not runs:
        return 0
    notifier.send(
        title=f"[warden] {len(runs)} 个 Run 等待人工处理超时",
        summary=describe_stuck(runs),
        details={"count": len(runs), "runs": [asdict(r) for r in runs]},
    )
    return len(runs)
