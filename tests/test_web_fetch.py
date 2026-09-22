"""真实联网抓取（`HttpFetchProvider`）测试。

这是项目里第一个真正会发网络请求的能力，所以测试的重点不是"能抓下来"，
而是**不能被抓成 SSRF 跳板**：
  - 非公网地址在发请求**之前**就被拒（云元数据端点 169.254.169.254 是头号目标）；
  - **重定向每一跳都重新校验** —— 公网 URL 返回 302 到内网地址是 SSRF 的经典绕过，
    用 httpx 的 `follow_redirects=True` 就会中招；
  - 超时/响应体/跳转次数都有界，且二进制内容不进上下文。

全程离线确定性：注入 `httpx.MockTransport` 与 `resolver`，不碰真实网络与 DNS。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import httpx
import pytest
from tests.conftest import ScriptedModel, weather_tool

from warden_agent.agent import augment_catalog
from warden_agent.model.model import ChatResponse
from warden_agent.policy.policy import PolicyEngine
from warden_agent.store.sqlite import SqliteStore
from warden_agent.tool.catalog import ToolCatalog
from warden_agent.web.search import (
    HttpFetchProvider,
    LocalMockFetchProvider,
    html_to_text,
    make_web_tools,
    providers_from_env,
)
from warden_agent.web.server import build_app

PUBLIC_IP = "93.184.216.34"


def _resolver(mapping: dict[str, list[str]] | None = None):  # type: ignore[no-untyped-def]
    """默认把任何域名解析成公网地址；mapping 可指定个别域名（用于 rebinding 场景）。"""
    table = mapping or {}

    def resolve(host: str) -> list[str]:
        return table.get(host, [PUBLIC_IP])

    return resolve


class _Seen(list):
    """记录请求的**逻辑 URL**（按 Host 头还原），并附带"实际连到哪"等物理信息。

    为什么要分两栏：现在连接被固定到**已校验的 IP**（防 DNS rebinding），
    所以"用户看到的 URL"和"实际连的地址"不再相同。
    断言"请求了哪些 URL"看逻辑 URL；断言"连到哪个 IP / SNI 是什么"看 physical / sni。
    """

    def __init__(self) -> None:
        super().__init__()
        self.physical: list[str] = []
        self.sni: list[str | None] = []


def _mock(routes: dict[str, tuple[int, dict[str, str], str]]):  # type: ignore[no-untyped-def]
    """造一个 MockTransport + 请求记录。routes: 逻辑URL -> (status, headers, body)。"""
    seen = _Seen()

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.headers.get("host", "")
        logical = f"{request.url.scheme}://{host}{request.url.path}"
        if request.url.query:
            logical += f"?{request.url.query}"
        seen.append(logical)
        seen.physical.append(str(request.url))
        seen.sni.append(request.extensions.get("sni_hostname"))  # type: ignore[arg-type]
        if logical not in routes:
            return httpx.Response(500, content=f"未预置的路由: {logical}")
        status, headers, body = routes[logical]
        return httpx.Response(status, headers=headers, content=body.encode("utf-8"))

    return httpx.MockTransport(handler), seen


def _provider(routes, *, resolver=None, **kw):  # type: ignore[no-untyped-def]
    transport, seen = _mock(routes)
    return HttpFetchProvider(
        transport=transport, resolver=resolver or _resolver(), **kw
    ), seen


# ---------- 正常抓取 ----------


def test_抓取HTML并提取正文() -> None:
    provider, _seen = _provider({
        "https://example.com/": (200, {"content-type": "text/html; charset=utf-8"}, (
            "<html><head><title>标题</title><style>body{color:red}</style></head>"
            "<body><h1>大标题</h1><script>evil()</script>"
            "<p>第一段正文</p><p>第二段&nbsp;正文</p></body></html>"
        )),
    })
    result = provider.fetch("https://example.com/")
    assert result.status == 200 and result.error is None
    assert "大标题" in result.content
    assert "第一段正文" in result.content
    assert "第二段 正文" in result.content      # 实体还原 + 空白收敛
    assert "evil()" not in result.content       # script 内容被剔除
    assert "color:red" not in result.content    # style 内容被剔除
    assert "<" not in result.content            # 标签没了


def test_纯文本原样返回() -> None:
    provider, _seen = _provider({
        "https://example.com/plain.txt": (200, {"content-type": "text/plain"}, "  hello  "),
    })
    result = provider.fetch("https://example.com/plain.txt")
    assert result.content == "hello"


def test_http错误状态不抛异常() -> None:
    provider, _seen = _provider({
        "https://example.com/404": (404, {"content-type": "text/plain"}, "not found"),
    })
    result = provider.fetch("https://example.com/404")
    assert result.status == 404
    assert result.error == "HTTP 404"


def test_二进制内容被拒() -> None:
    provider, _seen = _provider({
        "https://example.com/a.png": (200, {"content-type": "image/png"}, "binary"),
    })
    result = provider.fetch("https://example.com/a.png")
    assert result.error is not None and "不支持的内容类型" in result.error


def test_超大响应被截断() -> None:
    provider, _seen = _provider(
        {"https://example.com/big": (200, {"content-type": "text/plain"}, "x" * 5000)},
        max_bytes=100,
    )
    result = provider.fetch("https://example.com/big")
    assert "已截断" in result.content
    assert len(result.content) < 300          # 没有把 5000 字节全吞进来


def test_网络异常转成error而不是抛出() -> None:
    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("连不上")

    provider = HttpFetchProvider(
        transport=httpx.MockTransport(boom), resolver=_resolver()
    )
    result = provider.fetch("https://example.com/")
    assert result.status == 0
    assert result.error is not None and "网络请求失败" in result.error


# ---------- SSRF：这才是重点 ----------


@pytest.mark.parametrize("bad", [
    "http://169.254.169.254/latest/meta-data/",   # 云元数据端点（头号目标）
    "http://127.0.0.1:8080/admin",                # 环回
    "http://10.0.0.5/internal",                   # 私有
])
def test_非公网地址在发请求前就被拒(bad: str) -> None:
    provider, seen = _provider({})
    result = provider.fetch(bad)
    assert result.error is not None and "[拒绝]" in result.error
    assert seen == [], f"不该发出任何请求，却请求了 {seen}"


def test_重定向到内网地址被拦_经典SSRF绕过() -> None:
    """公网 URL 返回 302 到内网 —— 只校验初始 URL 的实现会在这里把内网请求打出去。"""
    provider, seen = _provider({
        "https://public.example.com/": (
            302,
            {"location": "http://169.254.169.254/latest/meta-data/"},
            "",
        ),
    })
    result = provider.fetch("https://public.example.com/")
    assert result.error is not None and "[拒绝]" in result.error
    # 第一跳请求了（它是公网），但内网地址**一次都没被请求**
    assert seen == ["https://public.example.com/"]


def test_重定向到解析为内网的域名也被拦() -> None:
    """域名看着正常，但解析到内网 —— 必须靠"解析后 IP 校验"拦下（防 DNS rebinding）。"""
    provider, seen = _provider(
        {
            "https://public.example.com/": (
                302, {"location": "https://internal.example.com/x"}, "",
            ),
        },
        resolver=_resolver({"internal.example.com": ["192.168.1.10"]}),
    )
    result = provider.fetch("https://public.example.com/")
    assert result.error is not None and "[拒绝]" in result.error
    assert "internal.example.com" not in " ".join(seen)


def test_重定向到公网正常跟随() -> None:
    provider, seen = _provider({
        "https://a.example.com/": (302, {"location": "https://b.example.com/ok"}, ""),
        "https://b.example.com/ok": (200, {"content-type": "text/plain"}, "到了"),
    })
    result = provider.fetch("https://a.example.com/")
    assert result.content == "到了"
    assert seen == ["https://a.example.com/", "https://b.example.com/ok"]


# ---------- 连接固定到已校验 IP（防 DNS rebinding）----------


def test_连接固定到已校验IP_防DNS重绑定() -> None:
    """核心回归：域名"校验时解析到公网、连接时改解析到环回"必须无效。

    做法：resolver 第一次返回公网 IP，之后一律返回 127.0.0.1（模拟 rebinding）。
    实现若"校验一次、连接时再解析一次"，连接就会打到 127.0.0.1 —— 这正是审计指出的口子。
    现在连接用的是**校验时那个 IP**，所以：
      - 实际连的是公网 IP；
      - resolver 只被调用一次（证明没有二次解析）。
    """
    calls = {"n": 0}

    def rebinding_resolver(host: str) -> list[str]:
        calls["n"] += 1
        return [PUBLIC_IP] if calls["n"] == 1 else ["127.0.0.1"]

    transport, seen = _mock({
        "https://example.com/": (200, {"content-type": "text/plain"}, "ok"),
    })
    provider = HttpFetchProvider(transport=transport, resolver=rebinding_resolver)

    assert provider.fetch("https://example.com/").content == "ok"
    assert seen.physical == [f"https://{PUBLIC_IP}/"], f"连到了 {seen.physical}"
    assert "127.0.0.1" not in " ".join(seen.physical)
    assert calls["n"] == 1, "校验与连接必须同源：不该再解析第二次"


def test_固定连接时保留主机名与SNI() -> None:
    """换成 IP 连之后，Host 头与 TLS SNI 必须仍是原主机名——否则虚拟主机/证书校验会挂。"""
    transport, seen = _mock({
        "https://example.com/x": (200, {"content-type": "text/plain"}, "ok"),
    })
    provider = HttpFetchProvider(transport=transport, resolver=_resolver())
    assert provider.fetch("https://example.com/x").content == "ok"
    assert seen == ["https://example.com/x"]        # 逻辑 URL（按 Host 头还原）不变
    assert seen.sni == ["example.com"]              # HTTPS 带上了 sni_hostname


def test_解析失败时拒绝而不是回落成不固定连接() -> None:
    """解析不出来时必须**拒绝**：回落到"不 pin 的连接"等于把 SSRF 的洞重新打开。"""
    transport, seen = _mock({})
    provider = HttpFetchProvider(transport=transport, resolver=lambda host: [])
    result = provider.fetch("https://example.com/")
    assert result.error is not None and "[拒绝]" in result.error
    assert seen == [], "被拒的请求不该发出去"


def test_IPv6地址固定时加方括号() -> None:
    """IPv6 字面量在 URL 里必须加方括号，否则拼出来的 URL 是非法的。"""
    v6 = "2606:2800:220:1:248:1893:25c8:1946"
    transport, seen = _mock({
        "https://v6.example.com/": (200, {"content-type": "text/plain"}, "ok"),
    })
    provider = HttpFetchProvider(transport=transport, resolver=lambda host: [v6])
    assert provider.fetch("https://v6.example.com/").content == "ok"
    assert seen.physical == [f"https://[{v6}]/"]


def test_带端口的URL固定后端口保留() -> None:
    transport, seen = _mock({
        "https://example.com:8443/a": (200, {"content-type": "text/plain"}, "ok"),
    })
    provider = HttpFetchProvider(transport=transport, resolver=_resolver())
    assert provider.fetch("https://example.com:8443/a").content == "ok"
    assert seen.physical == [f"https://{PUBLIC_IP}:8443/a"]
    assert seen == ["https://example.com:8443/a"]


def test_跳转次数超上限() -> None:
    """自指重定向：必须停下来，不能无限跟。"""
    provider, _seen = _provider(
        {"https://loop.example.com/": (302, {"location": "https://loop.example.com/"}, "")},
        max_redirects=2,
    )
    result = provider.fetch("https://loop.example.com/")
    assert result.error is not None and "跳转次数超过上限" in result.error


def test_相对跳转地址能正确解析() -> None:
    provider, seen = _provider({
        "https://a.example.com/dir/x": (302, {"location": "/next"}, ""),
        "https://a.example.com/next": (200, {"content-type": "text/plain"}, "ok"),
    })
    assert provider.fetch("https://a.example.com/dir/x").content == "ok"
    assert seen[-1] == "https://a.example.com/next"


# ---------- html_to_text ----------


def test_去掉脚本样式并保留换行() -> None:
    text = html_to_text(
        "<div>开头</div><script>var a=1;</script><div>结尾</div>"
    )
    assert "开头" in text and "结尾" in text
    assert "var a" not in text
    assert "\n" in text


# ---------- 工具层与装配 ----------


def test_web_fetch工具走真实provider() -> None:
    """工具层校验发生在 provider 之外，用它自己的 DNS 解析。

    所以这里刻意用**公网 IP 字面量**而不是域名：IP 字面量不需要解析 DNS，
    测试就不再受运行环境 DNS 影响（有些沙箱/代理会把所有域名通配解析到保留段，
    域名写法会因此被策略拒掉，导致测试随环境飘）。
    """
    provider, _seen = _provider({
        "https://93.184.216.34/": (200, {"content-type": "text/plain"}, "真实内容"),
    })
    catalog = ToolCatalog()
    for spec in make_web_tools(None, provider):
        catalog.register(spec)
    out = str(catalog.execute("web.fetch", {"url": "https://93.184.216.34/"}))
    assert "真实内容" in out


def test_web_fetch工具对内网URL返回拒绝() -> None:
    provider, seen = _provider({})
    catalog = ToolCatalog()
    for spec in make_web_tools(None, provider):
        catalog.register(spec)
    out = str(catalog.execute("web.fetch", {"url": "http://169.254.169.254/"}))
    assert "[拒绝]" in out
    assert seen == []


def test_providers_from_env开关() -> None:
    # 默认：离线 mock（不联网）
    _search, fetch = providers_from_env({})
    assert isinstance(fetch, LocalMockFetchProvider)

    # 显式关闭同样走 mock
    for off in ("0", "false", "off", ""):
        assert isinstance(providers_from_env({"WARDEN_WEB_FETCH": off})[1], LocalMockFetchProvider)

    # 开启才换成真实抓取
    for on in ("1", "true", "yes", "on"):
        assert isinstance(providers_from_env({"WARDEN_WEB_FETCH": on})[1], HttpFetchProvider)


def test_augment_catalog记录实际抓取实现() -> None:
    """能力清单要能看出外网出口是开还是关。"""
    catalog = ToolCatalog()
    extra = augment_catalog(catalog, web_providers=(None, HttpFetchProvider()))
    assert extra["web_fetch"] == "HttpFetchProvider"
    assert "web.fetch" in [t.name for t in catalog.all()]

    catalog2 = ToolCatalog()
    extra2 = augment_catalog(catalog2, web=True)   # 只在 web=True，无 provider
    assert extra2["web_fetch"] == "LocalMockFetchProvider"


@pytest.mark.asyncio
async def test_http能力清单暴露抓取实现() -> None:
    app = build_app(
        model=ScriptedModel([ChatResponse(content="好", finish_reason="stop")]),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=SqliteStore(Path(tempfile.mkdtemp()) / "t.db"),
        web=True,
        web_providers=(None, HttpFetchProvider(transport=httpx.MockTransport(
            lambda _r: httpx.Response(200, headers={"content-type": "text/plain"}, content=b"x")
        ), resolver=_resolver())),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        caps = (await c.get("/capabilities")).json()
        assert caps["features"]["web_fetch"] == "HttpFetchProvider"
