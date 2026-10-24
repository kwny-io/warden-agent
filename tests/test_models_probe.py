"""模型探测接口的契约：`/models` 只敢说「有 key」，`/models/probe` 才敢说「通不通」。

为什么需要这组判据（笔记 95 的标题缺陷）：
provider 宕机 / 网络不通 / base_url 写错 / key 被吊销 —— 这四种在「有没有 key」这件事上
长得**一模一样**，而旧接口只回答了后者，界面于是把「有 key」显示成「可用」，
用户发出第一条消息才被告知挂了。这里钉住三件事：

  1. 没 key 就别假装探过：400，且说清要先导入 Key；
  2. 探通 ⇒ `ok=true`；探不通 ⇒ `ok=false` **并把原因原样带出来**（不是一句「失败」）；
  3. 结果缓存，且 `force=true` 能绕过缓存 —— 否则界面 5 秒轮询会把厂商打爆。
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
    return tmp_path / "probe.db"


@pytest.mark.asyncio
async def test_没导入_key_时探针不假装探过(db_path, key_material: str) -> None:
    app = _app(SqliteStore(db_path), key_material)
    async with _client(app) as c:
        r = await c.post("/models/probe", json={"id": "deepseek"})
        assert r.status_code == 400, r.text
        assert "需要先导入 API Key" in r.json()["detail"]


@pytest.mark.asyncio
async def test_探通与探不通都要把话说清楚(db_path, key_material: str, monkeypatch) -> None:
    calls: list[str] = []

    def ok_probe(self: OpenAiCompatibleModel, timeout: float = 5.0) -> None:
        calls.append("ok")

    def bad_probe(self: OpenAiCompatibleModel, timeout: float = 5.0) -> None:
        calls.append("bad")
        raise ModelCallError("探测失败：APIConnectionError: 连不上 api.deepseek.com")

    app = _app(SqliteStore(db_path), key_material)
    async with _client(app) as c:
        assert (
            await c.post("/models/select", json={"id": "deepseek", "api_key": "sk-test-123"})
        ).status_code == 200

        monkeypatch.setattr(OpenAiCompatibleModel, "probe", ok_probe)
        r = await c.post("/models/probe", json={"id": "deepseek", "force": True})
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True and r.json()["reason"] is None

        monkeypatch.setattr(OpenAiCompatibleModel, "probe", bad_probe)
        r = await c.post("/models/probe", json={"id": "deepseek", "force": True})
        body = r.json()
        assert r.status_code == 200, r.text
        assert body["ok"] is False
        # 原因必须原样带出：只说「失败」等于让用户去猜是网络、key 还是 base_url
        assert "APIConnectionError" in body["reason"]
        assert calls == ["ok", "bad"]


@pytest.mark.asyncio
async def test_探测结果会缓存_force_才重探(db_path, key_material: str, monkeypatch) -> None:
    calls: list[int] = []

    def probe(self: OpenAiCompatibleModel, timeout: float = 5.0) -> None:
        calls.append(1)

    monkeypatch.setattr(OpenAiCompatibleModel, "probe", probe)
    app = _app(SqliteStore(db_path), key_material)
    async with _client(app) as c:
        await c.post("/models/select", json={"id": "deepseek", "api_key": "sk-test-123"})

        first = (await c.post("/models/probe", json={"id": "deepseek"})).json()
        second = (await c.post("/models/probe", json={"id": "deepseek"})).json()
        third = (await c.post("/models/probe", json={"id": "deepseek", "force": True})).json()

    assert len(calls) == 2, f"缓存没生效：探了 {len(calls)} 次"
    assert first["cached"] is False
    assert second["cached"] is True
    assert third["cached"] is False


@pytest.mark.asyncio
async def test_探测结论会进入模型列表_但只作为参考(db_path, key_material: str, monkeypatch) -> None:
    """`configured`（有 key）与 `last_probe`（通不通）必须分开报，不许合并成一个「可用」。"""

    def revoked(self: OpenAiCompatibleModel, timeout: float = 5.0) -> None:
        raise ModelCallError("探测失败：401 Unauthorized")

    monkeypatch.setattr(OpenAiCompatibleModel, "probe", revoked)
    app = _app(SqliteStore(db_path), key_material)
    async with _client(app) as c:
        await c.post("/models/select", json={"id": "deepseek", "api_key": "sk-revoked"})
        await c.post("/models/probe", json={"id": "deepseek", "force": True})
        models = {m["id"]: m for m in (await c.get("/models")).json()["models"]}

    deepseek = models["deepseek"]
    assert deepseek["configured"] is True, "有 key 就该报 configured"
    assert deepseek["last_probe"]["ok"] is False, (
        "探不通必须报出来，不能只因为 configured 就显示可用"
    )
    assert "401" in deepseek["last_probe"]["reason"]
    assert models["fake"]["last_probe"] is None, "没探过的模型不许编一个结论出来"


# ---------------------------------------------------------------------------
# 上面四条都用 monkeypatch 把 `probe()` 整个换掉了 —— 也就是**真实现从未被验过**。
# 下面两条才钉住真实现的两句承诺：「只调 GET /models（不烧 token）」与「单次尝试（不重试）」。


def _scripted_client(
    model: OpenAiCompatibleModel, handler: Any, *, max_retries: int = 0
) -> list[str]:
    """把模型内部的 openai 客户端换成一个只回固定响应的假传输，返回请求轨迹。

    `max_retries` 必须跟着**模型构造时那个值**走：假客户端偷惂成 0 的话，
    「`probe()` 里那个 `max_retries=0` 到底有没有起作用」就永远测不出来（假牙）。
    """
    from openai import OpenAI

    seen: list[str] = []

    def _record(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        return handler(request)

    model._client = OpenAI(  # noqa: SLF001 —— 测试要替换的就是这个私有客户端
        api_key="sk-test-123",
        base_url="http://upstream.test/v1",
        max_retries=max_retries,
        http_client=httpx.Client(transport=httpx.MockTransport(_record)),
    )
    return seen


def test_真探针只发_GET_models_且不碰对话端点() -> None:
    """承诺「只调 GET /models，不消耗 token」——那就必须能看到**请求轨迹**，而不是相信注释。"""
    model = OpenAiCompatibleModel(api_key="sk-test-123", base_url="http://upstream.test/v1")
    seen = _scripted_client(
        model, lambda req: httpx.Response(200, json={"object": "list", "data": []})
    )

    model.probe()

    assert seen == ["GET /v1/models"], f"探针打的不是模型目录：{seen}"
    assert not [s for s in seen if "chat" in s], (
        "探针碰了对话端点 ⇒ 会烧 token/占配额，与承诺相反"
    )


def test_真探针失败只尝试一次_不重试() -> None:
    """探不通要**当场**报错。若沿用客户端的 max_retries=2，界面会为一次探测转 3 轮超时。"""
    model = OpenAiCompatibleModel(
        api_key="sk-test-123", base_url="http://upstream.test/v1", max_retries=2
    )
    seen = _scripted_client(model, lambda req: httpx.Response(401, json={"error": "revoked"}))

    with pytest.raises(ModelCallError) as err:
        model.probe(timeout=0.1)

    assert len(seen) == 1, f"探针重试了：{seen}"
    assert "401" in str(err.value)


@pytest.mark.asyncio
async def test_真探针失败只尝试一次_不重试_反向对照() -> None:
    """上面那条用 401 测不出「少写 max_retries=0」——**401 本来就不重试**（假牙）。

    换一个 SDK 真会重试的状态码（500）才测得出来：`max_retries=0` 缺了就会打 3 次。
    """
    model = OpenAiCompatibleModel(
        api_key="sk-test-123", base_url="http://upstream.test/v1", max_retries=2
    )
    # 假客户端也要保留 2 次重试，否则两种写法都是「只打一次」，变异测不出来
    seen = _scripted_client(
        model, lambda req: httpx.Response(500, json={"error": "boom"}), max_retries=2
    )

    with pytest.raises(ModelCallError):
        model.probe(timeout=0.1)

    assert len(seen) == 1, f"探针重试了 {len(seen)} 次：{seen}"


@pytest.mark.asyncio
async def test_探测原因里不许带出自己的_key(db_path, key_material: str, monkeypatch) -> None:
    """进程级 redactor 只登记**经由 /models/select 导入**的 key 与部署级 key。

    key 从别处进 broker（启动时从 vault 恢复、运维直接 register）时 redactor 并不知道它，
    而 openai/httpx 的某些异常会把 `Authorization: Bearer ...` 拼进消息 ⇒ 明文跑进浏览器。
    这条用「直接 register 进 broker」造出那个现场（走 /models/select 反而测不出来）。
    """
    from warden_agent.credential.vault import DEPLOYMENT_SCOPE
    from warden_agent.web.http_kit import _model_key_name

    secret = "sk-user-only-1f2e3d4c5b6a"

    def leaky(self: OpenAiCompatibleModel, timeout: float = 5.0) -> None:
        raise ModelCallError(
            f"ConnectionError: headers={{'Authorization': 'Bearer {secret}'}} not reachable"
        )

    monkeypatch.setattr(OpenAiCompatibleModel, "probe", leaky)
    store = SqliteStore(db_path)
    broker = default_broker({"WARDEN_CREDENTIAL_KEY": key_material}, vault=store)
    # 绕过 /models/select：redactor 从未见过这把 key（= 启动时从 vault 恢复的情形）
    broker.register(_model_key_name("deepseek"), {"api_key": secret}, DEPLOYMENT_SCOPE)
    app = build_app(
        model=ScriptedModel([]),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=store,
        credential_broker=broker,
    )
    async with _client(app) as c:
        body = (await c.post("/models/probe", json={"id": "deepseek", "force": True})).json()

    assert body["ok"] is False
    assert secret not in body["reason"], f"密钥漏进了探测原因：{body['reason']}"
    assert "ConnectionError" in body["reason"], "抹密钥不能把有用信息一起抹了"


# ---------------------------------------------------------------------------
# 探不通要**分档**：换 Key / 去充值 / 改地址 / 等一等，是四件不同的事
# （2026-10-19：我自己就因为「余额不足」与「Key 无效」在界面上长得一样而反复换 Key）


@pytest.mark.parametrize(
    ("status", "body", "expect"),
    [
        (401, {"error": "invalid api key"}, "key_rejected"),
        (403, {"error": "forbidden"}, "key_rejected"),
        (402, {"error": "quota_exceeded: 余额不足"}, "quota_exhausted"),
        (404, {"error": "model not found"}, "model_not_found"),
        (429, {"error": "slow down"}, "rate_limited"),
        (500, {"error": "boom"}, "upstream_error"),
        (503, {"error": "unavailable"}, "upstream_error"),
    ],
)
def test_真异常要分对档(status: int, body: dict, expect: str) -> None:
    """用**真实 openai 异常**（MockTransport 造 HTTP 响应）验分档，不用假异常对象。

    为什么不用假异常：假异常只能证明我的 if-else 认识我自己造的类名，
    而真异常是 SDK 在真实响应上抛出来的——两者的类名/status_code 是否对得上，
    只有这条能证明。
    """
    from warden_agent.model.deepseek import ModelCallError, probe_failure_code

    model = OpenAiCompatibleModel(
        api_key="sk-test-123", base_url="http://upstream.test/v1", max_retries=0
    )
    _scripted_client(model, lambda req: httpx.Response(status, json=body))

    with pytest.raises(ModelCallError) as caught:
        model.probe(timeout=0.1)
    assert probe_failure_code(caught.value) == expect


def test_连不上与超时要分得开() -> None:
    """`APIConnectionError`（DNS/拒绝连接）≠ `APITimeoutError`：前者查网络，后者可能只是慢。"""
    from warden_agent.model.deepseek import ModelCallError, probe_failure_code

    def _raise(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nodename nor servname provided", request=req)

    model = OpenAiCompatibleModel(
        api_key="sk-test-123", base_url="http://upstream.test/v1", max_retries=0
    )
    _scripted_client(model, _raise)
    with pytest.raises(ModelCallError) as caught:
        model.probe(timeout=0.1)
    assert probe_failure_code(caught.value) == "unreachable"

    def _slow(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=req)

    model2 = OpenAiCompatibleModel(
        api_key="sk-test-123", base_url="http://upstream.test/v1", max_retries=0
    )
    _scripted_client(model2, _slow)
    with pytest.raises(ModelCallError) as caught2:
        model2.probe(timeout=0.1)
    assert probe_failure_code(caught2.value) == "timeout"
