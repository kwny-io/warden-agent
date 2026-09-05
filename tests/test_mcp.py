"""MCP 客户端测试：本地审查逻辑（纯单元）+ 真实 stdio 集成（有 node 才跑）。"""
from __future__ import annotations

import pytest

from warden_agent.mcp import (
    McpClient,
    McpImportReport,
    McpImportReview,
    McpToolBinding,
    node_available,
)
from warden_agent.tool.catalog import ToolCatalog


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


@pytest.mark.skipif(not node_available(), reason="需要 node 才能连 MCP server")
def test_mcp_真实连接_列出工具() -> None:
    client = McpClient(_SERVER)
    try:
        tools = client.list_tools()
    except RuntimeError as e:
        pytest.skip(f"无法连接 MCP server: {e}")
    assert len(tools) > 0
    assert any(t.name == "get-sum" for t in tools)


@pytest.mark.skipif(not node_available(), reason="需要 node 才能连 MCP server")
def test_mcp_真实连接_调用工具() -> None:
    client = McpClient(_SERVER)
    try:
        client.list_tools()  # 先建立连接/发现（触发 skip-if-无法连接）
    except RuntimeError as e:
        pytest.skip(f"无法连接 MCP server: {e}")
    result = client.call("get-sum", {"a": 2, "b": 3})
    assert "5" in str(result)


@pytest.mark.skipif(not node_available(), reason="需要 node 才能连 MCP server")
def test_mcp_导入审查并注册危险工具被拦() -> None:
    """关键：先审查再导入——危险的远端工具（如含 delete）会被拦截，不进工具管线。"""
    client = McpClient(_SERVER)
    try:
        report = client.import_reviewed(ToolCatalog())
    except RuntimeError as e:
        pytest.skip(f"无法连接 MCP server: {e}")
    assert isinstance(report, McpImportReport)
    assert report.discovered > 0
    # 应该没有任何危险名的工具被导入
    imported_names = [b.name for b in report.bindings]
    assert not any("delete" in n or "shell" in n for n in imported_names)
    # 有被拒绝的（比如 gzip-file-as-resource 或危险参数）或全部通过
    assert report.imported == len(imported_names)
