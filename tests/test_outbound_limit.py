"""出站限速/配额测试：给"Agent 主动往外发请求"的总量加闸。

背景：`web.fetch` 此前只有**单次请求**的边界（超时 10s / 响应体 200KB / 跳转 3 次），
它们只界定"一次"，不界定"多少次"——模型一轮抓 50 个链接、并发用户相乘、或打同一个站点
打到被 429，都没有东西拦。这里锁住四道闸的行为：

  - 全局速率 / 单 host 速率 / 并发上限 / 日配额；
  - 计数走 `RateLimitStore` → 多副本下换存储实现才是**全局**限额（用同一 SqliteStore 验证共享）；
  - **离线 provider 不占配额**（演示与测试不受影响）；
  - 被限流是**返回可读文本**而不是抛异常（工具不该把会话循环打崩）；
  - **URL 策略先于出站闸门**：被策略拒掉的 URL 不消耗配额。
"""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path

import pytest

from warden_agent.store.sqlite import SqliteStore
from warden_agent.tool.catalog import ToolCatalog
from warden_agent.web.coordination import InProcessRateLimitStore, SqlRateLimitStore
from warden_agent.web.outbound import (
    OutboundConfig,
    OutboundLimiter,
    outbound_from_env,
    parse_outbound_limit,
)
from warden_agent.web.search import LocalMockFetchProvider, WebFetchResult, make_web_tools


class _Clock:
    """可手动推进的时钟，让窗口边界可确定地测。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class _StubOnlineFetch:
    """假的联网抓取 provider：声明 requires_network=True，但不真发请求。"""

    requires_network = True

    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch(self, url: str) -> WebFetchResult:
        self.calls.append(url)
        return WebFetchResult(url=url, status=200, content=f"正文#{len(self.calls)}")


class _StubPolicy:
    """可切换放行/拒绝的 URL 策略桩（避免测试依赖真实 DNS）。"""

    def __init__(self, allow: bool = True) -> None:
        self.allow = allow

    def check(self, url: str) -> tuple[bool, str]:
        return True, "ok"

    def check_for_network(self, url: str) -> tuple[bool, str]:
        return (True, "ok") if self.allow else (False, "目标地址不是公网地址")


def _catalog(provider, policy=None, limiter=None) -> ToolCatalog:
    catalog = ToolCatalog()
    for spec in make_web_tools(None, provider, url_policy=policy, outbound=limiter):
        catalog.register(spec)
    return catalog


# ---------- 四道闸 ----------


def test_全局速率到上限后拒绝() -> None:
    lim = OutboundLimiter(
        OutboundConfig(max_requests=2, window_seconds=60), clock=_Clock()
    )
    assert lim.acquire("a.example").allowed
    lim.release()
    assert lim.acquire("b.example").allowed
    lim.release()
    denied = lim.acquire("c.example")
    assert denied.denied
    assert "出站总速率" in denied.reason
    assert denied.retry_after >= 1


def test_窗口过后额度恢复() -> None:
    clock = _Clock()
    lim = OutboundLimiter(OutboundConfig(max_requests=1, window_seconds=60), clock=clock)
    lim.acquire("a.example")
    lim.release()
    assert lim.acquire("a.example").denied
    clock.advance(61)  # 下一个窗口
    assert lim.acquire("a.example").allowed
    lim.release()


def test_单host限速只打该host不影响别家() -> None:
    lim = OutboundLimiter(
        OutboundConfig(
            max_requests=100, window_seconds=60,
            host_max_requests=1, host_window_seconds=60,
        ),
        clock=_Clock(),
    )
    lim.acquire("busy.example")
    lim.release()
    # 同一个 host 第二次被拒
    denied = lim.acquire("busy.example")
    assert denied.denied and "过于频繁" in denied.reason
    # 另一个 host 不受影响（否则就是把全局额度错按在 host 桶上）
    assert lim.acquire("quiet.example").allowed
    lim.release()


def test_并发上限_释放后可再用() -> None:
    lim = OutboundLimiter(OutboundConfig(max_concurrency=1), clock=_Clock())
    assert lim.acquire("a.example").allowed  # 占住唯一槽位（不释放）
    denied = lim.acquire("b.example")
    assert denied.denied and "并发" in denied.reason
    lim.release()  # 归还后应能再拿到
    assert lim.acquire("b.example").allowed
    lim.release()


def test_日配额用尽后拒绝() -> None:
    lim = OutboundLimiter(
        OutboundConfig(
            max_requests=100, window_seconds=60,
            host_max_requests=100, host_window_seconds=60,
            daily_quota=3,
        ),
        clock=_Clock(),
    )
    for i in range(3):
        assert lim.acquire(f"h{i}.example").allowed
        lim.release()
    denied = lim.acquire("h9.example")
    assert denied.denied and "配额" in denied.reason


def test_release幂等不会把信号量越还越多() -> None:
    lim = OutboundLimiter(OutboundConfig(max_concurrency=1))
    lim.acquire("a.example")
    lim.release()
    lim.release()  # 未持有时的释放应被忽略，而不是抛 BoundedSemaphore 溢出
    assert lim.acquire("a.example").allowed
    lim.release()


def test_多线程下不串号() -> None:
    """限流器会被多个线程共享：每个线程只该归还自己占的槽位。"""
    lim = OutboundLimiter(OutboundConfig(max_concurrency=1))
    assert lim.acquire("main.example").allowed  # 主线程占位，不释放
    results: list[bool] = []

    def worker() -> None:
        d = lim.acquire("worker.example")
        results.append(d.allowed)
        if d.allowed:  # 正常情况下拿不到（槽位被主线程占着）
            lim.release()

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert results == [False]  # 子线程被拒，且它的 release 不该误伤主线程的槽位
    lim.release()
    assert lim.acquire("after.example").allowed
    lim.release()


# ---------- 与存储接缝联动（多副本语义） ----------


def test_共享存储下两个限流器共用额度() -> None:
    """同一把 store 造出的两个限流器 = 两个副本；额度必须是共享的，不能各算一份。"""
    store = SqliteStore(Path(tempfile.mkdtemp()) / "rl.db")
    shared = SqlRateLimitStore(store)
    cfg = OutboundConfig(max_requests=1, window_seconds=60)
    repl_a = OutboundLimiter(cfg, store=shared, clock=_Clock())
    repl_b = OutboundLimiter(cfg, store=shared, clock=_Clock())

    assert repl_a.acquire("h.example").allowed  # 副本 A 用掉唯一额度
    repl_a.release()
    assert repl_b.acquire("h.example").denied  # 副本 B 看到的是同一份计数


def test_进程内存储不共享_如实暴露() -> None:
    """对照组：不共享会怎样——两个进程内限流器各自算一份，实际额度翻倍。"""
    cfg = OutboundConfig(max_requests=1, window_seconds=60)
    a = OutboundLimiter(cfg, store=InProcessRateLimitStore(), clock=_Clock())
    b = OutboundLimiter(cfg, store=InProcessRateLimitStore(), clock=_Clock())
    assert a.acquire("h.example").allowed
    a.release()
    assert b.acquire("h.example").allowed  # 各算一份 → 两个副本合起来是 2 次
    b.release()


# ---------- 工具层接线 ----------


def test_离线provider不占配额() -> None:
    """默认离线 mock 声明了 requires_network=False：演示/测试不该被出站配额影响。"""
    lim = OutboundLimiter(
        OutboundConfig(max_requests=1, window_seconds=60), clock=_Clock()
    )
    provider = LocalMockFetchProvider({"https://x.example/": "页面正文"})
    catalog = _catalog(provider, limiter=lim)
    for _ in range(5):  # 连抓 5 次，一次都不该被限流
        assert "页面正文" in str(catalog.execute("web.fetch", {"url": "https://x.example/"}))


def test_联网抓取受全局限速且返回可读文本() -> None:
    lim = OutboundLimiter(
        OutboundConfig(max_requests=1, window_seconds=60), clock=_Clock()
    )
    provider = _StubOnlineFetch()
    catalog = _catalog(provider, policy=_StubPolicy(True), limiter=lim)

    first = str(catalog.execute("web.fetch", {"url": "https://ok.example/a"}))
    assert "正文#1" in first
    second = str(catalog.execute("web.fetch", {"url": "https://ok.example/b"}))
    # 被限流：返回可读文本，不改抛异常；且 provider 没有被真的调用第二次
    assert "[限流]" in second and "出站总速率" in second
    assert len(provider.calls) == 1


def test_URL策略先于出站闸门_被拒URL不消耗配额() -> None:
    """策略拒绝不等于"发出去了"——不该吃掉配额。顺序错了会让内网 URL 刷爆配额。"""
    lim = OutboundLimiter(
        OutboundConfig(max_requests=1, window_seconds=60), clock=_Clock()
    )
    policy = _StubPolicy(allow=False)
    provider = _StubOnlineFetch()
    catalog = _catalog(provider, policy=policy, limiter=lim)

    for _ in range(3):  # 被策略拒 3 次
        out = str(catalog.execute("web.fetch", {"url": "http://169.254.169.254/"}))
        assert "[拒绝]" in out
    assert provider.calls == []  # 一次都没发

    policy.allow = True  # 换成合法 URL：配额应当还是满的
    out = str(catalog.execute("web.fetch", {"url": "https://ok.example/"}))
    assert "正文#1" in out


# ---------- 环境变量 ----------


@pytest.mark.parametrize(
    "spec,expected",
    [("300/30", (300, 30)), ("0", None), ("off", None), ("", None)],
)
def test_解析出站速率(spec: str, expected: tuple[int, int] | None) -> None:
    assert parse_outbound_limit(spec) == expected


def test_解析出站速率格式错误要报错() -> None:
    """配置写错要在启动时暴露，而不是悄悄按默认值跑。"""
    with pytest.raises(ValueError):
        parse_outbound_limit("abc")


def test_按环境变量装配() -> None:
    lim = outbound_from_env(
        {
            "WARDEN_OUTBOUND_LIMIT": "300/30",
            "WARDEN_OUTBOUND_HOST_LIMIT": "5/10",
            "WARDEN_OUTBOUND_MAX_CONCURRENCY": "3",
            "WARDEN_OUTBOUND_DAILY_QUOTA": "1000",
        }
    )
    assert lim.config.max_requests == 300 and lim.config.window_seconds == 30
    assert lim.config.host_max_requests == 5 and lim.config.host_window_seconds == 10
    assert lim.config.max_concurrency == 3
    assert lim.config.daily_quota == 1000


def test_默认开启且参数保守() -> None:
    """默认不是"不限"——单次有界、总量无界这口子没有理由默认敞着。"""
    cfg = outbound_from_env({}).config
    assert cfg.max_requests > 0 and cfg.host_max_requests > 0
    assert cfg.max_concurrency > 0
    assert cfg.daily_quota == 0  # 硬性停机上限交给运维显式决定


def test_环境变量写错抛错() -> None:
    with pytest.raises(ValueError):
        outbound_from_env({"WARDEN_OUTBOUND_MAX_CONCURRENCY": "many"})
