"""Web 搜索与抓取能力：多 Provider 可插拔 + URL 访问策略 + 出站限速 + web.search/web.fetch 工具。

  - WebSearchProvider / WebFetchProvider 抽象
  - LocalMock 默认（离线可测），真 provider 只需实现同一接口
  - WebUrlPolicy（只允许 http/https）
  - OutboundLimiter（出站限速/配额：全局 + 单 host + 并发 + 日配额）
  - make_web_tools（web.search / web.fetch 技能卡）
"""

from warden_agent.web.outbound import (
    OutboundConfig,
    OutboundDecision,
    OutboundLimiter,
    outbound_from_env,
    parse_outbound_limit,
)
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
    "OutboundConfig",
    "OutboundDecision",
    "OutboundLimiter",
    "outbound_from_env",
    "parse_outbound_limit",
]
