"""安全响应头：必须是"每个响应都带"，而不是"首页带了"。

为什么值得单独一个测试文件：安全头最常见的失效方式不是"忘了配"，而是**只在 happy path 配了**
——于是 404 探路响应、401 未认证响应、500 内部错误响应全裸奔，而攻击者恰恰就爱探这些路径。
所以这里逐条断言到**错误路径**上。

诚实边界（别把没测的说成测了）：
  · 这里验证的是"响应头确实发出去了"，**不是**"浏览器里 CSP 真的没挡住自己的资源"。
    后者需要真跑一次前端 e2e（本机没做），所以 `style-src` 才保守保留了 `'unsafe-inline'`。
  · HSTS **故意不设**：应用分不清自己是否在 TLS 后面（可能在反代后面），
    设错了要么无效要么把人锁在 HTTPS 上出不来——该由入口网关负责。

变异测试（`[tool.mutmut]` 圈定的三个模块之一）：本文件**没有等价变异**——mutmut 报存活
的每一条都是真缺口（说明那段逻辑没有被断言住），要么补测、要么写成注释说明为何等价。
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import Request, Response
from fastapi.responses import PlainTextResponse
from tests.conftest import ScriptedModel, weather_tool

from warden_agent.model.model import ChatResponse
from warden_agent.policy.policy import PolicyEngine
from warden_agent.store.sqlite import SqliteStore
from warden_agent.web.auth import TrustedCaller
from warden_agent.web.security_headers import SECURITY_HEADERS, SecurityHeadersMiddleware
from warden_agent.web.server import build_app


def _build_app() -> Any:
    """按仓库约定拼一个最小可用 app（用脚本模型，不碰真模型）。"""
    return build_app(
        model=ScriptedModel([ChatResponse(content="好", finish_reason="stop")]),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=SqliteStore(Path(tempfile.mkdtemp()) / "t.db"),
    )


@pytest.fixture()
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=_build_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _assert_headers(resp: httpx.Response) -> None:
    for name, value in SECURITY_HEADERS:
        assert resp.headers.get(name) == value, f"{name} 缺失或被改：{resp.headers.get(name)!r}"


async def test_正常响应带全套安全头(client: httpx.AsyncClient) -> None:
    resp = await client.get("/health/live")
    assert resp.status_code == 200
    _assert_headers(resp)


async def test_找不到路径也带安全头(client: httpx.AsyncClient) -> None:
    """404 是最常被扫的那类响应，绝不能裸奔。"""
    resp = await client.get("/definitely-not-a-route")
    assert resp.status_code == 404
    _assert_headers(resp)


async def test_认证拒绝响应也带安全头() -> None:
    """401/403 由**网关中间件**自己构造响应（不走端点）——这类响应最容易被安全头漏掉。"""
    app = build_app(
        model=ScriptedModel([ChatResponse(content="好", finish_reason="stop")]),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=SqliteStore(Path(tempfile.mkdtemp()) / "t.db"),
        api_keys={"k-alice": TrustedCaller("t", "user", "alice")},
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        # 不带密钥 → 401；带了但不够权限的路径 → 403（都能证明网关自建响应被覆盖）
        anonymous = await c.get("/runs")
        assert anonymous.status_code == 401, anonymous.status_code
        _assert_headers(anonymous)
        # 网关自建的 400（版本不受支持）也算这一类：不经端点直接返回
        bad_version = await c.get("/models", headers={"X-Warden-Api-Version": "9.0"})
        assert bad_version.status_code == 400, bad_version.status_code
        _assert_headers(bad_version)


async def test_未处理异常也不裸奔() -> None:
    """500 也必须带上（出错页面同样能被套框 / 被嗅探）。"""
    app = _build_app()

    @app.get("/boom-for-test")
    async def _boom() -> None:  # pragma: no cover - 只用来抛
        raise RuntimeError("boom")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/boom-for-test")
    assert resp.status_code == 500
    _assert_headers(resp)


async def test_重复的头会被去重而不是叠加(client: httpx.AsyncClient) -> None:
    """同名头若叠两份，浏览器对 CSP 是**取交集**——行为会变得难以推理，所以必须去重。"""
    resp = await client.get("/health/live")
    seen = [v for k, v in resp.headers.items() if k.lower() == "x-frame-options"]
    assert seen == ["DENY"]


def test_头不会被中间件加两遍() -> None:
    """build_app 只应挂一次；挂两次的话同名头会重复出现。"""
    app = _build_app()
    names = [m.cls for m in app.user_middleware]
    assert names.count(SecurityHeadersMiddleware) == 1


def test_websocket_与_lifespan_不受影响() -> None:
    """非 http 的 scope 直接放行——中间件不该因为多管闲事把 WS 拦成握手失败。

    这里断言的是**原样放行**：`receive`/`send` 必须是我们收到的那两个对象。
    改坏了会怎样（变异测试抓到的真实情形）：放行时把 `receive` 或 `send` 写成 `None`，
    WS/lifespan 会在应用内部报 TypeError——而那些路径上"看起来"什么都没发生。
    """
    seen: list[tuple[str, bool, bool]] = []
    recv = object()
    snd = object()

    async def _app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        seen.append((scope["type"], receive is recv, send is snd))

    middleware = SecurityHeadersMiddleware(_app)

    async def _drive() -> None:
        await middleware({"type": "lifespan"}, recv, snd)  # type: ignore[arg-type]

    asyncio.run(_drive())
    assert seen == [("lifespan", True, True)]


async def test_端点自己设的同名头会被换掉而不是叠加() -> None:
    """端点/依赖如果也设了安全头，必须**被我们这份覆盖**而不是两边共存。

    改坏了会怎样（变异测试抓到的真实情形）：去重时的 `k.lower() not in _HEADER_NAMES`
    写成 `k.upper()` 或 `in`——保留/剔除反过来以后，响应里会同时出现两份 CSP。
    浏览器的口径是同名头**取交集**，于是实际生效的策略比两边都紧，
    "为什么前端资源被挡"这类问题会变成无法推理的玄学。
    供应商/反代（它们常自己加 `x-frame-options`）也会造成这种双份。
    """
    app = _build_app()

    @app.get("/preset-for-test")
    async def _preset() -> Response:  # pragma: no cover - 只为造出"端点已设同名头"
        return Response(
            "ok",
            headers={
                "x-frame-options": "SAMEORIGIN",
                "content-security-policy": "default-src *",
            },
        )

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/preset-for-test")

    assert resp.status_code == 200
    assert resp.headers.get_list("x-frame-options") == ["DENY"]
    assert resp.headers.get("content-security-policy") == dict(SECURITY_HEADERS)[
        "Content-Security-Policy"
    ]
    _assert_headers(resp)


async def test_其它响应头不会被顺手丢掉(client: httpx.AsyncClient) -> None:
    """中间件只该动安全头，其他响应头（content-type 等）必须原样过去。

    **诚实说明**：这条钉不住 `message.get("headers", [])` 的默认值——Starlette 总会带上
    headers 键，默认值永远用不到（变异测试里那个变异体存活就是这个原因）。
    真正钉住默认值的是下一条 `test_响应消息缺headers键也不能崩`。
    """
    resp = await client.get("/health/live")
    assert resp.status_code == 200
    assert resp.headers.get("content-type", "").startswith("application/json")


async def test_响应消息缺headers键也不能崩() -> None:
    """响应消息**不带 headers 键**时，按"没有旧头"处理，而不是抛异常。

    为什么需要这条：`message.get("headers", [])` 的默认值只在"键不存在"时生效，
    而经 Starlette 走的正常路径永远带这个键——所以上面那条断言其实覆盖不到它。
    这里直接驱动 ASGI 中间件（它的职责就是"处理任意一条 `http.response.start`"）。
    改坏了会怎样（变异测试抓到的真实情形）：默认值写成 None → `for k, v in None`
    抛 TypeError → **整个响应 500**，而且栈在中间件里，排查方向会被带偏。
    """
    sent: list[dict[str, Any]] = []

    async def _app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 204})  # 故意不带 headers
        await send({"type": "http.response.body", "body": b""})

    async def _receive() -> dict[str, Any]:
        return {"type": "http.request"}

    async def _send(message: dict[str, Any]) -> None:
        sent.append(message)

    await SecurityHeadersMiddleware(_app)(
        {"type": "http", "method": "GET", "path": "/x", "headers": []}, _receive, _send
    )

    start = sent[0]
    assert start["status"] == 204
    got = {k: v for k, v in start["headers"]}
    for name, value in SECURITY_HEADERS:
        assert got[name.lower().encode()] == value.encode(), f"{name} 没补上"


async def test_请求体与响应体都原样透传() -> None:
    """中间件只拦 `http.response.start` 这条消息，请求体与响应体一律不碰。

    改坏了会怎样（变异测试抓到的真实情形）：把应用调用写成 `self.app(scope, None, send)`
    ——请求体读不出来，所有 POST（提交审批、发消息、上传）全变成 500。
    """
    app = _build_app()

    @app.post("/echo-for-test")
    async def _echo(request: Request) -> PlainTextResponse:  # pragma: no cover - 只为断言透传
        return PlainTextResponse(await request.body())

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post("/echo-for-test", content=b"hello-body")

    assert resp.status_code == 200, resp.text
    assert resp.text == "hello-body"
    _assert_headers(resp)


def test_csp_没有放开外部来源() -> None:
    """CSP 是"默认全禁再开口子"，所以这里盯的是**别多开门**（新增外域必须是有意识的决定）。"""
    csp = dict(SECURITY_HEADERS)["Content-Security-Policy"]
    assert "http://" not in csp and "https://" not in csp and "*" not in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "unsafe-eval" not in csp
