"""MCP 客户端测试：本地审查逻辑（纯单元）+ 真实 stdio 集成（环境具备才跑）。

**集成测试的跳过必须是"收集期可判定"的**（`_READY` 在导入时算一次），不能是
"跑起来连接失败再 pytest.skip"：

  - 后者会让"到底跑没跑"不确定——跳过数在不同环境、甚至不同次运行之间漂移；
  - 而"该跑的被静默跳过"和"通过了"在 CI 里看起来一样绿（同一个坑在 PG 集成测试上也踩过，
    为此 CI 里专门加了一条断言）。**判定为可跑之后，连接失败就该是失败**，不是跳过。

可真跑的条件 = node 可用 + ts 客户端依赖已装（`mcp-client.mjs` 在仓库里，但它依赖的
`node_modules` 是 gitignore 的，所以全新检出默认跑不了——这正是原先"有时跳过"的原因）。
"""
from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator

import pytest

from warden_agent.mcp import (
    McpClient,
    McpImportReport,
    McpImportReview,
    McpToolBinding,
    client_ready,
)
from warden_agent.tool.catalog import ToolCatalog

# 本机能不能真连 MCP server：导入时判定一次，整个文件共用（跳过原因也因此是确定的）
_READY, _READY_REASON = client_ready()

requires_mcp_server = pytest.mark.skipif(
    not _READY, reason=f"环境不具备真实 MCP 集成条件：{_READY_REASON}"
)


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


# ---- 集成：连接真实 MCP stdio server（仅当 node + 服务器可用）----
_SERVER = "npx -y @modelcontextprotocol/server-everything"
# 预热用的宽松超时：冷启动（npx 解析/下载 + node 启动）在负载下可能很慢，
# 预热只跑一次，跑通后 npx 缓存已热，后续操作就快。
_WARMUP_TIMEOUT_S = 600


@pytest.fixture(scope="session")
def warm_client() -> Iterator[McpClient]:
    """会话级预热：真实服务器的冷启动只做一次，之后再复用同一个已热客户端。

    这样定时断言面对的是“已就绪”的服务器，而不是每次重新冷启 npx（原 flake 的根因）。
    注意：环境已由 `client_ready()` 在收集期判定过；**预热失败即真失败**，不静默跳过
    （与“连接失败就该失败”的约定一致）。环境不具备条件时由 `requires_mcp_server` 跳过，
    此时本 fixture 根本不会被建立。
    """
    prev = os.environ.get("WARDEN_MCP_TIMEOUT_S")
    os.environ["WARDEN_MCP_TIMEOUT_S"] = str(_WARMUP_TIMEOUT_S)
    try:
        client = McpClient(_SERVER)
        tools = client.list_tools()  # 预热：把 npx/node 拉起来一次
        assert tools, "预热未发现任何工具（服务器异常）"
        yield client
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
