"""Web 搜索 / 抓取工具族 —— 多 Provider 可插拔。

  - WebSearchProvider / WebFetchProvider 抽象（可插拔）。
  - 多个 provider（Tavily / Brave / AliyunIQS / Browserless 等）。
  - WebUrlPolicy：URL 访问策略（默认只允许 http/https，防 file:// 这类危险协议）。
  - WebToolCatalog：注册 web.search / web.fetch 技能卡，Agent 自动会用。

本实现设计：
  - 抽象两个 Protocol：WebSearchProvider.search(query) / WebFetchProvider.fetch(url)。
  - 内置 LocalMockProvider：离线、可测、不花钱——真 provider（Tavily/Brave）只需再写一个
    类实现同样的接口即可接入（可插拔）。
  - WebUrlPolicy 挡住非 http/https（file://、ftp:// 等），安全边界。
  - web.search / web.fetch 通过 function_tool 暴露。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

from warden_agent.tool.catalog import ToolSpec, function_tool


# ---- 结果模型 ----
@dataclass
class WebSearchResult:
    title: str
    url: str
    snippet: str


@dataclass
class WebFetchResult:
    url: str
    status: int
    content: str
    error: str | None = None


# ---- Provider 抽象（可插拔）----
class WebSearchProvider(Protocol):
    def search(self, query: str, top_k: int = 5) -> list[WebSearchResult]: ...


class WebFetchProvider(Protocol):
    def fetch(self, url: str) -> WebFetchResult: ...


# ---- 内置：本地模拟 provider（离线可测）----
class LocalMockSearchProvider:
    """本地模拟搜索：命中本地"知识库"里的条目，不联网、确定、可测。"""

    def __init__(self, entries: list[WebSearchResult] | None = None) -> None:
        self._entries = list(entries or [])

    def search(self, query: str, top_k: int = 5) -> list[WebSearchResult]:
        q = query.lower()
        matched = [e for e in self._entries if q in e.title.lower() or q in e.snippet.lower()]
        return matched[:top_k]


class LocalMockFetchProvider:
    """本地模拟抓取：按 URL 查预置内容，不联网。"""

    def __init__(self, pages: dict[str, str] | None = None) -> None:
        self._pages = dict(pages or {})

    def fetch(self, url: str) -> WebFetchResult:
        if url in self._pages:
            return WebFetchResult(url=url, status=200, content=self._pages[url])
        return WebFetchResult(url=url, status=404, content="", error="页面不存在")


# ---- URL 策略 ----
class WebUrlPolicy:
    """URL 访问策略：默认只允许 http/https，挡住文件/本地协议等危险目标。"""

    @staticmethod
    def check(url: str) -> tuple[bool, str]:
        scheme = urlparse(url).scheme.lower()
        if scheme in ("http", "https"):
            return True, "ok"
        if not scheme:
            return True, "ok"
        return False, f"URL 协议 {scheme!r} 被策略禁止（只允许 http/https）"


# ---- 工具集 ----
def make_web_tools(
    search_provider: WebSearchProvider | None = None,
    fetch_provider: WebFetchProvider | None = None,
    url_policy: WebUrlPolicy | None = None,
    names: tuple[str, str] = ("web.search", "web.fetch"),
) -> list[ToolSpec]:
    """造 web.search / web.fetch 两张技能卡。

    - search_provider / fetch_provider：可传真实 provider（Tavily/Brave）；None 用本地模拟。
    - url_policy：URL 访问策略，默认只放 http/https。
    """
    search: WebSearchProvider = search_provider or LocalMockSearchProvider()
    fetch: WebFetchProvider = fetch_provider or LocalMockFetchProvider()
    policy = url_policy or WebUrlPolicy()
    search_name, fetch_name = names

    @function_tool(
        search_name,
        "在网上搜索与关键词相关的资料，返回结果标题/链接/摘要。当你需要实时外部信息时用它。",
        {"type": "object",
         "properties": {"query": {"type": "string", "description": "搜索关键词"}},
         "required": ["query"]},
        pure=True,
    )
    def search_tool(query: str) -> str:
        results = search.search(query)
        if not results:
            return "没有搜到相关结果。"
        return "\n".join(
            f"{i+1}. {r.title}: {r.url}\n   {r.snippet}" for i, r in enumerate(results)
        )

    @function_tool(
        fetch_name,
        "抓取一个网页的正文内容。需要明确的完整 URL。",
        {"type": "object",
         "properties": {"url": {"type": "string", "description": "要抓取的完整 URL"}},
         "required": ["url"]},
        pure=True,
    )
    def fetch_tool(url: str) -> str:
        ok, reason = policy.check(url)
        if not ok:
            return f"[拒绝] {reason}"
        result = fetch.fetch(url)
        if result.error:
            return f"[抓取失败] {result.error}"
        return result.content

    return [search_tool, fetch_tool]
