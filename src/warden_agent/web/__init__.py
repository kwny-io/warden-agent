"""Web 搜索与抓取能力：多 Provider 可插拔 + URL 访问策略 + web.search/web.fetch 工具。

  - WebSearchProvider / WebFetchProvider 抽象
  - LocalMock 默认（离线可测），真 provider 只需实现同一接口
  - WebUrlPolicy（只允许 http/https）
  - make_web_tools（web.search / web.fetch 技能卡）
"""

from warden_agent.web.search import (
    LocalMockFetchProvider,
    LocalMockSearchProvider,
    WebFetchProvider,
    WebFetchResult,
    WebSearchProvider,
    WebSearchResult,
    WebUrlPolicy,
    make_web_tools,
)

__all__ = [
    "LocalMockFetchProvider",
    "LocalMockSearchProvider",
    "WebFetchProvider",
    "WebFetchResult",
    "WebSearchProvider",
    "WebSearchResult",
    "WebUrlPolicy",
    "make_web_tools",
]
