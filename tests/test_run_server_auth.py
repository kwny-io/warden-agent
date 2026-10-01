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
    # 单 key 模式的默认身份是 demo-user（与控制台默认账号一致）
    assert keys["k-1"].user_id == "demo-user"


def test_单key模式身份可由环境变量指定() -> None:
    keys, mode = resolve_auth({"WARDEN_API_KEY": "k-1", "WARDEN_API_USER": "alice"})
    assert mode == "bearer"
    assert keys is not None
    assert keys["k-1"].user_id == "alice"


def test_多用户多密钥_每个key绑定独立身份() -> None:
    keys, mode = resolve_auth({"WARDEN_API_KEYS": "alice:k-aaa,bob:k-bbb"})
    assert mode == "bearer"
    assert keys is not None
    assert keys["k-aaa"].user_id == "alice"
    assert keys["k-bbb"].user_id == "bob"
    # 默认同租户，可用 WARDEN_TENANT 覆盖
    assert keys["k-aaa"].tenant_id == "local"


def test_多用户模式可指定租户() -> None:
    keys, _ = resolve_auth({"WARDEN_API_KEYS": "alice:k-aaa", "WARDEN_TENANT": "acme"})
    assert keys is not None
    assert keys["k-aaa"].tenant_id == "acme"


@pytest.mark.parametrize("bad", ["alice", "alice:", ":k-aaa", "alice:k-1,bob"])
def test_多用户格式错误时拒绝启动(bad: str) -> None:
    """`用户id:密钥` 写错就 fail-closed，而不是悄悄放行一个畸形配置。"""
    with pytest.raises(AuthConfigError):
        resolve_auth({"WARDEN_API_KEYS": bad})


def test_同一个用户两个key都有效用于零停机轮换() -> None:
    """轮换契约：新旧 key 同时在配，两份都能用、都指向同一个身份。

    这是“把密钥换成新的”不把在飞客户端打成 401 的前提：先发新 key → 确认能用 →
    再删旧 key。如果不支持两把 key，换 key 就只能“停服改配置重启”，
    或者硬打一个 401 窗口。
    """
    keys, mode = resolve_auth({"WARDEN_API_KEYS": "alice:k-old,alice:k-new"})
    assert mode == "bearer"
    assert keys is not None
    assert keys["k-old"].user_id == "alice"
    assert keys["k-new"].user_id == "alice"
    # 身份完全一致（同一租户/角色/product，换 key 不改归属）
    assert keys["k-old"] == keys["k-new"]
    # 旧 key 删掉后只剩新的
    keys2, _ = resolve_auth({"WARDEN_API_KEYS": "alice:k-new"})
    assert keys2 is not None and "k-old" not in keys2


def test_同一个密钥配给两个用户时拒绝启动() -> None:
    """静默覆盖是最危险的形态：alice 拿着自己的 key 却以 bob 的身份记账。

    会话归属、越权拦截、审计归属全都建立“身份由 key 决定”上，所以这里必须
    fail-closed，而不是最后写入者获胜。
    """
    with pytest.raises(AuthConfigError, match="同一个密钥被配给了两个用户"):
        resolve_auth({"WARDEN_API_KEYS": "alice:k-same,bob:k-same"})


def test_轮换写法不会被当成重复密钥冲突() -> None:
    """同一用户重复出现是轮换，不能误报成冲突（回归保护）。"""
    keys, _ = resolve_auth({"WARDEN_API_KEYS": "alice:k-1,alice:k-2,alice:k-3"})
    assert keys is not None and len(keys) == 3


def test_多用户key优先于单key() -> None:
    keys, mode = resolve_auth({"WARDEN_API_KEYS": "alice:k-a", "WARDEN_API_KEY": "k-single"})
    assert mode == "bearer"
    assert keys is not None
    assert "k-a" in keys and "k-single" not in keys


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
    """契约是"不抛错"，所以断言显式写出来：这种“放行”函数一旦被改成
    提前 return（没真的检查）也不会有人发现。"""
    assert ensure_listen_is_safe("0.0.0.0", "bearer") is None


def test_本机监听无鉴权时放行() -> None:
    assert ensure_listen_is_safe("127.0.0.1", "anon-dev") is None
