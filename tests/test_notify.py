"""告警投递通道测试（webhook 用 MockTransport 造假接收端，全程离线）。

本文件里的"令牌"是运行时随机生成的，源码里不含任何真实凭据。
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from warden_agent.agent import InMemoryRunStore
from warden_agent.core.run.status import AgentRun, RunStatus
from warden_agent.runtime.alerting import ALERT_LIST_LIMIT
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


# ---------------------------------------------------------------------------
# 第五十七批：告警载荷的**体积上界**（消息太大就送不出去，而那正是最需要它的时候）
# ---------------------------------------------------------------------------
def _many_stuck(n: int) -> tuple[InMemoryRunStore, InMemoryCheckpointStore]:
    """n 个卡在等审批的 Run（内存库，不进磁盘）。"""
    store = InMemoryRunStore()
    cps = InMemoryCheckpointStore()
    for i in range(n):
        rid = f"run-{i:04d}"
        store.save_run(AgentRun(rid))
        cps.save(Checkpoint(
            run_id=rid, status=RunStatus.WAITING_APPROVAL,
            iteration=1, step="awaiting_approval",
        ))
    return store, cps


def test_载荷有上界_且少给了必须说出来() -> None:
    """100 个卡死的 Run：载荷不能跟着长（完整载荷 31KB，5000 个时 1.29MB 会被 413），
    但**真实总数与省略个数都要写出来**——不能让人以为就这么多。
    """
    store, cps = _many_stuck(100)
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["raw"] = request.content
        captured["body"] = json.loads(request.content)
        return httpx.Response(200)

    n = WebhookNotifier("https://hook.example/x", transport=httpx.MockTransport(handler))
    out = notify_stuck(store, cps, n, older_than_seconds=3600)
    assert out.delivered is True

    body = captured["body"]
    details = body["details"]
    assert details["count"] == 100, "真实总数不能被截断掉（值班的人要看的就是它）"
    assert details["listed"] == ALERT_LIST_LIMIT
    assert details["omitted"] == 100 - ALERT_LIST_LIMIT
    assert details["truncated"] is True
    assert len(details["runs"]) == ALERT_LIST_LIMIT

    # 体积：未截断时这条请求是 31KB（实测），截断后应在个位数 KB
    raw = captured["raw"]
    assert isinstance(raw, bytes)
    assert len(raw) < 8 * 1024, f"载荷仍可能撑爆接收端：{len(raw)} 字节"
    # 群机器人（企业微信/钉钉）的 text 上限只有 2000~4096 字节
    assert len(body["summary"].encode("utf-8")) < 4096, "文本消息要能进群机器人"
    assert "另有 80 个未列出" in body["summary"], f"少给了要说出来：{body['summary'][-80:]!r}"
    assert body["title"].startswith("[warden] 100 个 Run"), "标题里的总数也要是真实的"


def test_命中数小于上限时不截断_反向控制() -> None:
    """反向控制：只有 3 个时不该出现「另有 N 个未列出」，也不能虚报 truncated。"""
    store, cps = _many_stuck(3)
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200)

    n = WebhookNotifier("https://hook.example/x", transport=httpx.MockTransport(handler))
    out = notify_stuck(store, cps, n, older_than_seconds=3600)
    assert out.delivered is True
    details = captured["body"]["details"]
    assert (details["count"], details["listed"], details["omitted"]) == (3, 3, 0)
    assert details["truncated"] is False
    assert "未列出" not in captured["body"]["summary"]


def test_真HTTP接收端_投递链端到端(caplog: pytest.LogCaptureFixture) -> None:
    """真浏览器/真网络之前，先把**真 HTTP** 这一段验了：起一个本地接收端，真发、真收。

    第五十六批只能写「只往单元层验过（假传输）」；这里用回环上的真 `http.server`
    把链路跑通（不联网、不依赖外部服务）：断言请求真的到了、JSON 能解、状态 200 被当成投递成功。

    为什么要 `caplog`：这条测试在一次冻结全量里红过（`delivered=False`），而当时用的是
    `--tb=line`，只留下一行断言，**真异常没被记下来**，事后无法回溯（只能靠“代理类诱因”
    这个假设）。拉不住异常就等于拉不住根因，所以失败信息里必须带上通道那行 warning。
    """
    received: list[dict[str, object]] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            n = int(self.headers.get("Content-Length", "0"))
            received.append({
                "path": self.path,
                "content_type": self.headers.get("Content-Type"),
                "body": json.loads(self.rfile.read(n)),
            })
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args: object) -> None:  # 不往 stderr 吐行
            return

    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        store, cps = _stuck_setup()
        n = notifier_from_env({"WARDEN_ALERT_WEBHOOK_URL": f"http://127.0.0.1:{port}/hook"})
        with caplog.at_level(logging.WARNING, logger="warden.notify"):
            out = notify_stuck(store, cps, n, older_than_seconds=3600)
        assert (out.found, out.delivered) == (1, True), (
            f"真投递失败：{out}；通道日志={caplog.text!r}"
        )
        assert len(received) == 1, "接收端没收到请求"
        got = received[0]
        assert got["path"] == "/hook"
        assert got["content_type"] == "application/json"
        assert got["body"]["source"] == "warden-agent"
        assert got["body"]["details"]["runs"][0]["run_id"] == "run-stuck"
    finally:
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------------------
# 代理口径（第六十九批）
# ---------------------------------------------------------------------------
# 现场：`cli.py::_client()` 早就写了 `trust_env=False`（注释：避免 127.0.0.1 被代理转发
# 导致 502），而告警投递容器没有同一个决定——同一类失败在 CLI 上是"用户当场看见报错"，
# 在这里**只表现为一行 warning**（`send()` 的取向是"投递失败绝不拖垮业务"），
# 所以它更难被发现。本仓那条"真回环投递"测试就是代理敏感的：
# 实测 `httpx.Client(trust_env=True)` + 一个坏代理 ⇒ 127.0.0.1 的 POST 被送给代理，
# `ConnectError: [WinError 10061]`。默认关掉它，例外用开关显式打开。

_DEAD_PROXY = "http://127.0.0.1:9"  # 9 = discard 端口，本机上必定拒连


def _loopback_receiver() -> tuple[HTTPServer, list[dict[str, object]], str]:
    """起一个只收 POST 的回环接收端，返回 (server, 收到的请求, url)。"""
    received: list[dict[str, object]] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            n = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(n)
            received.append({"path": self.path})
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args: object) -> None:  # 不往 stderr 吐行
            return

    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, received, f"http://127.0.0.1:{srv.server_address[1]}/hook"


def test_默认不看系统代理_有代理变量也直达(monkeypatch: pytest.MonkeyPatch) -> None:
    """行为级守卫：环境里有坏代理时，投递必须仍然直达回环地址。

    不是断言"某个属性是 False"——而是断言**流量真的没被送去代理**。
    这条守卫会红的两种改法：① 把 `trust_env` 的默认改成 True；② 忘了把它传给 httpx.Client。
    """
    monkeypatch.setenv("HTTP_PROXY", _DEAD_PROXY)
    monkeypatch.setenv("HTTPS_PROXY", _DEAD_PROXY)
    srv, received, url = _loopback_receiver()
    try:
        n = notifier_from_env({"WARDEN_ALERT_WEBHOOK_URL": url})
        assert isinstance(n, WebhookNotifier)
        assert n.trust_proxy is False, "默认必须是关的（与 cli.py::_client() 同一口径）"
        assert n.send(title="t", summary="s", details={}) is True, "坏代理不该拦住回环投递"
        assert len(received) == 1, "接收端没收到请求"
    finally:
        srv.shutdown()
        srv.server_close()


def test_开关打开就真的走代理(monkeypatch: pytest.MonkeyPatch) -> None:
    """负面对照：把开关打开，同一个坏代理必须真的把投递打失败。

    没有这条，上面那条可能只是"永远成功"的假绿（本仓在 security job 的
    "永真对照"上已经吃过一次亏）。
    """
    monkeypatch.setenv("HTTP_PROXY", _DEAD_PROXY)
    srv, received, url = _loopback_receiver()
    try:
        n = notifier_from_env({
            "WARDEN_ALERT_WEBHOOK_URL": url,
            "WARDEN_ALERT_WEBHOOK_TRUST_PROXY": "1",
        })
        assert isinstance(n, WebhookNotifier)
        assert n.trust_proxy is True, "开关没被读进去"
        assert n.send(title="t", summary="s", details={}) is False, "应当被代理吃掉"
        assert received == [], "请求不该到达接收端"
    finally:
        srv.shutdown()
        srv.server_close()


def test_投递失败日志自己说出代理这个开关(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """一行 warning 就够定位现场：日志里必须有"哪个变量"和"哪个开关"。

    现场证据：投递失败只记一行 warning，线上没人会盯 `warden stuck --notify` 的日志，
    所以"日志自己说出最可能被误配的那个开关"是这个功能的一部分，不是装饰。
    """
    monkeypatch.setenv("HTTP_PROXY", _DEAD_PROXY)
    n = notifier_from_env({
        "WARDEN_ALERT_WEBHOOK_URL": f"{_DEAD_PROXY}/hook",
        "WARDEN_ALERT_WEBHOOK_TRUST_PROXY": "1",
    })
    with caplog.at_level(logging.WARNING, logger="warden.notify"):
        assert n.send(title="t", summary="s", details={}) is False
    text = caplog.text
    assert "HTTP_PROXY" in text, f"日志没说清是哪个变量：{text!r}"
    assert "经系统代理" in text, f"开关打开时提示应当说\"在经代理\"：{text!r}"

    # 没设代理变量时不许乱指（否则提示会变成噪音，最后没人看）
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    caplog.clear()
    n2 = notifier_from_env({"WARDEN_ALERT_WEBHOOK_URL": f"{_DEAD_PROXY}/hook"})
    with caplog.at_level(logging.WARNING, logger="warden.notify"):
        assert n2.send(title="t", summary="s", details={}) is False
    assert "HTTP_PROXY" not in caplog.text, "没代理变量时不该提代理"


def test_直接构造也默认不走代理(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认参数值本身就是"默认安全"承诺的一半，必须有守卫盯着。

    为什么单列一条：上面那条走的是 `notifier_from_env`（它**显式**传值），
    所以把 `WebhookNotifier.__init__` 的默认值改成 `True` 时它照样绿——
    变异矩阵实测撞到（M6 预期 1 红、实际 0 红）。不经过工厂直接构造是同一份公开 API，
    将来任何一处 `WebhookNotifier(url)` 都会落在那个默认值上。
    """
    monkeypatch.setenv("HTTP_PROXY", _DEAD_PROXY)
    srv, received, url = _loopback_receiver()
    try:
        n = WebhookNotifier(url)
        assert n.trust_proxy is False, "直接构造的默认值必须也是关的"
        assert n.send(title="t", summary="s", details={}) is True, "坏代理不该拦住回环投递"
        assert len(received) == 1, "接收端没收到请求"
    finally:
        srv.shutdown()
        srv.server_close()
