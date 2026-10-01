"""统一安全响应头（CSP / 防嵌套 / 防嗅探）。

**为什么单独一个模块**：这三类头是"整套服务级策略"，散在各个端点里必然漂移
（改一个忘一个），而它们守的是三个很具体的攻击面：

1. **密钥外带**：控制台要渲染模型生成的 Markdown。
   一旦出现 XSS，`localStorage.getItem("warden.apiKey")` 就是一行代码的事。
   `Content-Security-Policy` 把脚本来源限制成同源 + 禁掉 `eval`，把"注入一段脚本"
   从"有可能"压成"要先攻破打包产物"。**注意**：CSP 是纵深防御，不是密钥可以随便放的借口
   ——前端那边也已改成**默认只存 `sessionStorage`**，要跨重启保留得在界面上显式勾"记住"
   （见 `web/src/lib/api.ts` 与 `web/src/App.tsx`）。
2. **点击劫持**：审批动作是安全敏感操作（点一下就等于批准一个高危工具）。
   若页面能被 iframe 套住，攻击者用透明层诱导点击即可；`frame-ancestors 'none'`
   + 老浏览器看的 `X-Frame-Options: DENY` 一起堵。
3. **类型嗅探**：接口返回 `application/json`、审计与 Markdown 里含用户可控文本，
   旧浏览器可能把它嗅探成 HTML 执行；`X-Content-Type-Options: nosniff` 禁止嗅探。

**实测口径（诚实说明，别把未验证的当已生效）**：
  · 用 `TestClient` 断言响应头在 200/404/500/SSE 上都存在（`tests/test_security_headers.py`）；
  · **但"浏览器里 CSP 真的没挡到自己的资源"这件事，本机没有跑浏览器 e2e 验证**——
    所以 `style-src` 才保守地留了 `'unsafe-inline'`（控制台有几处 `style={{width}}`
    做分栏拖拽，CSP 会连**行内 style 属性**一起拦），并且**没有**上
    `require-trusted-types-for`/`strict-dynamic` 这类强策略（会给自己找麻烦）。
  · HSTS 只在 HTTPS 下才有意义，而"是否在 TLS 后面"不是应用能可靠判断的（可能在反代后面），
    所以**故意不在这里设**：要设请设在入口网关/Ingress。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

#: 响应头（顺序固定，便于测试与比对）
SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    (
        "Content-Security-Policy",
        # 默认全禁，再逐项开口子（比反过来"禁几个"安全得多）
        "default-src 'self'; "
        # 脚本只允许同源打包产物；不给自己留 eval
        "script-src 'self'; "
        # 行内 style 属性在 CSP 口径下也算 inline，控制台的分栏拖拽用了它 → 必须放行
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self' data:; "
        # SSE 与 REST 都是同源；不放开任何外部域
        "connect-src 'self'; "
        # 插件/内嵌对象一律不许（Flash 时代的 XSS 载体，留着只有风险没有收益）
        "object-src 'none'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        # 不允许被任何页面套框（点击劫持）
        "frame-ancestors 'none'",
    ),
    # 老浏览器不认 frame-ancestors，这条是它们的兜底（两条都留，不冲突）
    ("X-Frame-Options", "DENY"),
    ("X-Content-Type-Options", "nosniff"),
    # 同源工具站不需要把 URL 泄漏给第三方；同时避免 Referer 里带上带 query 的密钥类参数
    ("Referrer-Policy", "no-referrer"),
    # 这些能力一个都用不上，显式关掉（不关的话第三方 iframe 场景才有意义，我们副本就不许被套）
    (
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    ),
)

_HEADER_NAMES = frozenset(name.lower().encode() for name, _ in SECURITY_HEADERS)
_ENCODED_HEADERS: tuple[tuple[bytes, bytes], ...] = tuple(
    (name.lower().encode(), value.encode()) for name, value in SECURITY_HEADERS
)


class SecurityHeadersMiddleware:
    """纯 ASGI 中间件：只改响应头，**不碰响应体**。

    为什么不用 `BaseHTTPMiddleware`：它要包一层 body 迭代器，而本服务的关键路径是
    **SSE 长连接流式输出**——多套一层就是把"流式"交给框架的缓冲实现去赌。
    这里只拦 `http.response.start` 这条消息，把头部补齐后原样放行，流仍然是流
    （`tests/test_security_headers.py` 里对 SSE 有断言）。
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope["type"] != "http":  # websocket / lifespan 不掺和
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                # 同名头先摘掉再补：避免"端点也设了一份"时出现重复头
                # （重复的 CSP 在浏览器里是**取交集**，会让行为难以推理）
                kept = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() not in _HEADER_NAMES
                ]
                message["headers"] = kept + list(_ENCODED_HEADERS)
            await send(message)

        await self.app(scope, receive, send_with_headers)


def install(app: Any) -> None:
    """把中间件挂到 FastAPI 应用上。

    **调用位置很重要**：要挂在网关中间件**之后**（Starlette 里后加的在外层），
    这样错误响应（404 / 未认证 401 / 500 的 problem+json）也带上这些头——
    出错的那一页同样是会被套框、会被嗅探的页面。
    """
    app.add_middleware(SecurityHeadersMiddleware)
