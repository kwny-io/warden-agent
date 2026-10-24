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
    _audit_key_status,
    _build_catalog,
    _build_policy,
    _cognition_from_env,
    _db_path,
    _event_keep_from_env,
    _knowledge_from_env,
    _session_cache_max_from_env,
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


def test_审计密钥启动口径() -> None:
    """审计关闭/带密钥/无密钥/开关要求密钥四种口径。"""
    assert _audit_key_status({}, audit_enabled=False, has_key=False) == "off"
    assert _audit_key_status({}, audit_enabled=True, has_key=True) == "keyed"
    assert _audit_key_status({}, audit_enabled=True, has_key=False) == "unkeyed"
    # 开了 REQUIRE_KEY 又没密钥 → 拒绝启动（required-missing）；有密钥时开关不影响
    assert _audit_key_status(
        {"WARDEN_AUDIT_REQUIRE_KEY": "1"}, audit_enabled=True, has_key=False
    ) == "required-missing"
    assert _audit_key_status(
        {"WARDEN_AUDIT_REQUIRE_KEY": "1"}, audit_enabled=True, has_key=True
    ) == "keyed"


def test_会话缓存上限解析() -> None:
    assert _session_cache_max_from_env({}) == 1000
    assert _session_cache_max_from_env({"WARDEN_SESSION_CACHE_MAX": "5"}) == 5
    assert _session_cache_max_from_env({"WARDEN_SESSION_CACHE_MAX": "0"}) == 0  # 0 = 不限


def test_知识库开关_支持开关与目录() -> None:
    """`WARDEN_KNOWLEDGE`：`1` = 内置离线语料；给路径 = 索引那个目录；`0`/空 = 关。"""
    assert _knowledge_from_env({}) is None
    assert _knowledge_from_env({"WARDEN_KNOWLEDGE": "0"}) is None
    assert _knowledge_from_env({"WARDEN_KNOWLEDGE": "off"}) is None
    assert _knowledge_from_env({"WARDEN_KNOWLEDGE": "1"}) is True
    assert _knowledge_from_env({"WARDEN_KNOWLEDGE": "on"}) is True
    assert _knowledge_from_env({"WARDEN_KNOWLEDGE": "./docs"}) == "./docs"


def test_认知开关_默认值_上下文上限默认值() -> None:
    """第115 批把规划的默认值从"关"翻成"开"——翻转的理由是成本只落在复杂任务上。

    这条断言就是那个决定的可执行版本：默认必须挂上规划器；
    而"简单任务到底花不花调用"另由 tests/test_planner_default.py 数调用次数钉住，
    不靠这条注释自证。
    """
    planner, intent, limit = _cognition_from_env({}, ToolCatalog(), model=object())  # type: ignore[arg-type]
    assert planner is not None        # 默认开规划（只对复杂任务多花一次调用）
    assert intent is not None         # 意图路由是纯本地规则，默认开
    assert isinstance(limit, int) and limit > 0

    planner_off, _i2, _l2 = _cognition_from_env(
        {"WARDEN_PLANNER": "0", "WARDEN_MAX_CONTEXT_CHARS": "1234"},
        ToolCatalog(), model=object(),  # type: ignore[arg-type]
    )
    assert planner_off is None        # 显式关掉仍然有效
    assert _cognition_from_env(
        {"WARDEN_MAX_CONTEXT_CHARS": "1234"}, ToolCatalog(), model=object(),  # type: ignore[arg-type]
    )[2] == 1234
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


def test_演示目录默认不注册演示卡() -> None:
    """默认目录里**没有**演示卡：weather.get / fs.delete 都要显式 WARDEN_DEMO_TOOLS=1。

    `run_server` 是真被启动的入口，默认就把“假天气 + 假删除”塞进模型可见的工具列表，
    等于让调用方拿到假的成功文本 —— 这是本项目最不该有的那类“形同虚设”。
    """
    names = {t.name for t in _build_catalog().all()}
    assert "weather.get" not in names
    assert "fs.delete" not in names


def test_显示打开后才注册演示卡() -> None:
    names = {t.name for t in _build_catalog({"WARDEN_DEMO_TOOLS": "1"}).all()}
    assert {"weather.get", "fs.delete"} <= names


def test_演示卡不谎报做了没做的事(tmp_path) -> None:
    """副作用声明与实际行为必须一致：删除卡**不删文件**，而且返回值里要写清是演示。

    旧写法 `return f"已删除 {path}"` 是纯粹的空壳谎报：一句话回成功、磁盘毫无变化。
    """
    catalog = _build_catalog({"WARDEN_DEMO_TOOLS": "1"})
    victim = tmp_path / "keep-me.txt"
    victim.write_text("还在这儿", encoding="utf-8")

    from warden_agent.tool.catalog import FunctionToolSpec

    spec = catalog.get("fs.delete")
    assert isinstance(spec, FunctionToolSpec) and spec.function is not None
    out = str(spec.function(path=str(victim)))
    assert victim.exists(), "演示卡居然真删了文件（演示卡不做真删除）"
    assert "演示" in out, f"返回值不能谎称成功：{out!r}"
    assert "已删除" not in out

    assert "演示" in spec.description, "工具描述也要写明这是演示卡"
    assert spec.pure is False, "演示卡的副作用声明要保持保守（非 pure → 走审批）"


def test_演示卡的天气返回也标注演示() -> None:
    from warden_agent.tool.catalog import FunctionToolSpec

    catalog = _build_catalog({"WARDEN_DEMO_TOOLS": "1"})
    spec = catalog.get("weather.get")
    assert isinstance(spec, FunctionToolSpec) and spec.function is not None
    out = str(spec.function(city="上海"))
    assert "演示" in out, f"固定值天气必须自称演示，不能让调用方当成真实数据：{out!r}"


def test_演示策略把删除列为需要审批() -> None:
    """`fs.delete` 必须落在 ASK 上——它是演示"人工闸门真的会拦"的那个高危工具。"""
    from warden_agent.policy.policy import Decision

    policy = _build_policy(_build_catalog({"WARDEN_DEMO_TOOLS": "1"}))
    assert policy.evaluate("fs.delete", {"path": "x"}).decision == Decision.ASK
    assert policy.evaluate("weather.get", {"city": "上海"}).decision == Decision.ALLOW


def test_演示策略默认拦所有非pure工具() -> None:
    """产品路径的门禁口径：**没声明 pure=True 的一律 ASK**，不靠工具名黑名单。

    改前是 `ask_when_tool_in({"fs.delete"})`，于是 `shell.run`（能在沙箱里跑任意
    命令）、`git.apply_patch`（写仓库）、MCP 导入的工具全部直接放行；唯一被拦住的
    `fs.delete` 反而是没有真实副作用的演示空壳。
    """
    from warden_agent.policy.policy import Decision

    policy = _build_policy(_build_catalog())
    for name in ("shell.run", "git.apply_patch", "fs.delete", "mcp.anything"):
        assert policy.evaluate(name, {}).decision == Decision.ASK, name
    # 未注册的名字也要 ASK（fail-closed），不能因为"目录里没有"就放行
    assert policy.evaluate("完全不存在的工具", {}).decision == Decision.ASK


def test_演示策略覆盖后注册进目录的工具() -> None:
    """关键的一条：策略闭包读的是**实时目录**。

    `build_app` 会继续往同一个 catalog 里 augment（memory/web/skill/mcp/git），
    所以策略必须在**注册之后**也能拦得住 —— 否则"启动时没有的工具"就成了绕过口。
    """
    from warden_agent.policy.policy import Decision
    from warden_agent.tool.catalog import function_tool

    catalog = _build_catalog()
    policy = _build_policy(catalog)          # 先建策略

    @function_tool(
        "late.registered",
        "启动后才注册的破坏性工具",
        {"type": "object", "properties": {}},
        pure=False,
    )
    def late() -> str:
        return "done"

    catalog.register(late)                    # 后注册
    assert policy.evaluate("late.registered", {}).decision == Decision.ASK

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
