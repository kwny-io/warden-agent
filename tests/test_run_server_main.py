"""`run_server.main()` 启动装配路径：用假 uvicorn 真正跑一遍 main。

`main()` 会起真实服务器，所以此前几乎没被测到（覆盖 ~50%）。这里把 `uvicorn.run`
换成记录器、把 `build_app` 换成捕获器，让 main 完整走一遍（存储 / 鉴权 / 限流 /
维护清扫 / 入站上限的装配），断言"配了什么就装配了什么"。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import warden_agent.runtime.maintenance as maintenance_mod
from warden_agent.memory.store import SqliteMemoryStore
from warden_agent.store.sqlite import SqliteStore
from warden_agent.web import run_server


class _FakeSweeper:
    instances: list[_FakeSweeper] = []

    def __init__(self, store, *, interval_s, memory_repository=None,
                 idempotency_ttl_s=0.0, rate_limit_max_age_s=0.0) -> None:
        self.store = store
        self.interval = interval_s
        self.memory = memory_repository
        self.ttl = idempotency_ttl_s
        self.max_age = rate_limit_max_age_s
        self.started = False
        self.stopped = False
        _FakeSweeper.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self, timeout: float = 2.0) -> None:
        self.stopped = True


def _patch_common(monkeypatch, tmp_path: Path):
    captured: dict = {}
    created: dict = {}

    monkeypatch.setattr(run_server, "load_env", lambda: None)
    monkeypatch.setattr(run_server, "setup_logging", lambda: None)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("WARDEN_DB_PATH", str(tmp_path / "srv.db"))
    monkeypatch.setenv("WARDEN_ALLOW_ANON", "1")
    monkeypatch.setattr(maintenance_mod, "MaintenanceSweeper", _FakeSweeper)
    _FakeSweeper.instances.clear()

    def _fake_build_app(**kwargs):
        created.update(kwargs)
        return object()

    monkeypatch.setattr(run_server, "build_app", _fake_build_app)

    def _fake_uvicorn_run(app, **kwargs):
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(run_server.uvicorn, "run", _fake_uvicorn_run)
    return captured, created


def test_main装配默认_维护清扫与入站上限透传(tmp_path: Path, monkeypatch) -> None:
    captured, created = _patch_common(monkeypatch, tmp_path)
    monkeypatch.setenv("WARDEN_MAINTENANCE_INTERVAL_S", "42")
    monkeypatch.setenv("WARDEN_IDEMPOTENCY_TTL_S", "123")

    run_server.main()

    sweeper = _FakeSweeper.instances[0]
    assert sweeper.interval == 42                 # 环境变量 → 清扫间隔
    assert sweeper.ttl == 123.0                   # 环境变量 → 幂等 TTL
    assert sweeper.memory is not None             # 记忆库接线（落盘）
    assert sweeper.started is True                # 真正启动了后台线程

    # 维护清扫对象被透传进 build_app（应用停机时才能 stop 它）
    assert created["maintenance"] is sweeper
    assert isinstance(created["store"], SqliteStore)
    assert isinstance(created["memory_repository"], SqliteMemoryStore)
    assert created["max_request_bytes"] > 0
    assert captured["host"] == "127.0.0.1"
    assert captured["app"] is not None

    created["store"].close()
    created["memory_repository"].close()


def test_main_受信代理透传给uvicorn(tmp_path: Path, monkeypatch) -> None:
    """`WARDEN_TRUSTED_PROXIES` → uvicorn 的 `forwarded_allow_ips`。

    不配时**不能**出现这个 kwarg（哪怕写 None）：uvicorn 把 `forwarded_allow_ips=None`
    当成 "自己从 `FORWARDED_ALLOW_IPS` 环境变量/默认值里读"，显式传 None 会把它
    默认的「只信回环」盖掉。所以只能"设了才传"。
    """
    captured, created = _patch_common(monkeypatch, tmp_path)
    monkeypatch.setenv("WARDEN_TRUSTED_PROXIES", "10.0.0.0/8,172.17.0.1")

    run_server.main()

    assert captured["forwarded_allow_ips"] == "10.0.0.0/8,172.17.0.1"
    created["store"].close()
    created["memory_repository"].close()


def test_main_不配受信代理时不传该参数(tmp_path: Path, monkeypatch) -> None:
    captured, created = _patch_common(monkeypatch, tmp_path)
    monkeypatch.delenv("WARDEN_TRUSTED_PROXIES", raising=False)

    run_server.main()

    assert "forwarded_allow_ips" not in captured
    created["store"].close()
    created["memory_repository"].close()


def test_main_0间隔时维护清扫关闭(tmp_path: Path, monkeypatch) -> None:
    captured, created = _patch_common(monkeypatch, tmp_path)
    monkeypatch.setenv("WARDEN_MAINTENANCE_INTERVAL_S", "0")

    run_server.main()
    assert _FakeSweeper.instances[0].interval == 0
    created["store"].close()
    created["memory_repository"].close()


def test_main_bearer鉴权与审计开关(tmp_path: Path, monkeypatch) -> None:
    captured, created = _patch_common(monkeypatch, tmp_path)
    monkeypatch.setenv("WARDEN_API_KEY", "secret-key")
    monkeypatch.setenv("WARDEN_AUDIT", "1")

    run_server.main()

    assert created["api_keys"]          # 配了 key → 开启鉴权
    assert created["audit_store"] is not None
    assert created["model_id"] == "fake"
    created["store"].close()
    created["memory_repository"].close()
    audit_store = created["audit_store"]
    close = getattr(audit_store, "close", None)
    if callable(close):
        close()


def test_main_对外监听无鉴权_拒绝启动(tmp_path: Path, monkeypatch) -> None:
    """fail-closed：0.0.0.0 且无鉴权 → 退出码 2，绝不裸奔。"""
    _patch_common(monkeypatch, tmp_path)
    monkeypatch.setenv("WARDEN_HOST", "0.0.0.0")
    monkeypatch.delenv("WARDEN_API_KEY", raising=False)
    monkeypatch.delenv("WARDEN_ALLOW_ANON", raising=False)

    with pytest.raises(SystemExit) as e:
        run_server.main()
    assert e.value.code == 2


# ---------------------------------------------------------------------------
# 假模型不许当成真能力对外服务（待办：演示/假模型 fail-closed）
# ---------------------------------------------------------------------------

def test_假模型_回环地址永远放行() -> None:
    """判定看的是"外部能不能连进来"，不是"配置看起来多正式"。

    本机跑假模型（哪怕开了鉴权）不拦：没有任何远端能连进来，而为了
    "试一下鉴权"还得先声明是个演示会很烦。
    """
    for mode in ("anon-dev", "bearer"):
        assert run_server.ensure_model_is_real(
            is_fake=True, host="127.0.0.1", auth_mode=mode, env={},
        ) is None
    assert run_server.ensure_model_is_real(
        is_fake=True, host="::1", auth_mode="bearer", env={},
    ) is None


def test_假模型_对外监听必须要显式声明() -> None:
    """0.0.0.0 上的固定脚本回复会被当成"系统真能干活"的证据 → 必须显式声明。

    声明的意义在于它会在启动日志与 `/capabilities` 里变成 `demo_instance=true`：
    看见的人得到的是"这是演示"，而不是"它居然能自己选工具"。
    """
    with pytest.raises(run_server.DemoModelRefusedError) as e:
        run_server.ensure_model_is_real(
            is_fake=True, host="0.0.0.0", auth_mode="bearer", env={},
        )
    assert "WARDEN_ALLOW_FAKE_MODEL=1" in str(e.value)   # 报错里给出路，不是死胡同
    assert "WARDEN_DEMO_INSTANCE=1" in str(e.value)

    # 只声明一半不够：一个变量拦不住"生产配置里残留一句 ALLOW_FAKE_MODEL"这种事故，
    # 第二个变量要的是一个只可能故意写出口的词。
    with pytest.raises(run_server.DemoModelRefusedError):
        run_server.ensure_model_is_real(
            is_fake=True, host="0.0.0.0", auth_mode="bearer",
            env={"WARDEN_ALLOW_FAKE_MODEL": "1"},
        )

    assert run_server.ensure_model_is_real(
        is_fake=True, host="0.0.0.0", auth_mode="bearer",
        env={"WARDEN_ALLOW_FAKE_MODEL": "1", "WARDEN_DEMO_INSTANCE": "1"},
    ) is None


def test_真模型从不因地址被拦() -> None:
    """反向保护：这个门禁只针对假模型，别把正常部署拦了。"""
    assert run_server.ensure_model_is_real(
        is_fake=False, host="0.0.0.0", auth_mode="bearer", env={},
    ) is None


def test_main_对外监听跑假模型_未声明则拒绝启动(tmp_path: Path, monkeypatch) -> None:
    """端到端：容器里最可能出错的组合（0.0.0.0 + 有 key + 没糊模型凭据）。"""
    _patch_common(monkeypatch, tmp_path)
    monkeypatch.setenv("WARDEN_HOST", "0.0.0.0")
    monkeypatch.setenv("WARDEN_API_KEY", "secret-key")   # 过了"对外必须鉴权"那道门
    monkeypatch.delenv("WARDEN_ALLOW_FAKE_MODEL", raising=False)
    monkeypatch.delenv("WARDEN_DEMO_INSTANCE", raising=False)

    with pytest.raises(SystemExit) as e:
        run_server.main()
    assert e.value.code == 2


def test_main_对外监听跑演示实例_声明后放行(tmp_path: Path, monkeypatch) -> None:
    """显式声明是开的口子：声明了就允许，且模型确实是假模型（标记为演示）。"""
    captured, created = _patch_common(monkeypatch, tmp_path)
    monkeypatch.setenv("WARDEN_HOST", "0.0.0.0")
    monkeypatch.setenv("WARDEN_API_KEY", "secret-key")
    monkeypatch.setenv("WARDEN_ALLOW_FAKE_MODEL", "1")
    monkeypatch.setenv("WARDEN_DEMO_INSTANCE", "1")

    run_server.main()

    assert captured["host"] == "0.0.0.0"      # 真的对外监听了
    assert created["model_id"] == "fake"      # 而模型确实是演示
    created["store"].close()
    created["memory_repository"].close()


# ---------------------------------------------------------------------------
# SSE 空闲心跳的**取值来源**：文档把它写成可调旋钮，那就必须真的读一次
#
# 这一组盯的不是心跳机制（机制在 tests/test_web_stream_disconnect.py 里验），
# 而是"环境变量 → build_app → 搬运循环"这条链上装配层有没有把它丢掉。
# 丢掉的表现是：操作者设了 `WARDEN_SSE_HEARTBEAT_SECONDS=30`，启动日志不提，
# 心跳还是 10 秒——没有报错，只有代理把连接当死连接掐掉时多出来的一批断线。
# ---------------------------------------------------------------------------

def test_main_SSE心跳从环境变量透传(tmp_path: Path, monkeypatch) -> None:
    captured, created = _patch_common(monkeypatch, tmp_path)
    monkeypatch.setenv("WARDEN_SSE_HEARTBEAT_SECONDS", "30")

    run_server.main()

    assert created["sse_heartbeat_seconds"] == 30.0
    created["store"].close()
    created["memory_repository"].close()


def test_main_SSE心跳不设时用默认值(tmp_path: Path, monkeypatch) -> None:
    """不设时必须是 `web/server.py` 那个常量，而不是 0（0 是"关心跳"，反了）。"""
    captured, created = _patch_common(monkeypatch, tmp_path)
    monkeypatch.delenv("WARDEN_SSE_HEARTBEAT_SECONDS", raising=False)

    run_server.main()

    assert created["sse_heartbeat_seconds"] == run_server.SSE_HEARTBEAT_SECONDS
    assert created["sse_heartbeat_seconds"] > 0
    created["store"].close()
    created["memory_repository"].close()


def test_main_SSE心跳为0_透传0而不是被当成未设置(tmp_path: Path, monkeypatch) -> None:
    """`0` 是有意义的取值（关心跳），不能被"falsy 就跳过"的写法吞掉。"""
    captured, created = _patch_common(monkeypatch, tmp_path)
    monkeypatch.setenv("WARDEN_SSE_HEARTBEAT_SECONDS", "0")

    run_server.main()

    assert created["sse_heartbeat_seconds"] == 0.0
    created["store"].close()
    created["memory_repository"].close()


def test_main_SSE心跳负数拒绝启动(tmp_path: Path, monkeypatch) -> None:
    """负数在**启动期**就红（fail-closed），不是被当成"关心跳"悄悄放过去。

    这里验的是既有的 `kind="int"` 格式校验（`str.isdigit()`）对**这个变量**确实生效——
    所以 `run_server` 里不需要再写一个 `if < 0` 分支（那是永远跑不到的死代码，
    写了反而会让人以为负数防线是那儿立的）。
    """
    _patch_common(monkeypatch, tmp_path)
    monkeypatch.setenv("WARDEN_SSE_HEARTBEAT_SECONDS", "-5")

    with pytest.raises(SystemExit) as e:
        run_server.main()
    assert e.value.code == 2
