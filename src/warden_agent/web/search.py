"""Web 搜索 / 抓取工具族 —— 多 Provider 可插拔。

  - WebSearchProvider / WebFetchProvider 抽象（可插拔）。
  - 多个 provider（Tavily / Brave / AliyunIQS / Browserless 等）。
  - WebUrlPolicy：URL 访问策略——只允许 http/https，并**拒绝本机/环回/私有/保留地址**
    （防 SSRF）。真实联网 provider 还必须过一遍 DNS 解析校验，见下。
  - WebToolCatalog：注册 web.search / web.fetch 技能卡，Agent 自动会用。

本实现设计：
  - 抽象两个 Protocol：WebSearchProvider.search(query) / WebFetchProvider.fetch(url)。
  - 内置 LocalMockProvider：离线、可测、不花钱——真 provider（Tavily/Brave）只需再写一个
    类实现同样的接口即可接入（可插拔）。
  - WebUrlPolicy 是安全边界：协议白名单 + 主机/地址校验。真实联网 provider
    须声明 `requires_network = True`，工具会自动额外做 DNS 解析校验（防 DNS rebinding）。
  - web.search / web.fetch 通过 function_tool 暴露。
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from html import unescape
from typing import Any, Protocol
from urllib.parse import urljoin, urlparse

import httpx

from warden_agent.core.settings import env_bool, env_opt, env_str
from warden_agent.core.tracing import current_traceparent
from warden_agent.tool.catalog import ToolSpec, function_tool
from warden_agent.web.outbound import OutboundLimiter
from warden_agent.web.readability import extract_main_text

logger = logging.getLogger(__name__)


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

    # 不联网 → 不占用出站配额（真实 provider 必须置 True，与 LocalMockFetchProvider 同理）
    requires_network = False

    def __init__(self, entries: list[WebSearchResult] | None = None) -> None:
        self._entries = list(entries or [])

    def search(self, query: str, top_k: int = 5) -> list[WebSearchResult]:
        q = query.lower()
        matched = [e for e in self._entries if q in e.title.lower() or q in e.snippet.lower()]
        return matched[:top_k]


class LocalMockFetchProvider:
    """本地模拟抓取：按 URL 查预置内容，不联网。"""

    # 不联网 → 不需要 DNS 解析校验（真实 provider 必须置 True，见 WebUrlPolicy）
    requires_network = False

    def __init__(self, pages: dict[str, str] | None = None) -> None:
        self._pages = dict(pages or {})

    def fetch(self, url: str) -> WebFetchResult:
        if url in self._pages:
            return WebFetchResult(url=url, status=200, content=self._pages[url])
        return WebFetchResult(url=url, status=404, content="", error="页面不存在")


# ---- 真实联网：HTTP 抓取 ----
_TEXTUAL_CONTENT_HINTS = (
    "text/", "application/json", "application/xml", "application/xhtml",
)
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|noscript)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
_BLOCK_BREAK_RE = re.compile(
    r"<br\s*/?>|</(p|div|li|tr|h[1-6]|section|article)>", re.IGNORECASE
)
_TAG_RE = re.compile(r"<[^>]+>")
_INLINE_WS_RE = re.compile(r"[ \t\u00a0]+")
_BLANK_RE = re.compile(r"\n\s*\n+")


def _is_textual(content_type: str) -> bool:
    ct = (content_type or "").split(";")[0].strip().lower()
    return any(ct.startswith(hint) for hint in _TEXTUAL_CONTENT_HINTS)


def html_to_text(raw: str) -> str:
    """把 HTML 粗提取成纯文本：去 script/style → 块级标签转换行 → 去标签 → 还原实体。

    只是"够模型读"的粗提取，不是正文抽取算法（Readability 那一类）。
    目的是让 `web.fetch` 返回的东西别是一坨标签。
    """
    text = _SCRIPT_STYLE_RE.sub(" ", raw)
    text = _BLOCK_BREAK_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = unescape(text)
    text = _INLINE_WS_RE.sub(" ", text)
    text = _BLANK_RE.sub("\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def _pin_to_validated_ip(
    url: str, ip: str
) -> tuple[str, dict[str, str], dict[str, Any]]:
    """把请求固定到**已校验的那个 IP**，返回 `(请求URL, 额外头, extensions)`。

    入参 `ip` 必须来自**同一次**解析校验（见 `WebUrlPolicy.resolve_for_connection`）——
    这里**不再解析 DNS**。这是关键：如果这里重新解析一次，"校验时是公网、连接时是内网"
    的 DNS rebinding 窗口就又打开了。

    从 URL 里把主机名拆出去、换成 IP，同时：
      - `Host` 头保留原名（虚拟主机 / 签名要用）；
      - HTTPS 用 `sni_hostname` 扩展保留 SNI 与**证书校验的主机名**。
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    netloc = f"[{ip}]" if ":" in ip else ip          # IPv6 要加方括号
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    request_url = parsed._replace(netloc=netloc).geturl()
    host_header = host if parsed.port is None else f"{host}:{parsed.port}"
    extra_headers = {"Host": host_header}
    extensions: dict[str, Any] = (
        {"sni_hostname": host} if parsed.scheme.lower() == "https" else {}
    )
    return request_url, extra_headers, extensions


class HttpFetchProvider:
    """**真实联网抓取**：httpx GET 一个公网 URL，返回可读的文本正文。

    这是项目里第一个真正会发网络请求的能力。安全上有四道约束，缺一道都能被绕过：

      1. **工具层 DNS 校验**（`make_web_tools` 里）：发请求前 `check_for_network`。
      2. **每跳再校验**（本类的 `_follow`）：**不跟随 httpx 的自动重定向**，
         而是自己逐跳解析并用策略重新校验目标。原因见下面 `_follow` 的注释——
         自动重定向是 SSRF 的经典绕过口。
      3. **只吃文本类响应**：`text/*` / json / xml；二进制直接拒（别把图片塞进模型上下文）。
      4. **有界**：超时、最大响应体、最大跳转次数都有上限。
         不设上限等于把"要不要拖死我"的决定权交给对端。

    测试可以注入 `transport`（httpx.BaseTransport）与 `resolver`，因此全程离线、确定。
    """

    requires_network = True

    def __init__(
        self,
        *,
        timeout_s: float = 10.0,
        max_bytes: int = 200_000,
        max_redirects: int = 3,
        user_agent: str = "warden-agent/0.1 (+https://github.com/kwny-io/warden-agent)",
        transport: Any = None,
        resolver: Callable[[str], Iterable[str]] | None = None,
    ) -> None:
        self.timeout_s = timeout_s
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.user_agent = user_agent
        self._transport = transport
        self._resolver = resolver

    def fetch(self, url: str) -> WebFetchResult:
        """抓取 URL。**不抛异常**——策略拒绝/网络失败都转成带 error 的结果。"""
        try:
            return self._follow(url)
        except httpx.HTTPError as e:
            return WebFetchResult(url=url, status=0, content="", error=f"网络请求失败: {e}")
        except ValueError as e:  # 策略拒绝 / 跳转超限
            return WebFetchResult(url=url, status=0, content="", error=str(e))

    def _follow(self, url: str) -> WebFetchResult:
        """逐跳跟随重定向，**每一跳都重新过策略**。

        为什么不能直接用 httpx 的 `follow_redirects=True`：那等于信任对端给的
        `Location`。一个公网域名只要返回 `302 Location: http://169.254.169.254/`
        （云元数据端点），就能让"只校验初始 URL"的实现把内网请求打出去——
        这是 SSRF 的经典绕过。所以这里关掉自动跳转，自己逐跳校验。
        """
        current = url
        for _ in range(self.max_redirects + 1):
            # 校验与"要连哪个 IP"出自**同一次解析**，随后直接把请求固定到该 IP——
            # 这样就不存在"校验一次、连接再解析一次"的 DNS rebinding 窗口。
            ok, reason, ip = WebUrlPolicy.resolve_for_connection(current, self._resolver)
            if not ok:
                raise ValueError(f"[拒绝] {reason}")
            result, location = self._fetch_once(current, ip)
            if location is None:
                return result
            current = urljoin(current, location)
        raise ValueError(f"跳转次数超过上限 {self.max_redirects}（起始 {url}）")

    def _fetch_once(self, url: str, ip: str | None) -> tuple[WebFetchResult, str | None]:
        """抓一跳。返回 (结果, 下一跳地址或 None)。

        `ip` 是**已校验**的连接目标（来自 `resolve_for_connection`）；请求会固定到它，
        同时用 `Host` 头与 `sni_hostname` 保留原主机名。
        """
        headers = {"User-Agent": self.user_agent, "Accept": "text/*,application/json"}
        # 链路追踪：把当前链路的 traceparent 带给出站请求，下游（若也支持 W3C trace）
        # 的日志才能和我们的这次调用对上。没有开启追踪时 current_traceparent() 返回 None。
        traceparent = current_traceparent()
        if traceparent:
            headers["traceparent"] = traceparent
        request_url = url
        extensions: dict[str, Any] = {}
        if ip:
            request_url, extra_headers, extensions = _pin_to_validated_ip(url, ip)
            headers.update(extra_headers)
        with (
            httpx.Client(
                timeout=self.timeout_s,
                headers=headers,
                follow_redirects=False,   # 见 _follow：绝不自动跳
                transport=self._transport,
            ) as client,
            client.stream("GET", request_url, extensions=extensions) as resp,
        ):
            if resp.is_redirect:
                return (
                    WebFetchResult(url=url, status=resp.status_code, content=""),
                    resp.headers.get("location"),
                )
            if resp.status_code >= 400:
                return (
                    WebFetchResult(
                        url=url, status=resp.status_code, content="",
                        error=f"HTTP {resp.status_code}",
                    ),
                    None,
                )
            ctype = resp.headers.get("content-type", "")
            if not _is_textual(ctype):
                return (
                    WebFetchResult(
                        url=url, status=resp.status_code, content="",
                        error=f"不支持的内容类型 {ctype!r}（只抓文本类响应）",
                    ),
                    None,
                )
            buf = bytearray()
            truncated = False
            for chunk in resp.iter_bytes():
                room = self.max_bytes - len(buf)
                if len(chunk) > room:
                    buf.extend(chunk[:room])
                    truncated = True
                    break
                buf.extend(chunk)
            text = buf.decode(resp.encoding or "utf-8", errors="replace")
            # HTML 走**正文抽取**（Readability 那一类）：只留主要内容，把导航/侧边栏/页脚排掉。
            # 抽不出正文时它内部会退回整页粗提取（见 web/readability.py 的兜底说明）。
            body = extract_main_text(text) if "html" in ctype.lower() else text.strip()
            if truncated:
                body += f"\n…（正文超过 {self.max_bytes} 字节，已截断）"
            return WebFetchResult(url=url, status=resp.status_code, content=body), None


# 搜索 API 预设（第三方搜索服务的端点与方法）。`custom` 走 WARDEN_SEARCH_ENDPOINT。
_SEARCH_PRESETS: dict[str, dict[str, str]] = {
    "tavily": {"endpoint": "https://api.tavily.com/search", "method": "POST"},
    "brave": {"endpoint": "https://api.search.brave.com/res/v1/web/search", "method": "GET"},
}
_SEARCH_UA = "warden-agent/0.1 (+https://github.com/kwny-io/warden-agent)"


class HttpSearchProvider:
    """**真实联网搜索**：调第三方搜索 API（Tavily / Brave / 自定义 JSON 端点）。

    与 `HttpFetchProvider` 同一取向：**不抛异常**——网络/接口错误转成空结果并记日志，
    工具层据此回"没搜到"，而不是把异常打进会话。

    `requires_network = True` → 工具层会把它计入**出站限速/配额**（与抓取同一道闸门）。
    端点来自配置（非用户输入），仍过一次静态 URL 策略校验，避免把 API key 发到内网地址。

    测试可注入 `transport`（httpx.BaseTransport），全程离线、确定。
    """

    requires_network = True

    def __init__(
        self,
        provider: str = "custom",
        *,
        api_key: str = "",
        endpoint: str = "",
        timeout_s: float = 10.0,
        transport: Any = None,
    ) -> None:
        preset = _SEARCH_PRESETS.get(provider, {})
        self.provider = provider
        self.endpoint = endpoint or preset.get("endpoint", "")
        self.method = preset.get("method", "GET")
        self.api_key = api_key
        self.timeout_s = timeout_s
        self._transport = transport

    def search(self, query: str, top_k: int = 5) -> list[WebSearchResult]:
        if not self.endpoint:
            return []
        ok, reason = WebUrlPolicy().check(self.endpoint)
        if not ok:
            logger.warning("搜索端点被 URL 策略拒绝（%s）：%s", self.endpoint, reason)
            return []
        try:
            resp = self._request(query, top_k)
            resp.raise_for_status()
            return self._parse(resp.json(), top_k)
        except Exception as e:  # noqa: BLE001 - 搜索失败不该把异常打进会话
            logger.warning("搜索失败（provider=%s）：%s", self.provider, e)
            return []

    def _request(self, query: str, top_k: int) -> httpx.Response:
        headers = {"User-Agent": _SEARCH_UA, "Accept": "application/json"}
        traceparent = current_traceparent()
        if traceparent:
            headers["traceparent"] = traceparent
        with httpx.Client(timeout=self.timeout_s, transport=self._transport) as client:
            if self.method == "POST":
                body: dict[str, Any] = {"query": query, "max_results": top_k}
                if self.api_key:
                    body["api_key"] = self.api_key
                return client.post(self.endpoint, json=body, headers=headers)
            if self.api_key:
                headers["X-Subscription-Token"] = self.api_key
            return client.get(self.endpoint, params={"q": query, "count": top_k}, headers=headers)

    @staticmethod
    def _parse(data: Any, top_k: int) -> list[WebSearchResult]:
        """兼容常见返回形态：`{"results":[...]}` 与 `{"web":{"results":[...]}}`（Brave）。"""
        items: list[Any] = []
        if isinstance(data, dict):
            if isinstance(data.get("results"), list):
                items = data["results"]
            elif isinstance(data.get("web"), dict) and isinstance(
                data["web"].get("results"), list
            ):
                items = data["web"]["results"]
        out: list[WebSearchResult] = []
        for item in items[:top_k]:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or item.get("link") or "")
            if not url:
                continue
            out.append(WebSearchResult(
                title=str(item.get("title") or ""),
                url=url,
                snippet=str(
                    item.get("snippet") or item.get("content") or item.get("description") or ""
                ),
            ))
        return out


def providers_from_env(
    env: Mapping[str, str],
) -> tuple[WebSearchProvider, WebFetchProvider]:
    """按环境变量选 web provider 组合。

    - 默认（不设）：**离线 mock**——零网络、可测，演示与测试不受网络影响。
      这也是项目"不配任何 key 也能全链路跑通"的底线。
    - `WARDEN_WEB_FETCH=1`：`web.fetch` 换成**真实联网抓取**（`HttpFetchProvider`）。
    - `WARDEN_SEARCH_PROVIDER=tavily|brave|custom`：`web.search` 换成**真实联网搜索**
      （`HttpSearchProvider`）。tavily/brave 需配 `WARDEN_SEARCH_API_KEY`；
      custom 需配 `WARDEN_SEARCH_ENDPOINT`。**配不全就如实退回 mock 并告警**——
      绝不假装"能搜"（那只会让模型拿着空结果硬编）。

    为什么默认不开真实抓取/搜索：Agent 能主动访问外网是一个**应该由运维显式决定**的能力，
    不该悄悄打开。开了之后抓取每一跳仍受 WebUrlPolicy 约束（拒内网/环回/元数据地址）。
    """
    fetch_enabled = env_bool("WARDEN_WEB_FETCH", False, env)
    fetch: WebFetchProvider = HttpFetchProvider() if fetch_enabled else LocalMockFetchProvider()

    kind = env_str("WARDEN_SEARCH_PROVIDER", "", env).strip().lower()
    search: WebSearchProvider
    if kind in ("", "off", "0", "false", "mock"):
        search = LocalMockSearchProvider()
    else:
        api_key = env_opt("WARDEN_SEARCH_API_KEY", env) or ""
        endpoint = env_opt("WARDEN_SEARCH_ENDPOINT", env) or ""
        if kind in _SEARCH_PRESETS and not api_key:
            logger.warning(
                "WARDEN_SEARCH_PROVIDER=%s 但未配 WARDEN_SEARCH_API_KEY —— 搜索退回离线 mock"
                "（不会假装能搜）", kind,
            )
            search = LocalMockSearchProvider()
        elif kind == "custom" and not endpoint:
            logger.warning(
                "WARDEN_SEARCH_PROVIDER=custom 但未配 WARDEN_SEARCH_ENDPOINT —— 退回离线 mock"
            )
            search = LocalMockSearchProvider()
        else:
            search = HttpSearchProvider(kind, api_key=api_key, endpoint=endpoint)
    return search, fetch


# ---- URL 策略 ----
_ALLOWED_SCHEMES = {"http", "https"}

# 明确拒绝的主机名：本机别名 + 云元数据端点
_BLOCKED_HOSTNAMES = {
    "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
    "metadata", "metadata.google.internal", "metadata.goog",
}


def _parse_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """host 是标准 IP 字面量则返回地址对象，否则 None。

    注意：`ipaddress` 不认传统简写（127.1 / 十进制 2130706433 / 十六进制 0x7f.. /
    八进制 0177..）。这类写法被浏览器和部分 HTTP 客户端当作 127.0.0.1 解析，
    是 SSRF 的常见绕过手法——由 `_looks_like_legacy_ip` 单独拦。
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _looks_like_legacy_ip(host: str) -> bool:
    """是否为"传统 IP 写法"（纯数字或点分数字 / 0x 开头）——这类一律拒绝。"""
    h = host.strip()
    if not h:
        return False
    if h.lower().startswith("0x"):
        return True
    # 全是数字和点（正常域名不可能长这样：TLD 不会是纯数字）
    return all(ch.isdigit() or ch == "." for ch in h)


def _is_public_ip(ip: str) -> bool:
    """公网地址判定：私有/环回/链路本地/保留/多播/未指定 都不算公网。"""
    addr = _parse_ip(ip)
    if addr is None:
        return False
    return not (
        addr.is_private or addr.is_loopback or addr.is_link_local
        or addr.is_reserved or addr.is_multicast or addr.is_unspecified
    )


def _resolve_all(
    host: str, resolver: Callable[[str], Iterable[str]] | None = None
) -> list[str] | None:
    """解析主机的**全部**地址；host 本身是 IP 字面量时直接返回它。

    resolver 可注入（host -> list[ip]），便于离线测试 DNS rebinding 场景。
    解析失败返回 None。
    """
    if _parse_ip(host) is not None:
        return [host]
    if resolver is not None:
        try:
            return [str(x) for x in resolver(host)]
        except Exception:
            return None
    try:
        infos = socket.getaddrinfo(host, None)
        return sorted({str(i[4][0]) for i in infos})
    except Exception:
        return None


class WebUrlPolicy:
    """URL 访问策略：只允许 http/https，并拒绝本机/内网/保留地址（防 SSRF）。

    分两级，原因是"要不要解析 DNS"取决于 provider 是否真的联网：

      - check(url)             **静态**检查：协议白名单、host 是否存在、
                               主机名黑名单、IP 字面量是否公网。离线可用。
      - check_for_network(url) 静态检查 + **解析 DNS 并校验每一个解析结果**。
                               真实联网 provider 在发请求前必须走这一级。

    为什么必须有第二级：只做静态检查时，攻击者可以让域名**解析到内网**
    （DNS rebinding），静态看域名完全正常。所以必须校验解析后的每一个 IP。
    """

    @staticmethod
    def check(url: str) -> tuple[bool, str]:
        """静态检查（不解析 DNS）。返回 (是否允许, 原因)。"""
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        if not scheme:
            return False, "URL 缺少协议（需以 http:// 或 https:// 开头）"
        if scheme not in _ALLOWED_SCHEMES:
            return False, f"URL 协议 {scheme!r} 被策略禁止（只允许 http/https）"

        host = parsed.hostname
        if not host:
            return False, "URL 缺少主机名"
        host_l = host.lower().rstrip(".")
        if host_l in _BLOCKED_HOSTNAMES or host_l.endswith(".localhost"):
            return False, f"主机名 {host!r} 被策略禁止（本机/元数据端点）"

        addr = _parse_ip(host)
        if addr is not None:
            # 标准 IP 字面量：公网才放行
            if not _is_public_ip(host):
                return False, f"目标地址 {host!r} 不是公网地址（本机/内网/保留段）"
        elif _looks_like_legacy_ip(host):
            # 传统 IP 简写（127.1 / 十进制 / 十六进制 / 八进制）：标准解析认不出，
            # 但浏览器可能当成本机地址 → 直接拒绝，不给绕过留口子。
            return False, f"主机名 {host!r} 形似传统 IP 简写，被策略禁止"
        return True, "ok"

    @staticmethod
    def check_for_network(
        url: str, resolver: Callable[[str], Iterable[str]] | None = None
    ) -> tuple[bool, str]:
        """静态检查 + DNS 解析校验。真实联网 provider 发请求前必须调用。

        校验**所有**解析结果：任意一个落到内网/环回/保留段就整体拒绝。

        ⚠️ 但"先查后连"本身不够——**连接必须用这次校验出来的那个 IP**
        （见 `resolve_for_connection`）。只查不 pin，域名可以在两次解析之间改指向。
        """
        ok, reason, _ip = WebUrlPolicy.resolve_for_connection(url, resolver)
        return ok, reason

    @staticmethod
    def resolve_for_connection(
        url: str, resolver: Callable[[str], Iterable[str]] | None = None
    ) -> tuple[bool, str, str | None]:
        """静态检查 + DNS 校验，并**返回这次连接要用的 IP**（校验与连接同源）。

        返回 `(是否允许, 原因, 要连接的 IP)`。这是消除 SSRF 里 DNS rebinding 窗口的关键：
        调用方拿到这个 IP 后必须**直接连它**，不能再解析一次。
        """
        ok, reason = WebUrlPolicy.check(url)
        if not ok:
            return False, reason, None
        host = urlparse(url).hostname or ""
        ips = _resolve_all(host, resolver)
        if not ips:
            return False, f"域名 {host!r} 解析失败", None
        bad = [ip for ip in ips if not _is_public_ip(ip)]
        if bad:
            return False, f"域名 {host!r} 解析到非公网地址 {bad}（拒绝，防 SSRF）", None
        return True, "ok", ips[0]


# ---- 工具集 ----
def make_web_tools(
    search_provider: WebSearchProvider | None = None,
    fetch_provider: WebFetchProvider | None = None,
    url_policy: WebUrlPolicy | None = None,
    names: tuple[str, str] = ("web.search", "web.fetch"),
    outbound: OutboundLimiter | None = None,
) -> list[ToolSpec]:
    """造 web.search / web.fetch 两张技能卡。

    - search_provider / fetch_provider：可传真实 provider（Tavily/Brave）；None 用本地模拟。
    - url_policy：URL 访问策略，默认只放 http/https 且拒绝内网地址。
    - outbound：出站限速/配额闸门（见 `web/outbound.py`）。**默认开启**，参数偏保守：
      单次请求的超时/体积上限只界定"一次"，不界定"多少次"——总量无界这口子没有理由默认敞着。
      离线 provider（`requires_network = False`）不占用配额，所以演示与测试不受影响。

    安全：发请求前会做 DNS 解析校验（`check_for_network`），**默认即开启**；
    只有明确声明 `requires_network = False` 的纯离线 provider（如内置 mock）
    才走静态检查。默认严格是为了"新接入的 provider 天然安全"，不依赖作者记得声明。

    顺序：**URL 策略先于出站闸门**——被策略拒绝的 URL 不该消耗配额（拒绝不是"发出去了"）。
    """
    search: WebSearchProvider = search_provider or LocalMockSearchProvider()
    fetch: WebFetchProvider = fetch_provider or LocalMockFetchProvider()
    policy = url_policy or WebUrlPolicy()
    limiter = outbound or OutboundLimiter()
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
        if getattr(search, "requires_network", True):
            with limiter.guard(f"search:{type(search).__name__}") as decision:
                if decision.denied:
                    return f"[限流] {decision.reason}"
                results = search.search(query)
        else:
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
        # 默认走严格路径（含 DNS 解析校验）；只有显式声明离线的 provider 才跳过。
        # 默认严格 = 新接入的 provider 天然安全，不靠作者记得声明。
        online = bool(getattr(fetch, "requires_network", True))
        if online:
            ok, reason = policy.check_for_network(url)
        else:
            ok, reason = policy.check(url)
        if not ok:
            return f"[拒绝] {reason}"
        if online:
            # 策略已过，这时才占用出站配额（按 host 分桶，对单个站点保持礼貌）
            with limiter.guard(_host_of(url)) as decision:
                if decision.denied:
                    return f"[限流] {decision.reason}"
                result = fetch.fetch(url)
        else:
            result = fetch.fetch(url)
        if result.error:
            return f"[抓取失败] {result.error}"
        return result.content

    return [search_tool, fetch_tool]


def _host_of(url: str) -> str:
    """取 URL 的 host，作为出站限速的分桶键（取不到就归到 unknown）。"""
    return (urlparse(url).hostname or "unknown").lower()
