"""真实搜索 provider（HttpSearchProvider）与 provider 选择测试（全程离线，MockTransport）。

本文件里的"密钥"全是**运行时随机生成**的，源码里不含任何真实凭据（仓库硬约定）。
"""

from __future__ import annotations

import json
import secrets

import httpx

from warden_agent.web.search import (
    HttpSearchProvider,
    LocalMockSearchProvider,
    providers_from_env,
)

# 运行时的假密钥（绝不在源码里写形似凭据的字面量）
_KEY = "test-" + secrets.token_hex(12)


def _resolve_public(host: str) -> list[str]:
    """离线解析器：测试端点一律视作解析到公网，避免真实 DNS 依赖。"""
    return ["93.184.216.34"]


# ---------- provider 选择（providers_from_env）----------


def test_默认是离线mock() -> None:
    search, _ = providers_from_env({})
    assert isinstance(search, LocalMockSearchProvider)


def test_tavily配了key就用真实provider() -> None:
    search, _ = providers_from_env({
        "WARDEN_SEARCH_PROVIDER": "tavily",
        "WARDEN_SEARCH_API_KEY": _KEY,
    })
    assert isinstance(search, HttpSearchProvider)
    assert search.endpoint == "https://api.tavily.com/search"
    assert search.method == "POST"


def test_brave用GET() -> None:
    search, _ = providers_from_env({
        "WARDEN_SEARCH_PROVIDER": "brave",
        "WARDEN_SEARCH_API_KEY": _KEY,
    })
    assert isinstance(search, HttpSearchProvider)
    assert search.method == "GET"
    assert "api.search.brave.com" in search.endpoint


def test_配不全如实退回mock而不是假装能搜() -> None:
    # tavily 没 key
    search, _ = providers_from_env({"WARDEN_SEARCH_PROVIDER": "tavily"})
    assert isinstance(search, LocalMockSearchProvider)
    # custom 没端点
    search, _ = providers_from_env({"WARDEN_SEARCH_PROVIDER": "custom"})
    assert isinstance(search, LocalMockSearchProvider)
    # custom 有端点
    search, _ = providers_from_env({
        "WARDEN_SEARCH_PROVIDER": "custom",
        "WARDEN_SEARCH_ENDPOINT": "https://search.example.com/api",
    })
    assert isinstance(search, HttpSearchProvider)
    assert search.endpoint == "https://search.example.com/api"


# ---------- 解析与请求形态 ----------


def test_tavily形态_POST带key并解析results() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "results": [
                {"title": "A", "url": "https://a.example", "content": "摘要A"},
                {"title": "B", "url": "https://b.example", "content": "摘要B"},
            ]
        })

    p = HttpSearchProvider("tavily", api_key=_KEY, transport=httpx.MockTransport(handler),
                           resolver=_resolve_public)
    out = p.search("agent 运行时", top_k=5)
    assert captured["method"] == "POST"
    body = captured["body"]
    assert body["query"] == "agent 运行时" and body["max_results"] == 5
    assert body["api_key"] == _KEY
    assert [(r.title, r.url, r.snippet) for r in out] == [
        ("A", "https://a.example", "摘要A"),
        ("B", "https://b.example", "摘要B"),
    ]


def test_brave形态_GET带头并解析webresults() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["params"] = dict(request.url.params)
        captured["token"] = request.headers.get("x-subscription-token")
        return httpx.Response(200, json={
            "web": {"results": [{"title": "R", "url": "https://r.example", "description": "描述"}]}
        })

    p = HttpSearchProvider("brave", api_key=_KEY, transport=httpx.MockTransport(handler),
                           resolver=_resolve_public)
    out = p.search("hello", top_k=3)
    assert captured["method"] == "GET"
    assert captured["params"] == {"q": "hello", "count": "3"}
    assert captured["token"] == _KEY
    assert out[0].title == "R" and out[0].snippet == "描述"


def test_网络错误返回空且不抛() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    p = HttpSearchProvider("custom", endpoint="https://s.example/api",
                           transport=httpx.MockTransport(handler), resolver=_resolve_public)
    assert p.search("x") == []


def test_非200返回空() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="err")

    p = HttpSearchProvider("custom", endpoint="https://s.example/api",
                           transport=httpx.MockTransport(handler), resolver=_resolve_public)
    assert p.search("x") == []


def test_端点被URL策略拒绝时不发请求() -> None:
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"results": []})

    # 内网端点 → 静态策略拒绝，绝不把 key 发向内网地址
    p = HttpSearchProvider("custom", api_key=_KEY, endpoint="http://127.0.0.1:8000/search",
                           transport=httpx.MockTransport(handler))
    assert p.search("x") == []
    assert called["n"] == 0, "被策略拒绝的端点不应发出任何请求"


def test_端点域名解析到内网时被拒绝且不发请求() -> None:
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"results": []})

    # 域名静态检查正常，但解析到内网 → 与抓取工具走同一条路径，必须拒绝
    p = HttpSearchProvider(
        "custom", api_key=_KEY, endpoint="https://search.evil.example/api",
        transport=httpx.MockTransport(handler),
        resolver=lambda h: ["10.0.0.9"],
    )
    assert p.search("x") == []
    assert called["n"] == 0, "解析到内网的端点不应发出任何请求"
