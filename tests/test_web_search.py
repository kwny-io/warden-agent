"""web.search / web.fetch 工具族测试。"""
from __future__ import annotations

from warden_agent.tool.catalog import ToolCatalog
from warden_agent.web import (
    LocalMockFetchProvider,
    LocalMockSearchProvider,
    WebSearchResult,
    WebUrlPolicy,
    make_web_tools,
)


def _catalog() -> ToolCatalog:
    search = LocalMockSearchProvider([
        WebSearchResult("哈法 Agent 中文文档", "https://warden.local/docs",
                        "这是一个 Agent 运行时框架的中文文档"),
        WebSearchResult("AI Agent 入门", "https://example.com/ai",
                        "介绍 AI Agent 的基本概念"),
    ])
    fetch = LocalMockFetchProvider({
        "https://warden.local/docs": "# Warden Agent\n Agent 运行时。",
    })
    catalog = ToolCatalog()
    for spec in make_web_tools(search_provider=search, fetch_provider=fetch):
        catalog.register(spec)
    return catalog


def test_web_search_返回相关结果() -> None:
    catalog = _catalog()
    out = catalog.execute("web.search", {"query": "Agent"})
    assert "哈法 Agent 中文文档" in str(out)
    assert "https://warden.local/docs" in str(out)


def test_web_search_无结果友好提示() -> None:
    catalog = _catalog()
    out = catalog.execute("web.search", {"query": "不存在的乱七八糟"})
    assert "没有搜到" in str(out)


def test_web_fetch_成功() -> None:
    catalog = _catalog()
    out = catalog.execute("web.fetch", {"url": "https://warden.local/docs"})
    assert "Warden Agent" in str(out)


def test_web_fetch_拒绝危险协议() -> None:
    """URL 策略：file:// 不允许。"""
    catalog = _catalog()
    out = catalog.execute("web.fetch", {"url": "file:///etc/passwd"})
    assert "[拒绝]" in str(out)


def test_urlPolicy() -> None:
    ok, _ = WebUrlPolicy.check("https://example.com")
    assert ok
    bad, reason = WebUrlPolicy.check("file:///etc/passwd")
    assert not bad
    assert "禁止" in reason
