"""告警投递通道：把"需要人管"的结论**真正送出去**（webhook / Alertmanager 兼容）。

为什么需要：`runtime/alerting.py` 只会"判断"哪些 Run 挂太久了——判断出来没人知道也白搭。
项目文档自己就写着"Run 进了等待审批就一直挂着，线上没人盯等于永久卡死"。本模块补上投递：
把卡死的 Run 汇成一条告警，POST 到配置的 webhook（自建告警网关、群机器人的中转、
或 Alertmanager 的 `webhook_configs` 接收端都能收）。

取向与审计 / OTLP 一致：**投递失败绝不拖垮业务**——`send` 返回布尔，异常吞掉并记日志。
但**那个布尔必须被用起来**：`notify_stuck` 返回 `NotifyOutcome`（命中数 + 是否真送出去 + 通道名），
调用方不得把“命中几个 Run”当成“已投递”——两者曾经被混为一谈，CLI 因此在一条都没送出去时
（未配通道的 `NullNotifier`、webhook 连不上、返 500）照样打印「已投递告警」（第五十六批）。
未配 `WARDEN_ALERT_WEBHOOK_URL` → `NullNotifier`（不投递），既有的 CLI/HTTP 查询行为不变。

代理口径（第六十九批补）：投递容器默认 `trust_env=False` —— 告警 webhook 通常是**内网**地址
（自建网关、群机器人中转），被系统代理（含 Windows 注册表里的代理设置）转发一次就是一次
静默失败：现象是连不上 / 502，而日志里只字不提代理。`cli.py::_client()` 早就是这个决定
（`trust_env=False`，见其注释"避免 127.0.0.1 被代理转发导致 502"），而**同一类失败在这里
只表现为一行 warning**，比 CLI 更难查（没有人会盯 `warden stuck --notify` 的日志）。
确实需要走代理的部署（例如只能经代理出网）用 `WARDEN_ALERT_WEBHOOK_TRUST_PROXY=1` 显式打开
——默认安全，例外要写出来。

诚实边界：这里做的是"把结构化告警 POST 出去"，不是完整的告警生命周期管理
（没有去重/抑制/静默/升级策略——那些交给 Alertmanager 那一层做）。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from warden_agent.core.settings import env_flag, env_str

logger = logging.getLogger("warden.notify")

# httpx 的 `trust_env=True` 会读这些变量（Windows 上还会读注册表里的代理设置）。
# 只用于**失败日志自述原因**，不参与任何决策。
_PROXY_ENV_NAMES = (
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
)


def _proxy_env_names() -> list[str]:
    """当前进程里真设了的代理变量名（httpx 的 `trust_env=True` 就读它们）。

    只用于**失败日志自述原因**，不参与任何决策。
    诚实边界：Windows 注册表里的代理设置不在这里检测——没设环境变量时提示不会出现。
    """
    return sorted({name for name in _PROXY_ENV_NAMES if os.environ.get(name)})


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
        trust_proxy: bool = False,
    ) -> None:
        self.url = url
        self.headers = dict(headers or {})
        self.timeout_s = timeout_s
        self._transport = transport
        # 默认不读系统代理：内网 webhook 被代理转发 = 静默失败（见模块 docstring）。
        self.trust_proxy = trust_proxy

    def _proxy_hint(self) -> str:
        """失败时把"代理"这条最可能的原因自己说出来（没设代理变量就返回空串）。

        为什么要这个：告警投递失败**只记一行 warning**（绝不能拖垮业务），所以这行日志往往就
        是线上唯一线索。而容器默认 `trust_env=False`（内网 webhook 不该被系统代理转发），
        于是"环境里有代理 + 目标是内网"就是一种**静默失败**：日志说"连不上"，不说为什么。
        读 `os.environ` 而不是构造时的 env 映射是故意的——`trust_env` 控制的就是 httpx 去读
        进程环境（`os.environ`），提示要说的是**httpx 实际会用到的那些变量**。

        提示**随当前口径变**：开关打开时说"经代理了"（那时代理不可达就是现象本身），
        否则说"没走代理、需要就走就打开开关"——固定一句会变成假话。
        """
        seen = _proxy_env_names()
        if not seen:
            return ""
        names = "/".join(seen)
        if self.trust_proxy:
            return f"（注意：已启用 trust_env，投递会经系统代理 {names}；代理不可达就是这个现象）"
        return (
            f"（注意：环境里有 {names}，但本客户端 trust_env=False，"
            "内网 webhook 默认不走系统代理；确认需要经代理出网就设 "
            "WARDEN_ALERT_WEBHOOK_TRUST_PROXY=1）"
        )

    def send(self, *, title: str, summary: str, details: dict[str, Any]) -> bool:
        import httpx

        payload = {
            "source": "warden-agent",
            "title": title,
            "summary": summary,
            "details": details,
        }
        try:
            with httpx.Client(
                timeout=self.timeout_s,
                transport=self._transport,
                trust_env=self.trust_proxy,
            ) as client:
                resp = client.post(self.url, json=payload, headers=self.headers)
            ok = 200 <= resp.status_code < 300
            if not ok:
                logger.warning(
                    "告警投递返回 status=%s（url=%s）%s",
                    resp.status_code,
                    self.url,
                    self._proxy_hint(),
                )
            return ok
        except Exception:  # noqa: BLE001 - 投递失败绝不能拖垮业务
            logger.warning(
                "告警投递失败（url=%s）%s——只记日志", self.url, self._proxy_hint(), exc_info=True
            )
            return False


def notifier_from_env(env: Mapping[str, str] | None = None) -> Notifier:
    """按环境造告警通道：配了 `WARDEN_ALERT_WEBHOOK_URL` 才投递，否则是空实现。

    `WARDEN_ALERT_WEBHOOK_TRUST_PROXY`（默认 0）决定投递时是否让 httpx 读系统代理。
    """
    url = env_str("WARDEN_ALERT_WEBHOOK_URL", "", env).strip()
    if not url:
        return NullNotifier()
    headers = _parse_headers(env_str("WARDEN_ALERT_WEBHOOK_HEADERS", "", env))
    trust_proxy = env_flag(env_str("WARDEN_ALERT_WEBHOOK_TRUST_PROXY", "", env))
    return WebhookNotifier(url, headers=headers, trust_proxy=trust_proxy)


@dataclass(frozen=True)
class NotifyOutcome:
    """一次告警投递的结果：命中多少、**有没有真的送出去**、走的哪个通道。

    为什么必须有 `delivered`：`Notifier.send()` 返回 `bool`，而它曾经被调用方丢掉——
    于是“未配通道”（`NullNotifier.send()` 恒 `False`）与“webhook 连不上/返 500”
    （也返回 `False`、只记 warning）都被显示成「已投递告警」。

    `found` 是**命中数**（有几个 Run 等着人工处理），不是投递数；
    一个 watcher 只发一条汇总消息，所以 `delivered` 是布尔。`found == 0` 时它恒为 `False`
    （没有要送的东西）——调用方先看 `found` 再看 `delivered`。

    故意**不**定义 `__bool__`：`if outcome:` 会重新变成“有命中就算成功”，那正是原来的 bug。
    """

    found: int
    delivered: bool
    channel: str


def notify_stuck(
    store: object,
    checkpoints: object,
    notifier: Notifier,
    *,
    older_than_seconds: float,
    owner: str | None = None,
) -> NotifyOutcome:
    """把“等待人工超时”的 Run 投递出去；返回 `NotifyOutcome`（命中数 + 是否真投递 + 通道）。

    返回值曾经是 `int`（命中数）且文档写着“投递的 Run 数”——那句话是假的：
    `notifier.send(...)` 的结果被丢掉了，未配通道和投递失败都返回同一个数。
    """
    from warden_agent.runtime.alerting import (
        ALERT_LIST_LIMIT,
        describe_stuck,
        stuck_awaiting_human,
    )

    runs = stuck_awaiting_human(
        store, checkpoints, older_than_seconds=older_than_seconds, owner=owner
    )
    channel = type(notifier).__name__
    if not runs:
        return NotifyOutcome(found=0, delivered=False, channel=channel)
    listed = runs[:ALERT_LIST_LIMIT]
    delivered = bool(
        notifier.send(
            title=f"[warden] {len(runs)} 个 Run 等待人工处理超时",
            summary=describe_stuck(runs),
            # 载荷必须有上界，且**少给了要说出来**：`count` 是真实总数（值班的人要看的就是它），
            # 详细清单只列最该先看的 `ALERT_LIST_LIMIT` 个，个数写在 `listed`/`omitted` 里。
            # 实测：5000 个 Run 时完整载荷是 1.29MB，超 nginx 默认 client_max_body_size=1MB
            # 会被 413 拒绝，且恰好发生在最需要告警的群体性事故里（第五十七批）。
            details={
                "count": len(runs),
                "listed": len(listed),
                "omitted": len(runs) - len(listed),
                "truncated": len(runs) > len(listed),
                "runs": [asdict(r) for r in listed],
            },
        )
    )
    return NotifyOutcome(found=len(runs), delivered=delivered, channel=channel)
