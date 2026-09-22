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
