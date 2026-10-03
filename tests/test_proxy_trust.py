"""反向代理信任边界：`WARDEN_TRUSTED_PROXIES` 与 `request.client.host` 的真实含义。

## 这里要钉住的是什么

`request.client.host` 在下面两处被当成「调用者」用：

- `web/ratelimit.py` 的 `client_key(user_id, client_host)` —— 匿名时用 `ip:<host>` 分桶；
- `web/server.py` 的幂等作用域 —— 匿名时 `ip:<host>:<Idempotency-Key>` 是缓存命名空间。

它其实是 **TCP 对端**。放在 Ingress/Nginx 后面时，对端永远是代理自己，于是：

1. 限流**塌成一个桶**：两个真实客户端共用一个配额，一个能把另一个挤成 429；
2. 幂等命名空间相撞：两个真实客户端用同一个 `Idempotency-Key` 时，
   **后到的那个直接读到前一个的缓存响应体**（实测见下）。

第 2 条是**跨调用者读取响应体**，比"限流不准"严重得多，所以它必须有测试盯着。

修法不是"无条件信 XFF"（那等于谁都能伪造来源 IP，把限流彻底绕掉），而是
**只信受信对端带来的 XFF**：配 `WARDEN_TRUSTED_PROXIES` → 透传成
uvicorn 的 `forwarded_allow_ips` → 由 `ProxyHeadersMiddleware` 按
"XFF 里最右边那个不可信地址"改写 `scope["client"]`。

## 两边的断言都要有

- **不配**：XFF 被忽略（塔成一个桶）——这是**刻意保留的安全默认**，
  若哪天有人把默认改成信任 XFF，这条测试会红；
- **配了**：按真实客户端分隔，且**只有受信对端**的 XFF 被采信。

测试里用 `httpx.ASGITransport(client=(ip, port))` 扮演"TCP 对端是代理"，
再手写 `X-Forwarded-For` 扮演"真实客户端"——这正是在反代后面的真实数据流。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import httpx
import pytest
from tests.conftest import ScriptedModel, weather_tool
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from warden_agent.core.settings import validate_env
from warden_agent.model.model import ChatResponse
from warden_agent.policy.policy import PolicyEngine
from warden_agent.store.sqlite import SqliteStore
from warden_agent.web.ratelimit import RateLimiter
from warden_agent.web.run_server import trusted_proxies_from_env
from warden_agent.web.server import build_app

PROXY_IP = "10.0.0.5"       # 前置代理（TCP 对端）
OTHER_IP = "192.0.2.7"      # 另一个对端：**在受信网段之外**（TEST-NET-1）
CLIENT_A = "203.0.113.11"   # 真实客户端 A
CLIENT_B = "203.0.113.22"   # 真实客户端 B
TRUSTED = "10.0.0.0/8"      # 只信集群内网段里的代理


# ---------- 配置面：解析与启动校验 ----------


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, None),                                        # 没配
        ({"WARDEN_TRUSTED_PROXIES": ""}, None),            # 空串 = 没配
        ({"WARDEN_TRUSTED_PROXIES": "   "}, None),         # 全空白 = 没配
        ({"WARDEN_TRUSTED_PROXIES": " 10.0.0.0/8 "}, "10.0.0.0/8"),  # 两侧空白会被去掉
        ({"WARDEN_TRUSTED_PROXIES": "10.0.0.1,10.0.0.2"}, "10.0.0.1,10.0.0.2"),
    ],
)
def test_解析受信代理(env: dict[str, str], expected: str | None) -> None:
    assert trusted_proxies_from_env(env) == expected


@pytest.mark.parametrize(
    "value",
    ["10.0.0.0/8", "172.17.0.1", "::1", "10.0.0.1,192.168.0.0/16", "fd00::/8"],
)
def test_启动校验放行合法取值(value: str) -> None:
    assert validate_env({"WARDEN_TRUSTED_PROXIES": value}) == []


@pytest.mark.parametrize("value", ["*", "0.0.0.0/0", "::/0", "10.0.0.0/8,*"])
def test_启动校验拒绝全信任值(value: str) -> None:
    """`*` / `/0` 等于"谁都能伪造来源 IP"，在启动时就要拦下来（fail-closed）。

    断言**文案**：只说"启动失败"的话，把 `_proxies_problem` 退化成"永远返回 None"
    也照样绿——而那时 `*` 会被 uvicorn 的 `_TrustedHosts.always_trust` 全盘接受。
    """
    errors = validate_env({"WARDEN_TRUSTED_PROXIES": value})
    assert len(errors) == 1
    assert "信任所有对端" in errors[0]
    assert "WARDEN_TRUSTED_PROXIES" in errors[0]


@pytest.mark.parametrize(
    "value", ["10.0.0.0/33", "999.1.1.1", "10.0.0.0/8,,172.17.0.1", "10.0.0.0/8,"]
)
def test_启动校验拒绝非法取值(value: str) -> None:
    """非法值必须报错，因为 uvicorn 自己**静默**把解析不了的东西当普通字面量。

    于是"网段写错一位"的表现是"XFF 忽然不生效"（退回塔桶），极难排查。
    """
    errors = validate_env({"WARDEN_TRUSTED_PROXIES": value})
    assert len(errors) == 1
    assert "WARDEN_TRUSTED_PROXIES" in errors[0]


# ---------- 行为面：限流分桶 ----------


def _app(**kw):
    return build_app(
        model=ScriptedModel(
            [
                ChatResponse(content="回答一", finish_reason="stop"),
                ChatResponse(content="回答二", finish_reason="stop"),
            ]
        ),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=SqliteStore(Path(tempfile.mkdtemp()) / "t.db"),
        **kw,
    )


def _client(app, peer: str = PROXY_IP):
    """对端固定为 `peer`（模拟"请求都从代理进来"），真实客户端用 XFF 表达。"""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(peer, 1234)),
        base_url="http://test",
    )


def _trusting(app, trusted: str = TRUSTED):
    """把 uvicorn 在 `forwarded_allow_ips=...` 时自动加的中间件手写挂上。"""
    return ProxyHeadersMiddleware(app, trusted_hosts=trusted)


@pytest.mark.asyncio
async def test_不配受信代理_限流塔成一个桶() -> None:
    """安全默认（不信任 XFF）：两个真实客户端共用一个桶 → 第二个被挤成 429。

    这是**刻意保留**的行为：不配 `WARDEN_TRUSTED_PROXIES` 时谁都可以往
    `X-Forwarded-For` 里随便填一个 IP 来刷新自己的配额，比"限流不准"更糟。
    若有人把默认改成无条件信任 XFF，这条会红。
    """
    app = _app(rate_limiter=RateLimiter(1, 60))  # 每桶 1 次/分钟
    async with _client(app) as c:
        a = await c.get("/status/a", headers={"X-Forwarded-For": CLIENT_A})
        b = await c.get("/status/b", headers={"X-Forwarded-For": CLIENT_B})
    assert (a.status_code, b.status_code) == (200, 429)
    assert b.json()["errorCode"] == "RATE_LIMITED"


@pytest.mark.asyncio
async def test_配了受信代理_按真实客户端分桶() -> None:
    """配了受信代理：两个真实客户端各拿一个桶，且**同一个**客户端仍会被限住。

    只断言"都是 200"是不够的——把限流器整个关掉也能过。第三个请求来自
    真实客户端 A（第二个请求已经用掉了它的额度），必须还是 429。
    """
    app = _trusting(_app(rate_limiter=RateLimiter(1, 60)))
    async with _client(app) as c:
        a = await c.get("/status/a", headers={"X-Forwarded-For": CLIENT_A})
        b = await c.get("/status/b", headers={"X-Forwarded-For": CLIENT_B})
        a2 = await c.get("/status/c", headers={"X-Forwarded-For": CLIENT_A})
    assert (a.status_code, b.status_code) == (200, 200)
    assert a2.status_code == 429


@pytest.mark.asyncio
async def test_不受信对端自带的XFF被忽略() -> None:
    """对端不在受信名单里时，它自带的 XFF 一律不采信（按对端 IP 分桶）。

    这条盯的是"信任的是**对端**，不是请求头"：写反了的表现是任何客户端
    自填一个 XFF 就能换一个桶（限流形同虚设）。
    """
    app = _trusting(_app(rate_limiter=RateLimiter(1, 60)))
    async with _client(app, peer=OTHER_IP) as c:
        a = await c.get("/status/a", headers={"X-Forwarded-For": CLIENT_A})
        b = await c.get("/status/b", headers={"X-Forwarded-For": CLIENT_B})
    assert (a.status_code, b.status_code) == (200, 429)


# ---------- 行为面：幂等命名空间（跨调用者读响应体） ----------


async def _same_key_pair(app) -> tuple[str, str]:
    """两个**不同**的真实客户端，对同一个 run 用同一个 Idempotency-Key。"""
    body = {"text": "你好"}
    headers_a = {"Idempotency-Key": "same-key", "X-Forwarded-For": CLIENT_A}
    headers_b = {"Idempotency-Key": "same-key", "X-Forwarded-For": CLIENT_B}
    async with _client(app) as c:
        a = await c.post("/chat/run-proxy", json=body, headers=headers_a)
        b = await c.post("/chat/run-proxy", json=body, headers=headers_b)
    return a.json()["text"], b.json()["text"]


@pytest.mark.asyncio
async def test_不配受信代理_幂等缓存被另一个客户端读到() -> None:
    """安全默认下的**跨调用者读取**：B 拿到的是 A 的缓存响应体。

    这是把"匿名作用域 = 对端 IP"放在反代后面的真实后果，不是理论推演：
    A 执行拿"回答一"，B 用同一个 key 打过来时**没有执行**，直接回了 A 的那份。
    修复（配 `WARDEN_TRUSTED_PROXIES`）后两者才会各自执行。
    """
    a_text, b_text = await _same_key_pair(_app())
    assert a_text == b_text == "回答一"


@pytest.mark.asyncio
async def test_配了受信代理_幂等键互不串() -> None:
    app = _trusting(_app())
    a_text, b_text = await _same_key_pair(app)
    assert (a_text, b_text) == ("回答一", "回答二")


# ---------- 第五十三批：占位符会让这层「静默不成立」 ----------


@pytest.mark.asyncio
async def test_占位符会让受信代理静默失效_所以必须fail_closed() -> None:
    """`WARDEN_TRUSTED_PROXIES=REPLACE_ME-pod-cidr`（`deploy/k8s/configmap.yaml` 的原值）
    会让「已配受信代理」这件事**静默不成立**。

    机制（真 uvicorn 实现，本文件用的是 `ProxyHeadersMiddleware` 本身）：`_TrustedHosts`
    对解析不出 IP 的项会当作**字面量**（它自己的注释就写着：因为非法 IP 一律变字面量，
    “用户无法知道自己写错了”）；而 "REPLACE_ME-pod-cidr" 永远不会等于任何对端地址
    ⇒ XFF 一律不采信、`scope["client"]` 仍是 TCP 对端 ⇒ 第三十九批修好的两件事
    （限流按调用者分桶 / 幂等键不串）在**照着参考部署上线**时又退回去了。

    所以这一层在第五十三批改成 fail-closed：占位符在启动校验里就直接判错
    （`core/settings.py::_PLACEHOLDER_RE`——与变量类型无关，因为“占位符”是
    “部署模板忘了替换”的专用判据，只修 proxies 一个变量等于留下一个漏的）。

    两个断言都必须有：
      1) 对照：同一个 XFF 在**真值** `10.0.0.0/8` 下会被采信（两个客户端各自一个桶）；
      2) 占位符下又塔成一个桶——即“日志说配了、机制没生效”本身是 rebuttable 的事实，
         不是描述；若哪天 uvicorn 变成“非法字面量 = 信任所有人”，第 2 条会红
         （那比今天的 bug 更危险，必须被拦下来）。
    """
    placeholder = "REPLACE_ME-pod-cidr"

    # (1) 真值：两个真实客户端各自一个桶
    good = _trusting(_app(rate_limiter=RateLimiter(1, 60)), trusted=TRUSTED)
    async with _client(good) as c:
        a = await c.get("/status/a", headers={"X-Forwarded-For": CLIENT_A})
        b = await c.get("/status/b", headers={"X-Forwarded-For": CLIENT_B})
    assert (a.status_code, b.status_code) == (200, 200)

    # (2) 占位符：又变成一个桶（第二个被挤成 429）
    bad = _trusting(_app(rate_limiter=RateLimiter(1, 60)), trusted=placeholder)
    async with _client(bad) as c:
        a = await c.get("/status/a", headers={"X-Forwarded-For": CLIENT_A})
        b = await c.get("/status/b", headers={"X-Forwarded-For": CLIENT_B})
    assert (a.status_code, b.status_code) == (200, 429), (
        "占位符下两个真实客户端又共用一个限流桶：说明「已配受信代理」这句话是假的"
    )

    # (3) 而它现在过不了启动校验 ⇒ 这句假话下回根本走不到运行期
    assert validate_env({"WARDEN_TRUSTED_PROXIES": placeholder}), (
        "占位符能穿过 validate_env 的话，这个测试证明的静默失效就会在生产上重现"
    )
