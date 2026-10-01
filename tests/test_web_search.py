"""web.search / web.fetch 工具族测试。"""
from __future__ import annotations

from warden_agent.tool.catalog import ToolCatalog
from warden_agent.web import (
    LocalMockFetchProvider,
    LocalMockSearchProvider,
    WebFetchResult,
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


# ---- SSRF 防护：静态检查（不解析 DNS）----

def test_urlPolicy_拒绝无协议URL() -> None:
    ok, reason = WebUrlPolicy.check("example.com/x")
    assert not ok
    assert "协议" in reason


def test_urlPolicy_拒绝本机与内网地址() -> None:
    """环回 / 私有 / 链路本地(云元数据) / 保留段 一律拒绝。"""
    for url in (
        "http://localhost/x",
        "http://127.0.0.1:8000/audit",
        "http://10.0.0.5/x",
        "http://192.168.1.1/x",
        "http://172.16.0.1/x",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/x",
        "http://0.0.0.0/x",
    ):
        ok, reason = WebUrlPolicy.check(url)
        assert not ok, f"应拒绝: {url}"
        assert reason


def test_urlPolicy_拒绝云元数据主机名() -> None:
    ok, _ = WebUrlPolicy.check("http://metadata.google.internal/computeMetadata/v1/")
    assert not ok


def test_urlPolicy_允许公网地址() -> None:
    for url in ("https://example.com/x", "http://93.184.216.34/x", "https://warden.local/docs"):
        ok, reason = WebUrlPolicy.check(url)
        assert ok, f"应允许: {url} ({reason})"


def test_urlPolicy_拒绝传统IP简写() -> None:
    """127.1 / 十进制 / 十六进制 / 八进制 都可能被当成本机地址，是 SSRF 常见绕过。"""
    for url in ("http://127.1/x", "http://2130706433/x",
                "http://0x7f000001/x", "http://0177.0.0.1/x"):
        ok, reason = WebUrlPolicy.check(url)
        assert not ok, f"应拒绝: {url}"
        assert reason


def test_urlPolicy_拒绝CGNAT与云元数据地址() -> None:
    """100.64.0.0/10（含阿里云元数据 100.100.100.200）不得被当成公网段放行。"""
    for url in (
        "http://100.64.0.1/x",
        "http://100.100.100.200/latest/meta-data",
        "http://[fd00:ec2::254]/x",
    ):
        ok, reason = WebUrlPolicy.check(url)
        assert not ok, f"应拒绝: {url}"
        assert reason


# ---- SSRF 防护：DNS 解析校验（防域名指向内网）----

def test_check_for_network_放行解析到公网的域名() -> None:
    ok, reason = WebUrlPolicy.check_for_network(
        "https://evil.example/x", resolver=lambda h: ["93.184.216.34"])
    assert ok, reason


def test_check_for_network_拦截解析到内网的域名() -> None:
    """DNS rebinding：域名看着正常，但解析到内网 → 必须拒绝。"""
    ok, reason = WebUrlPolicy.check_for_network(
        "https://evil.example/x", resolver=lambda h: ["10.0.0.9"])
    assert not ok
    assert "非公网" in reason


def test_check_for_network_多结果里有一个内网就拒绝() -> None:
    """解析出多个地址时，只要有一个落内网就整体拒绝。"""
    ok, _ = WebUrlPolicy.check_for_network(
        "https://mixed.example/x",
        resolver=lambda h: ["93.184.216.34", "127.0.0.1"])
    assert not ok


def test_check_for_network_解析失败即拒绝() -> None:
    """拿不准就不放行（失败关闭）。"""
    def boom(host: str) -> list[str]:
        raise OSError("dns down")

    ok, reason = WebUrlPolicy.check_for_network("https://x.example/y", resolver=boom)
    assert not ok
    assert "解析失败" in reason


def test_check_for_network_拦截解析到元数据与CGNAT() -> None:
    """与静态检查同一判定：解析到 CGNAT / 云元数据地址也必须拒。"""
    for ip in ("100.64.0.1", "100.100.100.200", "169.254.169.254"):
        ok, _ = WebUrlPolicy.check_for_network(
            "https://evil.example/x", resolver=lambda h, ip=ip: [ip])
        assert not ok, f"应拒绝解析到 {ip}"


def test_check_for_network_公网地址放行() -> None:
    ok, reason = WebUrlPolicy.check_for_network(
        "https://ok.example/x", resolver=lambda h: ["8.8.8.8"])
    assert ok, reason


# ---- 工具层：真实联网 provider 自动获得 DNS 校验 ----

class _FakeNetworkFetch:
    """模拟"真实联网"的抓取 provider。"""

    requires_network = True

    def __init__(self) -> None:
        self.called = False

    def fetch(self, url: str):  # type: ignore[no-untyped-def]
        self.called = True
        return WebFetchResult(url=url, status=200, content="不该被抓到")


def _catalog_with(fetch) -> ToolCatalog:  # type: ignore[no-untyped-def]
    catalog = ToolCatalog()
    for spec in make_web_tools(fetch_provider=fetch):
        catalog.register(spec)
    return catalog


def test_联网provider被静态拒绝时不发请求() -> None:
    fetch = _FakeNetworkFetch()
    out = _catalog_with(fetch).execute("web.fetch", {"url": "http://127.0.0.1:8000/audit"})
    assert "[拒绝]" in str(out)
    assert fetch.called is False, "被拒绝的 URL 绝不能真的去抓"


def test_联网provider对公网地址放行() -> None:
    fetch = _FakeNetworkFetch()
    out = _catalog_with(fetch).execute("web.fetch", {"url": "https://93.184.216.34/x"})
    assert "[拒绝]" not in str(out)
    assert fetch.called is True


def test_本地mockprovider不要求联网() -> None:
    """离线 mock 不该因为校验过严而连测试都跑不了。"""
    assert LocalMockFetchProvider.requires_network is False


def test_未声明联网的provider默认走严格校验(monkeypatch) -> None:
    """默认必须严格：新接入的 provider 不声明也该受保护，不靠作者记得声明。"""
    import socket as _socket

    class _Undeclared:
        def fetch(self, url: str):  # type: ignore[no-untyped-def]
            return WebFetchResult(url=url, status=200, content="水")

    # 让域名解析到内网（模拟 DNS rebinding）
    monkeypatch.setattr(
        _socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("10.0.0.9", 0))])

    out = _catalog_with(_Undeclared()).execute(
        "web.fetch", {"url": "https://evil.example/x"})
    assert "[拒绝]" in str(out)


# ---------- “不是真的就说不是真的”（离线演示 provider）----------


def test_演示搜索结果必须自报演示() -> None:
    """模型分不出 mock 和真结果：给模型看的文本里必须写清“这是演示数据”。

    否则内置样例里的链接会被当成事实引用——能力少不可怕，装作有能力才可怕。
    """
    out = str(_catalog().execute("web.search", {"query": "Agent"}))
    assert out.startswith("[离线演示数据]"), out
    assert "不是真实互联网结果" in out
    assert "哈法 Agent 中文文档" in out, "标了演示也要照旧返回结果（它是演示用数据）"


def test_演示搜索无结果也带演示前缀() -> None:
    out = str(_catalog().execute("web.search", {"query": "不存在的乱七八糟"}))
    assert out.startswith("[离线演示数据]")
    assert "没有搜到" in out


def test_演示抓取失败不能讲成404() -> None:
    """离线 provider 里没有这条 URL，**不等于真实网络上 404**。

    说成 404，模型会得出“这个链接是死链”的结论——而真相是它根本没联网。
    """
    out = str(_catalog().execute("web.fetch", {"url": "https://warden.local/没有的页面"}))
    assert out.startswith("[离线演示数据]")
    assert "不是真实网络请求失败" in out


def test_真provider不加演示前缀() -> None:
    """“没这条前缀”必须是真结果的标志，否则前缀就失去意义了。"""

    class _RealSearch:
        requires_network = False
        is_mock = False

        def search(self, query: str, top_k: int = 5) -> list[WebSearchResult]:
            return [WebSearchResult("真结果", "https://example.com/real", "真的")]

    catalog = ToolCatalog()
    for spec in make_web_tools(search_provider=_RealSearch()):
        catalog.register(spec)
    out = str(catalog.execute("web.search", {"query": "x"}))
    assert out == "1. 真结果: https://example.com/real\n   真的", out


def test_搜索没打通不能报成没有结果() -> None:
    """provider 故障（网络错误/配置写错）过去被压成空列表 → 工具回“没有搜到相关结果”，

    模型会理解成“世上没有这条信息”，然后基于“没有”继续编。必须分开说。
    """

    class _BrokenSearch:
        requires_network = False
        is_mock = False
        last_error = "搜索请求失败：httpx.ConnectError: boom"

        def search(self, query: str, top_k: int = 5) -> list[WebSearchResult]:
            return []

    catalog = ToolCatalog()
    for spec in make_web_tools(search_provider=_BrokenSearch()):
        catalog.register(spec)
    out = str(catalog.execute("web.search", {"query": "x"}))
    assert "[搜索失败]" in out, out
    assert "httpx.ConnectError" in out, "失败原因要带出来，不然运维无从下手"
    assert "没有搜到" not in out, "故障不能讲成“没有结果”"


def test_搜索端点为空时也报失败而不是没有结果() -> None:
    """回归：`HttpSearchProvider` 没端点时曾经静默返回 []。"""
    from warden_agent.web.search import HttpSearchProvider

    p = HttpSearchProvider("custom", api_key="test-key", endpoint="")
    assert p.search("x") == []
    assert "WARDEN_SEARCH_ENDPOINT" in p.last_error
