"""启动期鉴权决策测试（fail-closed）。

测的是 `run_server` 的**启动决策**，不是 HTTP 中间件（那是 test_web_phase13 的事）：

  - 有 key                      → bearer 模式
  - 无 key 且未显式允许匿名      → 抛 AuthConfigError（**拒绝启动**）
  - 无 key 但显式 WARDEN_ALLOW_ANON=1 → anon-dev
  - 对外监听 + 非 bearer         → 拒绝（Docker 里最容易踩的组合）

为什么这么严：`/approve`、`/reject` 是人工审批闸门的入口。接口无鉴权时，
调用方可以自己批准自己的高危操作 —— 那"审批"就成了摆设。
"""

from __future__ import annotations

import pytest

from warden_agent.web.run_server import (
    AuthConfigError,
    ensure_listen_is_safe,
    is_loopback_host,
    resolve_auth,
)


def test_有key时开启bearer鉴权() -> None:
    keys, mode = resolve_auth({"WARDEN_API_KEY": "k-1"})
    assert mode == "bearer"
    assert keys is not None
    assert "k-1" in keys
    assert keys["k-1"].tenant_id == "local"


def test_无key且未显式允许匿名时拒绝启动() -> None:
    with pytest.raises(AuthConfigError) as ei:
        resolve_auth({})
    msg = str(ei.value)
    assert "WARDEN_API_KEY" in msg
    assert "WARDEN_ALLOW_ANON" in msg


def test_空字符串的key不算配了key() -> None:
    """避免 .env 里留了个空值就悄悄裸奔 —— 空串等同没设，仍 fail-closed。"""
    with pytest.raises(AuthConfigError):
        resolve_auth({"WARDEN_API_KEY": ""})


@pytest.mark.parametrize("flag", ["1", "true", "yes", "TRUE", " 1 "])
def test_显式允许匿名时进入开发模式(flag: str) -> None:
    keys, mode = resolve_auth({"WARDEN_ALLOW_ANON": flag})
    assert mode == "anon-dev"
    assert keys is None


@pytest.mark.parametrize("flag", ["0", "false", "no", ""])
def test_非真值的匿名开关不算数(flag: str) -> None:
    with pytest.raises(AuthConfigError):
        resolve_auth({"WARDEN_ALLOW_ANON": flag})


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_回环地址识别(host: str) -> None:
    assert is_loopback_host(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "10.0.0.5"])
def test_非回环地址识别(host: str) -> None:
    assert is_loopback_host(host) is False


def test_对外监听且无鉴权时拒绝启动() -> None:
    """容器里的典型组合：WARDEN_HOST=0.0.0.0 但忘了设 key。"""
    with pytest.raises(AuthConfigError):
        ensure_listen_is_safe("0.0.0.0", "anon-dev")


def test_对外监听且有鉴权时放行() -> None:
    ensure_listen_is_safe("0.0.0.0", "bearer")


def test_本机监听无鉴权时放行() -> None:
    ensure_listen_is_safe("127.0.0.1", "anon-dev")
