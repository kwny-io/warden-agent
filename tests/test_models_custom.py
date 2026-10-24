"""自建 / 私有部署模型接入的契约。

这个功能存在的理由只有一条：**有些地方不愿把对话发到公网大模型**。
所以这里要钉住的不是"能不能填个 URL"，而是几件一旦做错就会让用户
付出真实代价的事：

  1. 端点必须能落到库里、重启还在（否则配一次重启就没了，等于没有）；
  2. 端点必须**按用户隔离**——A 公司的内网地址不该出现在 B 的界面上；
  3. `configured` 要诚实：只有 key 没有地址 = 装不上，不能报"已配置"；
  4. 探针的缓存键要带上端点：换个地址就是另一台上游，沿用旧结论会骗人；
  5. `base_url` 的校验**有意放行内网与回环**——拦了就没有这个功能了。
     但 `file://` 这类非 http(s) 必须挡住。

另有第 6 条，是实测才发现的：自建端点**不许走系统代理**。Windows 的系统代理
不认 `ProxyOverride` 里的 `localhost;127.*;10.*;…`，httpx 的 `trust_env=True`
照样把回环/内网的请求塞给代理——"对话不出你的网络"这句话会当场变成假的。
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.conftest import ScriptedModel, weather_tool

from warden_agent.credential.broker import default_broker
from warden_agent.model.deepseek import ModelCallError, OpenAiCompatibleModel
from warden_agent.policy.policy import PolicyEngine
from warden_agent.store.sqlite import SqliteStore
from warden_agent.web.auth import TrustedCaller
from warden_agent.web.server import build_app


def _app(store: SqliteStore, key_material: str) -> Any:
    return build_app(
        model=ScriptedModel([]),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=store,
        credential_broker=default_broker({"WARDEN_CREDENTIAL_KEY": key_material}, vault=store),
    )


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture()
def key_material() -> str:
    return secrets.token_bytes(32).hex()


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "custom.db"


def _custom_row(client_models: list[dict[str, Any]]) -> dict[str, Any]:
    by_id = {m["id"]: m for m in client_models}
    assert "custom" in by_id, f"模型目录里应当有 custom：{sorted(by_id)}"
    return by_id["custom"]


@pytest.mark.asyncio
async def test_目录里有自建项_且标出它需要额外端点配置(db_path, key_material: str) -> None:
    app = _app(SqliteStore(db_path), key_material)
    async with _client(app) as c:
        rows = (await c.get("/models")).json()["models"]

    custom = _custom_row(rows)
    assert custom["needs_endpoint"] is True
    assert custom["needs_key"] is True
    # 没配过就是没配过：不许因为"目录里有这一项"就报已配置
    assert custom["configured"] is False
    assert custom["base_url"] is None and custom["model"] is None
    # 内置四家不该被动到
    others = [m for m in rows if m["id"] != "custom"]
    assert all(m["needs_endpoint"] is False for m in others)


@pytest.mark.asyncio
async def test_保存自建端点后重启仍在_且回显地址但不回显_key(
    db_path, key_material: str
) -> None:
    store = SqliteStore(db_path)
    app = _app(store, key_material)
    async with _client(app) as c:
        r = await c.post(
            "/models/select",
            json={
                "id": "custom",
                "api_key": "sk-local-placeholder",
                "base_url": "http://10.0.0.7:8000/v1",
                "model": "qwen2.5-7b-instruct",
            },
        )
        assert r.status_code == 200, r.text

    # 换一个进程（新 app、同一个库）：配置必须还在
    app2 = _app(SqliteStore(db_path), key_material)
    async with _client(app2) as c:
        custom = _custom_row((await c.get("/models")).json()["models"])

    assert custom["configured"] is True
    assert custom["base_url"] == "http://10.0.0.7:8000/v1"
    assert custom["model"] == "qwen2.5-7b-instruct"
    # key 永远不回显：整份响应里都不该出现它
    assert "sk-local-placeholder" not in str(custom)


@pytest.mark.asyncio
async def test_只填了_key_没填地址_不算已配置(db_path, key_material: str) -> None:
    """只有 key 没有地址等于装不上。若这里报 configured=true，界面会显示"已配置"，
    用户点切换才收到 400——那是把一次失败伪装成一次成功。"""
    app = _app(SqliteStore(db_path), key_material)
    async with _client(app) as c:
        r = await c.post(
            "/models/select", json={"id": "custom", "api_key": "sk-only-key"}
        )
        assert r.status_code == 400, r.text
        assert "端点地址" in r.json()["detail"]
        custom = _custom_row((await c.get("/models")).json()["models"])

    assert custom["configured"] is False


@pytest.mark.parametrize(
    "bad_url",
    [
        "file:///etc/passwd",  # 读本地文件
        "ftp://example.com/v1",  # 非 http(s)
        "http://",  # 没有主机名
        "http:///v1",  # 同上
        "not-a-url",  # 连 scheme 都没有
    ],
)
@pytest.mark.asyncio
async def test_非_http_地址必须被挡住(db_path, key_material: str, bad_url: str) -> None:
    app = _app(SqliteStore(db_path), key_material)
    async with _client(app) as c:
        r = await c.post(
            "/models/select",
            json={"id": "custom", "api_key": "k", "base_url": bad_url, "model": "m"},
        )
    assert r.status_code == 400, f"{bad_url} 应当被拒：{r.text}"


@pytest.mark.parametrize(
    "private_url",
    [
        "http://127.0.0.1:11434/v1",  # 本机 Ollama
        "http://localhost:8080/v1",
        "http://192.168.1.20:1234/v1",  # 公司内网 vLLM
        "http://10.0.0.7:8000/v1",
        "http://vllm.default.svc:8000/v1",  # K8s 服务名
        "https://gateway.internal/v1",  # 名字都解析不出来也要收
    ],
)
@pytest.mark.asyncio
async def test_内网与回环地址必须放行(
    db_path, key_material: str, private_url: str
) -> None:
    """这条不是"顺便测一下"：**这个功能的用户全是私有部署**。

    照 `rag/knowledge.py` 那条 SSRF 规则去拦私有地址，功能当场失效——
    而那正是用户唯一想要的用法。
    """
    app = _app(SqliteStore(db_path), key_material)
    async with _client(app) as c:
        r = await c.post(
            "/models/select",
            json={"id": "custom", "api_key": "k", "base_url": private_url, "model": "m"},
        )
    assert r.status_code == 200, f"{private_url} 不该被拒：{r.text}"


@pytest.mark.asyncio
async def test_探针用请求体里的候选端点_不用库里那份(
    db_path, key_material: str, monkeypatch
) -> None:
    """「先探通、再保存」：用户在地址栏里敲完就想知道通不通。"""
    seen: list[tuple[str, str]] = []

    def probe(self: OpenAiCompatibleModel, timeout: float = 5.0) -> None:
        seen.append((self._client.base_url.host or "", self.model))

    monkeypatch.setattr(OpenAiCompatibleModel, "probe", probe)
    app = _app(SqliteStore(db_path), key_material)
    async with _client(app) as c:
        await c.post(
            "/models/select",
            json={"id": "custom", "api_key": "k", "base_url": "http://10.0.0.1/v1", "model": "a"},
        )
        r = await c.post(
            "/models/probe",
            json={
                "id": "custom",
                "force": True,
                "base_url": "http://10.0.0.2/v1",
                "model": "b",
            },
        )
        assert r.status_code == 200, r.text

    assert seen == [("10.0.0.2", "b")], f"探针该用请求体的候选端点：{seen}"


@pytest.mark.asyncio
async def test_探针能用还没保存的密钥_且不把它写进库(
    db_path, key_material: str, monkeypatch
) -> None:
    """没保存过的模型一把已登记的 key 都没有。

    探针若只认库里那把，"先探通再保存"对需要密钥的模型就是死路——用户被迫先把一份
    可能写错的配置存进去，才允许验证它有没有写错。请求体带的 key 只用于这一次探测。
    """
    used: list[str | None] = []

    def probe(self: OpenAiCompatibleModel, timeout: float = 5.0) -> None:
        used.append(self._client.api_key)

    monkeypatch.setattr(OpenAiCompatibleModel, "probe", probe)
    store = SqliteStore(db_path)
    app = _app(store, key_material)
    async with _client(app) as c:
        r = await c.post(
            "/models/probe",
            json={
                "id": "custom",
                "force": True,
                "base_url": "http://10.0.0.9/v1",
                "model": "m",
                "api_key": "ephemeral-key",
            },
        )
        assert r.status_code == 200, r.text
        # 探针不许落库：库里不该因为这个请求多出一份自建配置
        listed = (await c.get("/models")).json()
        custom = next(m for m in listed["models"] if m["id"] == "custom")
        assert custom["configured"] is False, f"探针不该把临时密钥存下来：{custom}"
        assert custom["base_url"] is None, f"探针不该把临时端点存下来：{custom}"

    assert used == ["ephemeral-key"], f"探针该用请求体里那把临时 key：{used}"


@pytest.mark.asyncio
async def test_换了端点就不吃旧结论的缓存(
    db_path, key_material: str, monkeypatch
) -> None:
    """同一台"自建"换个地址就是另一台上游。沿用旧结论会实实在在地骗人：
    刚改对地址，界面还显示"探不通"（或反过来）。"""
    calls: list[str] = []

    def probe(self: OpenAiCompatibleModel, timeout: float = 5.0) -> None:
        calls.append(self._client.base_url.host or "")

    monkeypatch.setattr(OpenAiCompatibleModel, "probe", probe)
    app = _app(SqliteStore(db_path), key_material)
    async with _client(app) as c:
        await c.post(
            "/models/select",
            json={"id": "custom", "api_key": "k", "base_url": "http://a.internal/v1", "model": "m"},
        )
        first = (
            await c.post("/models/probe", json={"id": "custom", "base_url": "http://a.internal/v1"})
        ).json()
        # 同端点：命中缓存
        again = (
            await c.post("/models/probe", json={"id": "custom", "base_url": "http://a.internal/v1"})
        ).json()
        # 换端点：必须重探
        other = (
            await c.post("/models/probe", json={"id": "custom", "base_url": "http://b.internal/v1"})
        ).json()

    assert first["cached"] is False
    assert again["cached"] is True
    assert other["cached"] is False, "换了端点还吃缓存 = 拿 A 的结论回答 B"
    assert calls == ["a.internal", "b.internal"]


@pytest.mark.asyncio
async def test_自建探针探不通时原因要能分档(db_path, key_material: str, monkeypatch) -> None:
    """私有端点的典型故障是"忘了启用 OpenAI 兼容模式"（404）与"服务没起"（连不上），
    这两件事该给不同的话。"""

    def not_found(self: OpenAiCompatibleModel, timeout: float = 5.0) -> None:
        # 真 `probe()` 是 `raise ModelCallError(...) from e`，分档靠的是**原因链上的
        # status_code**，不是错误串措辞。这里必须照同样形状造，否则测的是个假体。
        class _NotFound(Exception):
            status_code = 404

        cause = _NotFound("404 page not found")
        raise ModelCallError(f"探测失败：{type(cause).__name__}: {cause}") from cause

    monkeypatch.setattr(OpenAiCompatibleModel, "probe", not_found)
    app = _app(SqliteStore(db_path), key_material)
    async with _client(app) as c:
        await c.post(
            "/models/select",
            json={"id": "custom", "api_key": "k", "base_url": "http://10.1.1.1/v1", "model": "m"},
        )
        body = (
            await c.post("/models/probe", json={"id": "custom", "force": True})
        ).json()

    assert body["ok"] is False
    assert body["code"] == "model_not_found"
    assert body["hint"], "分到档了就得给一句能照着做的话"


@pytest.mark.asyncio
async def test_端点按用户隔离_看不到也用不上别人的(db_path, key_material: str) -> None:
    """A 公司配的内网地址不该出现在 B 的界面上——这是"每用户自己配"的全部含义。"""
    store = SqliteStore(db_path)
    app = build_app(
        model=ScriptedModel([]),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=store,
        api_keys={
            "k-alice": TrustedCaller("tenant-a", "user", "alice"),
            "k-bob": TrustedCaller("tenant-a", "user", "bob"),
        },
        credential_broker=default_broker({"WARDEN_CREDENTIAL_KEY": key_material}, vault=store),
    )
    async with _client(app) as c:
        hdr_a = {"Authorization": "Bearer k-alice"}
        hdr_b = {"Authorization": "Bearer k-bob"}
        r = await c.post(
            "/models/select",
            headers=hdr_a,
            json={
                "id": "custom",
                "api_key": "sk-alice",
                "base_url": "http://alice-internal/v1",
                "model": "alice-model",
            },
        )
        assert r.status_code == 200, r.text

        alice = _custom_row((await c.get("/models", headers=hdr_a)).json()["models"])
        bob = _custom_row((await c.get("/models", headers=hdr_b)).json()["models"])

    assert alice["base_url"] == "http://alice-internal/v1"
    assert bob["base_url"] is None, "别人的端点不该漏到我的界面上"
    assert bob["configured"] is False, "别人的 key 也不该算进我的配置"


@pytest.mark.asyncio
async def test_切到自建要真的换掉会话用的模型(db_path, key_material: str) -> None:
    app = _app(SqliteStore(db_path), key_material)
    async with _client(app) as c:
        r = await c.post(
            "/models/select",
            json={
                "id": "custom",
                "api_key": "k",
                "base_url": "http://10.0.0.9/v1",
                "model": "llama-3.1-8b",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["current"] == "custom"
        assert (await c.get("/models")).json()["current"] == "custom"


@pytest.mark.asyncio
async def test_非管理员不能把自建端点设成部署默认_且失败不留副作用(
    db_path, key_material: str
) -> None:
    """403 必须在**写库之前**。否则请求失败了，配置却已经存进去了。"""
    store = SqliteStore(db_path)
    app = build_app(
        model=ScriptedModel([]),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=store,
        api_keys={"k-bob": TrustedCaller("tenant-a", "user", "bob")},
        credential_broker=default_broker({"WARDEN_CREDENTIAL_KEY": key_material}, vault=store),
    )
    async with _client(app) as c:
        hdr = {"Authorization": "Bearer k-bob"}
        r = await c.post(
            "/models/select",
            headers=hdr,
            json={
                "id": "custom",
                "api_key": "sk-bob",
                "base_url": "http://bob-internal/v1",
                "model": "m",
                "scope": "deployment",
            },
        )
        assert r.status_code == 403, r.text
        custom = _custom_row((await c.get("/models", headers=hdr)).json()["models"])

    assert custom["configured"] is False, "403 之后不许悄悄留下已保存的配置"
    assert custom["base_url"] is None


@pytest.mark.asyncio
async def test_自建端点不许走系统代理(db_path, key_material: str, monkeypatch) -> None:
    """「对话不出你的网络」这句话，会被系统代理当场变成假的。

    实测现场（Windows）：注册表里 `ProxyServer=127.0.0.1:7897`，而
    `ProxyOverride` 写着 `localhost;127.*;10.*;…`。但 httpx 的 `trust_env=True`
    只认环境变量 / `urllib.getproxies()`，**不认 ProxyOverride 的语义**——
    于是 `http://127.0.0.1:8061/v1` 的请求被送给代理，回来一个 502。
    用户看到的是"我本机的端点怎么都探不通"，实际是他的对话被转去公网代理了。

    所以自建这一路必须 `trust_env=False`；内置公网厂商保留默认（在代理后面访问
    api.deepseek.com 正是需要它）。
    """
    # 起一个真的本地 OpenAI 兼容端点（只回 `GET /models`，探针就调这一个）。
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 —— http.server 的命名约定
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"object": "list", "data": []}).encode())

        def log_message(self, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    # 设一个坏代理：任何 trust_env=True 的请求都会经它、然后失败。
    # 端点用回环地址 —— 正是「本机部署的小模型」这个用法。
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    try:
        app = _app(SqliteStore(db_path), key_material)
        async with _client(app) as c:
            r = await c.post(
                "/models/probe",
                json={
                    "id": "custom",
                    "force": True,
                    "base_url": f"http://127.0.0.1:{port}/v1",
                    "model": "m",
                    "api_key": "not-needed",
                },
            )
            assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True, (
            "回环端点被塞进系统代理了（httpx 不认 Windows ProxyOverride 里的 127.*）："
            f"{body.get('reason')}"
        )
    finally:
        server.shutdown()
        server.server_close()  # shutdown() 只停 serve_forever，不关监听 socket
