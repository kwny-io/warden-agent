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
from warden_agent.web.ratelimit import RateLimiter, client_key, limiter_from_env, parse_rate_limit
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
    with pytest.raises(ValueError):
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
