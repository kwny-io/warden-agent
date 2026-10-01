"""限流测试：进程内固定窗口限流器 + 网关 429 行为。

限流保护的是"服务还活着"（可用性），与审批闸门保护的"Agent 能干什么"互补。
这里覆盖：窗口计数、窗口滚动、按调用者/来源分桶、公开路径豁免、
以及 429 的 problem+json 与 Retry-After 头。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import httpx
import pytest
from tests.conftest import ScriptedModel, weather_tool

from warden_agent.model.model import ChatResponse
from warden_agent.policy.policy import PolicyEngine
from warden_agent.store.sqlite import SqliteStore
from warden_agent.web.auth import TrustedCaller
from warden_agent.web.ratelimit import (
    RateLimitConfig,
    RateLimiter,
    client_key,
    limiter_from_env,
    parse_rate_limit,
)
from warden_agent.web.server import build_app

# ---------- 解析与配置 ----------


def test_解析限流配置() -> None:
    lim = parse_rate_limit("600/60")
    assert lim is not None
    assert lim.config.max_requests == 600
    assert lim.config.window_seconds == 60


@pytest.mark.parametrize("off", ["", "0", "off", "false", "no"])
def test_关闭限流(off: str) -> None:
    assert parse_rate_limit(off) is None


@pytest.mark.parametrize("bad", ["600", "abc/60", "600/x", "/60"])
def test_格式错误抛错(bad: str) -> None:
    # 要断言**文案**，不只断言"抛了 ValueError"：
    #   1) 报错文案被改成 None / 加料前缀时，只有这条能拦；
    #   2) 校验条件里 `or` 被改成 `and` 时，校验会被跳过，最后由 `int("abc")`
    #      抛出一个“invalid literal for int()”的、看不出所以然的 ValueError——
    #      只断言异常类型依然会绿。
    with pytest.raises(ValueError, match="次数/窗口秒数"):
        parse_rate_limit(bad)


def test_不设环境变量走默认() -> None:
    lim = limiter_from_env({})
    assert lim is not None
    assert lim.config.max_requests == 600


def test_环境变量可关闭() -> None:
    assert limiter_from_env({"WARDEN_RATE_LIMIT": "0"}) is None


def test_键按调用者优先_其次IP() -> None:
    assert client_key("alice", "1.2.3.4") == "caller:alice"
    assert client_key(None, "1.2.3.4") == "ip:1.2.3.4"
    assert client_key(None, None) == "anonymous"


# ---------- 窗口行为 ----------


def test_窗口内超限被拒() -> None:
    now = [0.0]
    lim = RateLimiter(3, 10, clock=lambda: now[0])
    assert [lim.allow("k") for _ in range(5)] == [True, True, True, False, False]
    # 拒绝不消耗配额：仍在窗口内，继续拒
    assert lim.allow("k") is False


def test_窗口滚动后恢复() -> None:
    now = [0.0]
    lim = RateLimiter(2, 10, clock=lambda: now[0])
    assert lim.allow("k") and lim.allow("k")
    assert lim.allow("k") is False
    now[0] = 10.0  # 进入新窗口
    assert lim.allow("k") is True


def test_不同键互不影响() -> None:
    lim = RateLimiter(1, 10, clock=lambda: 0.0)
    assert lim.allow("a")
    assert lim.allow("b")  # b 有自己的桶
    assert lim.allow("a") is False


def test_retry_after为正() -> None:
    now = [0.0]
    lim = RateLimiter(1, 10, clock=lambda: now[0])
    lim.allow("k")
    lim.allow("k")
    assert lim.retry_after("k") >= 1


def test_非法参数报错() -> None:
    with pytest.raises(ValueError):
        RateLimiter(0, 10)
    with pytest.raises(ValueError):
        RateLimiter(10, 0)


# ---------- 断言缺口：变异测试照出来的（2026-09-30）----------
#
# 这一片（`src/warden_agent/web/ratelimit.py`）第一次跑变异测试的结果是 **21 个突变体全部存活**。
# 关键前提：那些行**都被执行到了**（mutmut 报 no_tests=0）——不是"没覆盖"，是"跑了但几乎不检查结果"。
# 当时被放过去的例子：把"窗口剩余时间"的减法改成加法、把放行时的等待秒数 0 改成 1、
# 把参数下限从 0 改成 1、把 `store=store` 改成 `store=None`（单副本下行为一模一样，
# 只有"多副本时计数落到共享存储"会静默失效）。
# 下面每条注释都写明"改坏了会怎样"，便于以后有人想删掉某条时知道它在守什么。


class _RecordingStore:
    """记账用的假 store：证明"传进来的 store 真的被用了"，同时免去真实时钟的不确定。"""

    def __init__(self, *, count: int = 1, start: float = 0.0) -> None:
        self.calls: list[tuple[str, int, float]] = []
        self._count = count
        self._start = start

    def hit(self, bucket_key: str, window_seconds: int, now: float) -> tuple[int, float]:
        self.calls.append((bucket_key, window_seconds, now))
        return self._count, self._start


def test_放行时的等待秒数必须是零() -> None:
    """`return True, 0` 被改成 `return True, 1`：只断言 `allow()`（它取第一位）就漏了第二位。"""
    lim = RateLimiter(2, 10, clock=lambda: 0.0)
    assert lim.check("k") == (True, 0)


def test_拒绝时的等待秒数按剩余窗口向上取整() -> None:
    """等待秒数会进 `Retry-After` 给客户端，算错两头都疼：
    偏大把好客户端钉在外面；偏小让客户端立刻重试，限流形同虚设。

    这条同时照出三种写坏方式（都曾在存活名单里）：
      `window + 已过去时间`  → 报 12 而不是 8
      `now + 窗口起点`      → 算出负数，被 `max(1, …)` 兜成 1——**看起来完全"合理"**，最难手工发现
      `+0.999` 改成 `-0.999` / `+1.999` → 报 7 / 9
    所以窗口起点必须**不是 0**（不然前两种改写退化成同一个值，测不出来）。
    """
    now = [0.0]
    lim = RateLimiter(1, 10, clock=lambda: now[0])
    assert lim.allow("k") is True
    now[0] = 10.0  # 窗口滚动：新窗口起点是 10.0
    assert lim.allow("k") is True
    now[0] = 12.0  # 窗口内第 2 次 → 拒；剩余 10 - (12 - 10) = 8
    allowed, wait = lim.check("k")
    assert allowed is False
    assert wait == 8


def test_等待秒数最少是一秒() -> None:
    """剩余 0.5 秒时应当报 1（向上取整且不为 0）；把 `max(1, …)` 的下限改成 2 会报 2。"""
    now = [0.0]
    lim = RateLimiter(1, 10, clock=lambda: now[0])
    lim.allow("k")
    now[0] = 9.5
    allowed, wait = lim.check("k")
    assert allowed is False
    assert wait == 1


def test_窗口与次数为一也是合法配置() -> None:
    """`max_requests <= 0 or window_seconds <= 0` 被改成 `<= 1` 时，
    只有"刚好用 1"的调用会暴露——现有用例都用 10/60，所以一直没拦住。"""
    lim = RateLimiter(1, 1, clock=lambda: 0.0)
    assert lim.config == RateLimitConfig(1, 1)
    assert lim.allow("k") is True
    assert lim.allow("k") is False


def test_非法参数的报错文案要说明原因() -> None:
    """报错文案被改成 None 时，日志里只剩一个空异常：运维看到 `ValueError`
    而不知道是哪个参数错了（这类"日志里没有关键字"的退化只能靠文案断言拦）。"""
    with pytest.raises(ValueError, match="正整数"):
        RateLimiter(0, 10)
    with pytest.raises(ValueError, match="正整数"):
        RateLimiter(10, 0)


def test_传入的store会被真正使用() -> None:
    """`store=store` 被改成 `store=None`：单副本下行为完全一样（默认就造一个进程内 store），
    于是没有任何测试会发现——**而多副本时计数落到共享存储这件事会静默失效**（每副本各限一份，
    总限额翻倍）。这是"参数被丢掉"类缺陷里最隐蔽的一种，必须直接断言"store 被调用了"。"""
    store = _RecordingStore()
    lim = parse_rate_limit("5/30", store=store)
    assert lim is not None
    assert lim.allow("k") is True
    assert [call[:2] for call in store.calls] == [("k", 30)]


def test_默认配置是600次每分钟且用传入的store() -> None:
    """默认值写错（600 改成别的、60 改成 61）在生产里看不见——没有任何调用方会显式传值。"""
    store = _RecordingStore()
    lim = limiter_from_env({}, store=store)
    assert lim is not None
    assert (lim.config.max_requests, lim.config.window_seconds) == (600, 60)
    lim.allow("k")
    assert len(store.calls) == 1


def test_环境变量生效且用传入的store() -> None:
    """既验环境变量真被解析（而不是被当成"没设"→ None），也验 store 透传到底。"""
    store = _RecordingStore()
    lim = limiter_from_env({"WARDEN_RATE_LIMIT": "7/30"}, store=store)
    assert lim is not None
    assert (lim.config.max_requests, lim.config.window_seconds) == (7, 30)
    lim.allow("k")
    assert len(store.calls) == 1


# ---------- 网关行为 ----------


def _app(**kw):
    return build_app(
        model=ScriptedModel([ChatResponse(content="好", finish_reason="stop")]),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=SqliteStore(Path(tempfile.mkdtemp()) / "t.db"),
        **kw,
    )


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


@pytest.mark.asyncio
async def test_超限返回429与problem_json() -> None:
    app = _app(rate_limiter=RateLimiter(2, 60))
    async with _client(app) as c:
        assert (await c.get("/status/a")).status_code == 200
        assert (await c.get("/status/b")).status_code == 200
        r = await c.get("/status/c")
        assert r.status_code == 429
        assert r.headers["content-type"].startswith("application/problem+json")
        assert r.json()["errorCode"] == "RATE_LIMITED"
        assert int(r.headers["Retry-After"]) >= 1


@pytest.mark.asyncio
async def test_健康探针不被限流() -> None:
    """负载均衡探活必须始终可达，否则限流会把服务判死。"""
    app = _app(rate_limiter=RateLimiter(1, 60))
    async with _client(app) as c:
        for _ in range(5):
            assert (await c.get("/health/live")).status_code == 200
            assert (await c.get("/health/ready")).status_code == 200


@pytest.mark.asyncio
async def test_按调用者分桶() -> None:
    app = _app(
        rate_limiter=RateLimiter(1, 60),
        api_keys={
            "k-alice": TrustedCaller("t", "user", "alice"),
            "k-bob": TrustedCaller("t", "user", "bob"),
        },
    )
    async with _client(app) as c:
        a_auth = {"Authorization": "Bearer k-alice"}
        b_auth = {"Authorization": "Bearer k-bob"}
        # alice 用掉自己的配额
        assert (await c.get("/status/a", headers=a_auth)).status_code == 200
        assert (await c.get("/status/b", headers=a_auth)).status_code == 429
        # bob 不受影响
        assert (await c.get("/status/c", headers=b_auth)).status_code == 200


@pytest.mark.asyncio
async def test_未配置限流时不限制() -> None:
    app = _app()  # rate_limiter=None
    async with _client(app) as c:
        for i in range(20):
            assert (await c.get(f"/status/r{i}")).status_code == 200


@pytest.mark.asyncio
async def test_限流拒绝计入指标() -> None:
    from warden_agent.core.metrics import metrics

    app = _app(rate_limiter=RateLimiter(1, 60))
    async with _client(app) as c:
        await c.get("/status/a")
        assert (await c.get("/status/b")).status_code == 429
    # 直接读指标注册表（再发 HTTP 请求自己也会被限流，反而测不到）
    assert "warden_rate_limited_total" in metrics().render()
