"""告警投递通道测试（webhook 用 MockTransport 造假接收端，全程离线）。

本文件里的"令牌"是运行时随机生成的，源码里不含任何真实凭据。
"""

from __future__ import annotations

import json
import secrets

import httpx

from warden_agent.agent import InMemoryRunStore
from warden_agent.core.run.status import AgentRun, RunStatus
from warden_agent.runtime.checkpoint import Checkpoint, InMemoryCheckpointStore
from warden_agent.runtime.notify import (
    NotifyOutcome,
    NullNotifier,
    WebhookNotifier,
    notifier_from_env,
    notify_stuck,
)

_TOK = secrets.token_hex(8)


def test_未配通道是空实现() -> None:
    n = notifier_from_env({})
    assert isinstance(n, NullNotifier)
    assert n.send(title="t", summary="s", details={}) is False


def test_配了通道并解析头() -> None:
    n = notifier_from_env({
        "WARDEN_ALERT_WEBHOOK_URL": "https://hook.example/x",
        "WARDEN_ALERT_WEBHOOK_HEADERS": f"Authorization=Bearer {_TOK}, X-T=1",
    })
    assert isinstance(n, WebhookNotifier)
    assert n.headers == {"Authorization": f"Bearer {_TOK}", "X-T": "1"}


def test_webhook投递成功与失败都不抛() -> None:
    seen: dict[str, object] = {}

    def ok(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200)

    n = WebhookNotifier("https://hook.example/x", transport=httpx.MockTransport(ok))
    assert n.send(title="T", summary="S", details={"count": 1}) is True
    assert seen["body"]["title"] == "T" and seen["body"]["source"] == "warden-agent"

    def bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    n2 = WebhookNotifier("https://hook.example/x", transport=httpx.MockTransport(bad))
    assert n2.send(title="T", summary="S", details={}) is False

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    n3 = WebhookNotifier("https://hook.example/x", transport=httpx.MockTransport(boom))
    assert n3.send(title="T", summary="S", details={}) is False


def _stuck_setup() -> tuple[InMemoryRunStore, InMemoryCheckpointStore]:
    store = InMemoryRunStore()
    store.save_run(AgentRun("run-stuck"))
    cps = InMemoryCheckpointStore()
    cps.save(Checkpoint(
        run_id="run-stuck", status=RunStatus.WAITING_APPROVAL,
        iteration=1, step="awaiting_approval",
    ))
    return store, cps


def test_notify_stuck投递卡死的run() -> None:
    store, cps = _stuck_setup()
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200)

    n = WebhookNotifier("https://hook.example/x", transport=httpx.MockTransport(handler))
    out = notify_stuck(store, cps, n, older_than_seconds=3600)
    # 命中数是 found；**是否真送出去**是 delivered（两者曾经被混为一谈）
    assert (out.found, out.delivered, out.channel) == (1, True, "WebhookNotifier")
    body = captured["body"]
    assert body["details"]["count"] == 1
    assert body["details"]["runs"][0]["run_id"] == "run-stuck"


def test_notify_stuck投递失败必须如实标未投递() -> None:
    """webhook 返 500：命中 1 个，但**没送出去**——不能报成已投递。"""
    store, cps = _stuck_setup()
    n = WebhookNotifier(
        "https://hook.example/x",
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    out = notify_stuck(store, cps, n, older_than_seconds=3600)
    assert (out.found, out.delivered) == (1, False)
    assert out.channel == "WebhookNotifier"


def test_未配通道时命中也不等于已投递() -> None:
    """`NullNotifier.send()` 恒 False（不发任何请求）——命中 1 个但 delivered 必须是 False。"""
    store, cps = _stuck_setup()
    out = notify_stuck(store, cps, NullNotifier(), older_than_seconds=3600)
    assert (out.found, out.delivered, out.channel) == (1, False, "NullNotifier")


def test_outcome不能有真假值_必须显式看delivered() -> None:
    """结构约束：`NotifyOutcome` 故意不定义 `__bool__`。

    否则 `if outcome:` 会重新变成“有命中就算成功”——那正是第五十六批修的 bug。
    """
    assert "__bool__" not in vars(NotifyOutcome)
    assert "__len__" not in vars(NotifyOutcome)


def test_notify_stuck没有卡死就不投递() -> None:
    store = InMemoryRunStore()
    cps = InMemoryCheckpointStore()
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200)

    n = WebhookNotifier("https://hook.example/x", transport=httpx.MockTransport(handler))
    out = notify_stuck(store, cps, n, older_than_seconds=3600)
    assert (out.found, out.delivered) == (0, False)
    assert out.channel == "WebhookNotifier"
    assert called["n"] == 0, "没有卡死 Run 时不应发出任何请求"
