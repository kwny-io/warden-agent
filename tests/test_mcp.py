"""MCP 客户端测试：本地审查逻辑（纯单元）+ 真实 stdio 集成（环境具备才跑）。

**集成测试的跳过必须是"收集期可判定"的**（`_READY` 在导入时算一次），不能是
"跑起来连接失败再 pytest.skip"：

  - 后者会让"到底跑没跑"不确定——跳过数在不同环境、甚至不同次运行之间漂移；
  - 而"该跑的被静默跳过"和"通过了"在 CI 里看起来一样绿（同一个坑在 PG 集成测试上也踩过，
    为此 CI 里专门加了一条断言）。**判定为可跑之后，连接失败就该是失败**，不是跳过。

可真跑的条件 = node 可用 + ts 客户端依赖已装 + **本地已装的演示 server**
（`mcp-client.mjs` 在仓库里，但它依赖的 `node_modules` 是 gitignore 的，所以全新检出
默认跑不了——这正是原先"有时跳过"的原因）。

  - 第 50 批补一段：这三项都是**本地事实**，**不需要网络**。之前这条集成测试用
    `npx -y @modelcontextprotocol/server-everything` 起服务器，而 npx 冷启动会去 registry
    确认版本——断网时 3 条用例从"该跑也能跑"变成 setup error（假红）。
  - 第 51 批的计时器教训：当时把"一次 list 要 30.5s"归因成 npx 冷启动，于是把上限
    调到 90s；实际上那 30s 是 mjs 里**没被清除的计时器**（真实工作量 ~1s）。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Iterator

import pytest

from warden_agent.mcp import (
    McpClient,
    McpImportReport,
    McpImportReview,
    McpToolBinding,
    client_ready,
    local_everything_server,
)
from warden_agent.tool.catalog import ToolCatalog

# 本机能不能真连 MCP server：导入时判定一次，整个文件共用（跳过原因也因此是确定的）
_CLIENT_READY, _CLIENT_READY_REASON = client_ready()


def _binding(name: str, desc: str = "", props: dict | None = None) -> McpToolBinding:
    return McpToolBinding(name=name, description=desc,
                          input_schema={"type": "object",
                                        "properties": props or {"p": {"type": "string"}}})


# ---- 本地审查逻辑（不依赖 node）----
def test_审查_普通工具通过() -> None:
    review = McpImportReview()
    d = review.review(_binding("weather.get"))
    assert d.allowed


def test_审查_危险命名拒绝() -> None:
    review = McpImportReview()
    assert not review.review(_binding("run_shell")).allowed
    assert not review.review(_binding("fs.delete")).allowed


def test_审查_危险参数拒绝() -> None:
    review = McpImportReview()
    assert not review.review(_binding("deploy", props={"command": {"type": "string"}})).allowed


def test_审查_白名单放行() -> None:
    review = McpImportReview(allowed_names={"run_shell"})
    assert review.review(_binding("run_shell")).allowed


def test_审查_关闭危险拦截全放行() -> None:
    review = McpImportReview(block_dangerous=False)
    assert review.review(_binding("fs.delete")).allowed


# ---- 集成：连接真实 MCP stdio server（仅当 node + 本地 server 都在）----
# 用**本地已装的入口**，不用 `npx -y ...`：npx 每次冷启动都要去 registry 确认版本，
# **网络一断就挂到超时** —— 第 50 批那次全量跑就因为断网红了 3 条 setup error
# （而包早就作为 devDependency 装在 ts/mcp-client/node_modules 里）。
_SERVER = local_everything_server()
# 能不能真跑 = 两个**本地事实**（node + ts 客户端依赖、本地 server 入口），不含任何网络输入。
# 判定为可跑之后，连接失败就该是**失败**，不是跳过（与下一段"连接失败就该失败"的约定一致）。
_READY, _READY_REASON = _CLIENT_READY, _CLIENT_READY_REASON
if _SERVER is None and _READY:
    _READY, _READY_REASON = False, (
        "本地 MCP 演示 server 未安装（ts/mcp-client/node_modules 下缺 "
        "server-everything/dist/index.js，在该目录执行 npm install）"
    )

requires_mcp_server = pytest.mark.skipif(
    not _READY, reason=f"环境不具备真实 MCP 集成条件：{_READY_REASON}"
)

# 预热超时（秒）：**这个值现在真的会传到 node 侧**（第 51 批修的接线：Python 之前从不给
# mjs 传 `--timeout`，而 mjs 里硬编码的 30000ms 才是真正生效的那个数，于是这里写 90
# 和写 30 没区别）。真实工作量约 1~2s（本机实测），所以 90s 是安全阀，
# 不是"宽松一点更保险"：这个值曾经写成 600，结果一次冷启动变慢就让
# **整套回归静默卡了 10 分钟以上**（日志停在 `tests/test_mcp.py .....` 后面，
# 看起来像死锁而不像"在等外部进程"）。
# 一个测试能拖住整套回归多久，必须是**有界且看得见**的——见下面 fixture 里的进度输出。
_WARMUP_TIMEOUT_S = 90


@pytest.fixture(scope="session")
def warm_client(request: pytest.FixtureRequest) -> Iterator[McpClient]:
    """会话级预热：真实服务器的冷启动只做一次，之后再复用同一个已热客户端。

    这样定时断言面对的是“已就绪”的服务器，而不是每次重新冷启 npx（原 flake 的根因）。
    注意：环境已由 `client_ready()` 在收集期判定过；**预热失败即真失败**，不静默跳过
    （与“连接失败就该失败”的约定一致）。环境不具备条件时由 `requires_mcp_server` 跳过，
    此时本 fixture 根本不会被建立。

    预热期间**主动往终端写一行进度**：这一段最长要等 `_WARMUP_TIMEOUT_S` 秒，
    没有任何输出的话，人在看日志时无法区分“在等外部进程”和“死锁了”。
    """
    reporter = request.config.pluginmanager.get_plugin("terminalreporter")
    write_line = getattr(reporter, "write_line", None)
    prev = os.environ.get("WARDEN_MCP_TIMEOUT_S")
    os.environ["WARDEN_MCP_TIMEOUT_S"] = str(_WARMUP_TIMEOUT_S)
    started = time.monotonic()
    if callable(write_line):
        write_line(f"[mcp] 预热真实 server（本地入口 + node 冷启动，上限 {_WARMUP_TIMEOUT_S}s）…")
    try:
        client = McpClient(_SERVER)
        tools = client.list_tools()  # 预热：把 npx/node 拉起来一次
        assert tools, "预热未发现任何工具（服务器异常）"
        if callable(write_line):
            write_line(f"[mcp] 预热完成：{len(tools)} 个工具，{time.monotonic() - started:.1f}s")
        yield client
    except Exception as exc:
        # 把耗时带进失败信息：这样"连不上"和"连上了但很慢"一眼可分
        raise AssertionError(
            f"MCP server 预热失败（{time.monotonic() - started:.1f}s / "
            f"上限 {_WARMUP_TIMEOUT_S}s）：{type(exc).__name__}: {exc}"
        ) from exc
    finally:
        if prev is None:
            os.environ.pop("WARDEN_MCP_TIMEOUT_S", None)
        else:
            os.environ["WARDEN_MCP_TIMEOUT_S"] = prev


@requires_mcp_server
def test_mcp_真实连接_列出工具(warm_client: McpClient) -> None:
    tools = warm_client.list_tools()    # 环境已判定可跑 → 连不上就是真失败，不该跳过
    assert len(tools) > 0
    assert any(t.name == "get-sum" for t in tools)


@requires_mcp_server
def test_mcp_真实连接_调用工具(warm_client: McpClient) -> None:
    result = warm_client.call("get-sum", {"a": 2, "b": 3})
    assert "5" in str(result)


@requires_mcp_server
def test_mcp_导入审查并注册危险工具被拦(warm_client: McpClient) -> None:
    """关键：先审查再导入——危险的远端工具（如含 delete）会被拦截，不进工具管线。"""
    report = warm_client.import_reviewed(ToolCatalog())
    assert isinstance(report, McpImportReport)
    assert report.discovered > 0
    # 应该没有任何危险名的工具被导入
    imported_names = [b.name for b in report.bindings]
    assert not any("delete" in n or "shell" in n for n in imported_names)
    # 有被拒绝的（比如 gzip-file-as-resource 或危险参数）或全部通过
    assert report.imported == len(imported_names)


# ---- 超时可配置 + 有界重试（不依赖 node）----


def test_mcp超时可配置并传给subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    from warden_agent.mcp import client as client_mod

    seen: dict[str, object] = {}

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(cmd, 0, stdout='{"tools": []}', stderr="")

    monkeypatch.setattr(client_mod.subprocess, "run", fake_run)
    monkeypatch.setenv("WARDEN_MCP_TIMEOUT_S", "321")
    assert McpClient("fake-server").list_tools() == []
    assert seen["timeout"] == 321


def test_mcp超时用默认120(monkeypatch: pytest.MonkeyPatch) -> None:
    from warden_agent.mcp import client as client_mod

    seen: dict[str, object] = {}

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(cmd, 0, stdout='{"tools": []}', stderr="")

    monkeypatch.setattr(client_mod.subprocess, "run", fake_run)
    monkeypatch.delenv("WARDEN_MCP_TIMEOUT_S", raising=False)
    assert McpClient("fake-server").list_tools() == []
    assert seen["timeout"] == client_mod._DEFAULT_TIMEOUT_S == 120


def test_mcp_list超时重试一次后成功(monkeypatch: pytest.MonkeyPatch) -> None:
    """只读的 list：第一次超时允许重试一次，第二次成功。"""
    from warden_agent.mcp import client as client_mod

    calls = {"n": 0}

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))
        tool = {"name": "get-sum", "description": "", "inputSchema": {}}
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"tools": [tool]}), stderr=""
        )

    monkeypatch.setattr(client_mod.subprocess, "run", fake_run)
    tools = McpClient("fake-server").list_tools()
    assert [t.name for t in tools] == ["get-sum"]
    assert calls["n"] == 2


def test_mcp_call超时不重试(monkeypatch: pytest.MonkeyPatch) -> None:
    """call 可能有副作用：超时**绝不重试**，只能执行一次。"""
    from warden_agent.mcp import client as client_mod

    calls = {"n": 0}

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))

    monkeypatch.setattr(client_mod.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="超时"):
        McpClient("fake-server").call("danger", {"x": 1})
    assert calls["n"] == 1


def test_mcp_list两次都超时报错不无限重试(monkeypatch: pytest.MonkeyPatch) -> None:
    from warden_agent.mcp import client as client_mod

    calls = {"n": 0}

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))

    monkeypatch.setattr(client_mod.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="超时"):
        McpClient("fake-server").list_tools()
    assert calls["n"] == 2  # 有界：最多两次


# ---- 可用性判定本身（"跳过要确定"这条约定的守卫）----


def test_可用性判定_缺node时报不可用(monkeypatch: pytest.MonkeyPatch) -> None:
    from warden_agent.mcp import client as client_mod

    monkeypatch.setattr(client_mod.shutil, "which", lambda _name: None)
    ok, why = client_mod.client_ready()
    assert ok is False and "node" in why


def test_可用性判定_缺客户端依赖时报不可用(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """这就是 CI 里的情况：`mcp-client.mjs` 在仓库里，但它依赖的 node_modules 是 gitignore 的。"""
    from warden_agent.mcp import client as client_mod

    monkeypatch.setattr(client_mod.shutil, "which", lambda _name: "/usr/bin/node")
    fake_cli = tmp_path / "mcp-client.mjs"
    fake_cli.write_text("// 占位", encoding="utf-8")       # 入口在，但依赖没装
    monkeypatch.setattr(client_mod, "_DEFAULT_CLI", str(fake_cli))
    ok, why = client_mod.client_ready()
    assert ok is False and "依赖未安装" in why


def test_可用性判定_依赖齐全时报可用(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from warden_agent.mcp import client as client_mod

    monkeypatch.setattr(client_mod.shutil, "which", lambda _name: "/usr/bin/node")
    fake_cli = tmp_path / "mcp-client.mjs"
    fake_cli.write_text("// 占位", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    monkeypatch.setattr(client_mod, "_DEFAULT_CLI", str(fake_cli))
    ok, why = client_mod.client_ready()
    assert ok is True and why == "ok"


# ---- 第 51 批：两处“改了但没生效”的守卫 ----
def _capture_node_cmd(monkeypatch: pytest.MonkeyPatch, payload: str) -> list[list[str]]:
    """拦住真正的 node 进程，只把**将要执行的命令行**记下来。"""
    from types import SimpleNamespace

    from warden_agent.mcp import client as client_mod

    seen: list[list[str]] = []

    def fake_run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        seen.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, payload, "")

    monkeypatch.setattr(
        client_mod,
        "subprocess",
        SimpleNamespace(
            run=fake_run,
            TimeoutExpired=subprocess.TimeoutExpired,
            CompletedProcess=subprocess.CompletedProcess,
        ),
    )
    return seen


def test_超时预算必须传给node客户端(monkeypatch: pytest.MonkeyPatch) -> None:
    """守卫：`WARDEN_MCP_TIMEOUT_S` 是**两个进程共用的一个预算**，只钉住 Python 侧等于没钉。

    第 50 批踩过：fixture 设了 90s、断言消息里也写“上限 90s”，而真正生效的是
    `ts/mcp-client/mcp-client.mjs` 里硬编码的 `timeoutMs: 30000` —— 调用方写的那个数
    从头到尾没人读过。变异验证：把 `cmd += ["--timeout", ...]` 删掉 → 本用例红。
    """
    from warden_agent.mcp.client import _DEFAULT_TIMEOUT_S

    monkeypatch.delenv("WARDEN_MCP_TIMEOUT_S", raising=False)
    seen = _capture_node_cmd(monkeypatch, '{"tools": []}')
    McpClient("node fake-server.mjs").list_tools()
    assert "--timeout" in seen[0], "命令行里没有 --timeout：node 侧会用它自己的默认值"
    assert seen[0][seen[0].index("--timeout") + 1] == str(_DEFAULT_TIMEOUT_S * 1000), (
        "传下去的单位或默认值不对（mjs 的 --timeout 是毫秒）"
    )

    monkeypatch.setenv("WARDEN_MCP_TIMEOUT_S", "90")
    seen2 = _capture_node_cmd(monkeypatch, '{"tools": []}')
    McpClient("node fake-server.mjs").list_tools()
    assert seen2[0][seen2[0].index("--timeout") + 1] == str(90 * 1000), (
        "调用方设的预算没有传到 node 侧"
    )


@requires_mcp_server
def test_成功调用不该白等满超时预算(monkeypatch: pytest.MonkeyPatch) -> None:
    """守卫：node 侧的计时器必须被 `clearTimeout` 掉。

    不清掉的话**成功也要白等满整个预算**：第 50 批实测 `node mcp-client.mjs --op list`
    真实工作量 ~1s 却耗时 **31s**（= 硬编码的 30s 计时器 + 1s），而当时把它误读成
    “npx 冷启动 30.5s”，于是把上限一路调到 90s —— 治的是症状。
    这里把预算改为 20s（而不是默认 120s），这样回归时本用例 **20s 内**就红，不拖累整套回归。
    阈值 15s：远大于真实工作量（本机 ~2s），远小于计时器（20s）。
    """
    assert _SERVER is not None
    monkeypatch.setenv("WARDEN_MCP_TIMEOUT_S", "20")
    started = time.monotonic()
    tools = McpClient(_SERVER).list_tools()
    elapsed = time.monotonic() - started
    assert tools, "本地 server 没列出任何工具"
    assert elapsed < 15, (
        f"一次 list 花了 {elapsed:.1f}s，远大于真实工作量：像是 node 侧的计时器没被清除"
    )
