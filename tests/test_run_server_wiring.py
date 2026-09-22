"""启动装配的**纯函数**部分：从环境变量到"到底起了什么"。

为什么单独测这些：`run_server.py` 整体覆盖率一直偏低（41%）——因为 `main()` 会真的起服务器，
没法直接测。但它的**判断逻辑**（要不要开某个能力、监听是否安全、路径取哪个）全是纯函数，
而且**本项目两次"未声明依赖/配置读错"都发生在这条路径上**。把它们单独测掉，
既补覆盖，也守住"启动口径"。

（真正起服务的那条路径靠容器冒烟 + 端到端演示验证，见 `scripts/container_smoke.sh`。）
"""

from __future__ import annotations

import pytest

from warden_agent.tool.catalog import ToolCatalog
from warden_agent.web.run_server import (
    AuthConfigError,
    _build_catalog,
    _build_policy,
    _cognition_from_env,
    _db_path,
    _event_keep_from_env,
    _knowledge_from_env,
    _shared_state_from_env,
    _stability_from_env,
    ensure_listen_is_safe,
    is_loopback_host,
)

# ---------- 监听地址与鉴权的组合（fail-closed 的关键一处）----------


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.5"])
def test_回环地址识别(host: str) -> None:
    assert is_loopback_host(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "10.0.0.1", "example.com", ""])
def test_非回环地址识别(host: str) -> None:
    assert is_loopback_host(host) is False


def test_对外监听且无鉴权_拒绝启动() -> None:
    """这是"裸奔服务"的最后一道闸：对外监听 + 无鉴权 = 直接拒绝启动。"""
    with pytest.raises(AuthConfigError):
        ensure_listen_is_safe("0.0.0.0", "anon-dev")
    # 开了鉴权就允许对外
    ensure_listen_is_safe("0.0.0.0", "bearer")
    # 回环 + 无鉴权是本地开发场景，允许
    ensure_listen_is_safe("127.0.0.1", "anon-dev")


# ---------- 各能力开关的解析口径 ----------


def test_共享状态开关() -> None:
    assert _shared_state_from_env({}) is False
    assert _shared_state_from_env({"WARDEN_SHARED_STATE": "1"}) is True
    assert _shared_state_from_env({"WARDEN_SHARED_STATE": "on"}) is True


def test_稳定性层默认开_且认on() -> None:
    """`WARDEN_STABILITY` 默认开（产品入口），统一走 env_bool 后也认 `on`。"""
    assert _stability_from_env({}) is True
    assert _stability_from_env({"WARDEN_STABILITY": "on"}) is True
    assert _stability_from_env({"WARDEN_STABILITY": "0"}) is False


def test_事件保留数_默认500_非正数回落默认() -> None:
    assert _event_keep_from_env({}) == 500
    assert _event_keep_from_env({"WARDEN_EVENT_KEEP": "50"}) == 50
    assert _event_keep_from_env({"WARDEN_EVENT_KEEP": "0"}) == 500      # <=0 → 默认
    assert _event_keep_from_env({"WARDEN_EVENT_KEEP": "-3"}) == 500


def test_知识库开关_支持开关与目录() -> None:
    """`WARDEN_KNOWLEDGE`：`1` = 内置离线语料；给路径 = 索引那个目录；`0`/空 = 关。"""
    assert _knowledge_from_env({}) is None
    assert _knowledge_from_env({"WARDEN_KNOWLEDGE": "0"}) is None
    assert _knowledge_from_env({"WARDEN_KNOWLEDGE": "off"}) is None
    assert _knowledge_from_env({"WARDEN_KNOWLEDGE": "1"}) is True
    assert _knowledge_from_env({"WARDEN_KNOWLEDGE": "on"}) is True
    assert _knowledge_from_env({"WARDEN_KNOWLEDGE": "./docs"}) == "./docs"


def test_认知开关_默认关_上下文上限默认值() -> None:
    planner, intent, limit = _cognition_from_env({}, ToolCatalog(), model=object())  # type: ignore[arg-type]
    assert planner is None            # 默认不开规划（会多花一次模型调用）
    assert intent is not None         # 意图路由是纯本地规则，默认开
    assert isinstance(limit, int) and limit > 0

    planner_on, _i, _l = _cognition_from_env(
        {"WARDEN_PLANNER": "1", "WARDEN_MAX_CONTEXT_CHARS": "1234"},
        ToolCatalog(), model=object(),  # type: ignore[arg-type]
    )
    assert planner_on is not None
    assert _cognition_from_env(
        {"WARDEN_MAX_CONTEXT_CHARS": "1234"}, ToolCatalog(), model=object(),  # type: ignore[arg-type]
    )[2] == 1234


def test_写错上下文上限_报错而不是静默用默认() -> None:
    """`env_int` 的口径：写错就点名报错（此前是 `isdigit()` 不成立就悄悄用默认值）。"""
    with pytest.raises(ValueError, match="WARDEN_MAX_CONTEXT_CHARS"):
        _cognition_from_env(
            {"WARDEN_MAX_CONTEXT_CHARS": "很多"}, ToolCatalog(), model=object()  # type: ignore[arg-type]
        )


def test_数据库路径默认值() -> None:
    assert _db_path().endswith(".db")


# ---------- 演示目录与策略（装配出来的东西要能用）----------


def test_演示目录里有天气与删除工具() -> None:
    names = {t.name for t in _build_catalog().all()}
    assert {"weather.get", "fs.delete"} <= names


def test_演示策略把删除列为需要审批() -> None:
    """`fs.delete` 必须落在 ASK 上——它是演示"人工闸门真的会拦"的那个高危工具。"""
    from warden_agent.policy.policy import Decision

    policy = _build_policy()
    assert policy.evaluate("fs.delete", {"path": "x"}).decision == Decision.ASK
    assert policy.evaluate("weather.get", {"city": "上海"}).decision == Decision.ALLOW

# ---------- 存储选择（多副本的前提：能真的选到 PostgreSQL）----------


def test_默认用SQLite(tmp_path, monkeypatch) -> None:
    from warden_agent.store.sqlite import SqliteStore
    from warden_agent.web.run_server import _store_from_env

    monkeypatch.setenv("WARDEN_DB_PATH", str(tmp_path / "t.db"))
    store = _store_from_env({})
    assert isinstance(store, SqliteStore)


def test_配了PG主机时连不上_明确报错且不退回SQLite() -> None:
    """**刻意不回落**：悄悄退回 SQLite 会让人"以为多副本在共享、其实各一个库"。"""
    from warden_agent.web.run_server import _store_from_env

    # 指向一个必然连不上的地址（端口 1）
    with pytest.raises(RuntimeError) as exc:
        _store_from_env({"WARDEN_PG_HOST": "127.0.0.1", "WARDEN_PG_PORT": "1"})
    assert "PostgreSQL" in str(exc.value)
    assert "不退回 SQLite" in str(exc.value)
