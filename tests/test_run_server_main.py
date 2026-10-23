"""`run_server.main()` 启动装配路径：用假 uvicorn 真正跑一遍 main。

`main()` 会起真实服务器，所以此前几乎没被测到（覆盖 ~50%）。这里把 `uvicorn.run`
换成记录器、把 `build_app` 换成捕获器，让 main 完整走一遍（存储 / 鉴权 / 限流 /
维护清扫 / 入站上限的装配），断言"配了什么就装配了什么"。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

import warden_agent.runtime.maintenance as maintenance_mod
from warden_agent.memory.store import SqliteMemoryStore
from warden_agent.store.sqlite import SqliteStore
from warden_agent.web import run_server

ROOT = Path(__file__).resolve().parents[1]


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


def test_main_模板里的占位符_真拒绝启动(tmp_path: Path, monkeypatch) -> None:
    """端到端（真跑 `main()`）：把 `deploy/k8s/configmap.yaml` 里的**原值**放进环境，
    进程必须 exit 2——而不是「启动成功、日志里说信任已开」。

    这是第五十三批整批结论的**进程级**证据：占位符不只被 `validate_env` 拦下，
    它真的会让服务起不来（`web/run_server.py:556-560` 的 `logger.error("配置错误：%s")`
    + `raise SystemExit(2)`）⇒ 照着 `deploy/k8s/` 的模板值部署，pod 会 CrashLoopBackOff，
    **不会**静默服务、也不会“看起来是绿的”。

    测试与模板同源：那个字符串就是 `deploy/k8s/configmap.yaml` 里的当前值
    （`tests/test_k8s_manifests.py::test_模板里的占位符必须过不了启动校验` 盯着它）。
    """
    _patch_common(monkeypatch, tmp_path)
    monkeypatch.setenv("WARDEN_HOST", "127.0.0.1")
    monkeypatch.setenv("WARDEN_TRUSTED_PROXIES", "REPLACE_ME-pod-cidr")

    with pytest.raises(SystemExit) as e:
        run_server.main()
    assert e.value.code == 2


def test_main_拼错的变量会告警_且告警指的路真能走通(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """拼错的 `WARDEN_*` 要告警，而且告警里点的"清单在哪"必须真的存在。

    这条告警只在"操作者手上正拿着一个写错的变量名"时出现，它唯一的作用就是
    把人送到清单那儿。原句点的是"文档 15"，而仓库里没有任何编号为 15 的文档
    （README 的章节是标题式的）——照它去找的人会一无所获，**比不告警还坏**。
    所以这里不只断言"告警发出去了"，还把 `「」` 里的落点拿回 README 对照：
    以后谁改了那节标题，这条测试会红，告警不会静静烂掉。
    """
    captured, created = _patch_common(monkeypatch, tmp_path)
    monkeypatch.setenv("WARDEN_RATELIMIT", "10/60")  # 登记表里没有这个名字

    with caplog.at_level(logging.WARNING, logger="run_server"):
        run_server.main()

    hits = [
        record.getMessage()
        for record in caplog.records
        if "WARDEN_RATELIMIT" in record.getMessage()
    ]
    assert hits, "拼错的 WARDEN_* 没告警——它会静默按默认值跑"
    msg = hits[0]
    assert "未被识别" in msg

    pointers = re.findall(r"「([^」]+)」", msg)
    assert pointers, f"告警没有给出可查的清单位置：{msg}"
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for pointer in pointers:
        assert pointer in readme, f"告警指向 README「{pointer}」，但 README 里没这个标题"
    assert "ENV_SPECS" in msg
    settings = (ROOT / "src" / "warden_agent" / "core" / "settings.py").read_text(
        encoding="utf-8"
    )
    assert "ENV_SPECS" in settings, "告警点了 ENV_SPECS，但 settings.py 里没有它"

    created["store"].close()
    created["memory_repository"].close()


# ---------------------------------------------------------------------------
# 单机存储 × 多副本开关：文档禁止的组合必须起不来
# ---------------------------------------------------------------------------


def test_main_sqlite配多副本开关_拒绝启动(tmp_path: Path, monkeypatch) -> None:
    """真入口也拦得住——单测绿而入口没接线，是最常见的"守卫不接线"缺口。"""
    _patch_common(monkeypatch, tmp_path)
    monkeypatch.setenv("WARDEN_SHARED_STATE", "1")

    with pytest.raises(SystemExit) as e:
        run_server.main()
    assert e.value.code == 2


def test_main_单副本时sqlite照常启动(tmp_path: Path, monkeypatch) -> None:
    """反向对照：不许因为新守卫把最常用的部署方式挡在门外。"""
    captured, created = _patch_common(monkeypatch, tmp_path)
    monkeypatch.delenv("WARDEN_SHARED_STATE", raising=False)

    run_server.main()

    assert "app" in captured
    created["store"].close()
    created["memory_repository"].close()


# ---------------------------------------------------------------------------
# 自建端点走环境变量启动：这条路上模型也要关系统代理
# ---------------------------------------------------------------------------
# 「自建接入」有三个构造入口（`create_model('custom')` / 界面切换 / 本文件这条启动路），
# 漏掉任何一个都让"对话不出你的网络"在那条路上变成假的。这条测的是**启动装配**
# 真的把 `trust_env=False` 传下去了——回环端点 + 坏代理，能探通才算过。

def test_main_自建端点启动时不走系统代理(tmp_path: Path, monkeypatch) -> None:
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from warden_agent.model.deepseek import ModelCallError

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"object": "list", "data": []}).encode())

        def log_message(self, *args: object) -> None:
            return

    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{srv.server_address[1]}/v1"
    try:
        captured, created = _patch_common(monkeypatch, tmp_path)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.setenv("WARDEN_BASE_URL", base_url)
        monkeypatch.setenv("WARDEN_MODEL_API_KEY", "not-needed")
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
        monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")

        run_server.main()

        # 启动装配的结果就是 build_app 收到的 model：不是另建一个来冒充。
        from warden_agent.model.deepseek import OpenAiCompatibleModel

        model = created["model"]
        assert isinstance(model, OpenAiCompatibleModel), (
            f"设了自建端点却没构造自建模型，实际是 {type(model).__name__}"
        )
        try:
            model.probe()
        except ModelCallError as exc:
            raise AssertionError(
                f"启动路径构造的自建模型走了系统代理，回环端点被转发到公网：{exc}"
            ) from exc
        created["store"].close()
        created["memory_repository"].close()
    finally:
        srv.shutdown()
        srv.server_close()
