"""凭证接线测试：模型 API Key 由凭证 broker 加密保管、日志脱敏器生效。

改之前：`CredentialBroker` / `SecretRedactor` 全仓零引用（只在自身测试里出现），
运行中的服务从不经过它们——README 却把"凭证 AES-GCM 加密 + 脱敏"列为防御能力。
这里锁住接上之后的行为：
  - `/models/select` 导入的 key 进 broker（加密存储 + 租约），不再明文躺 dict；
  - `/models` 的 configured 状态由 broker 决定；
  - 通过 broker 取回 key 可直接切换模型（不需要重复导入）；
  - 脱敏器能把密钥从任意文本里抹掉，并挂在 app 上供复用。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import httpx
import pytest
from tests.conftest import ScriptedModel, weather_tool

from warden_agent.credential.broker import CredentialBroker, SecretRedactor, default_broker
from warden_agent.credential.crypto import CredentialCipher
from warden_agent.model.model import ChatResponse
from warden_agent.policy.policy import PolicyEngine
from warden_agent.store.sqlite import SqliteStore
from warden_agent.web.server import build_app

SECRET = "sk-" + "wiretest" * 3  # 拼接构造的假密钥，不含真实凭据


def _app(**kw):
    return build_app(
        model=ScriptedModel([ChatResponse(content="好", finish_reason="stop")]),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=SqliteStore(Path(tempfile.mkdtemp()) / "t.db"),
        **kw,
    )


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


# ---------- broker 装配 ----------


def test_配置了密钥材料时用它() -> None:
    broker = default_broker({"WARDEN_CREDENTIAL_KEY": "deployment-material"})
    assert isinstance(broker, CredentialBroker)


def test_未配置密钥材料时退化为进程内临时密钥且不报错() -> None:
    """不应因为少一个环境变量就起不来——但要告警（这里只验证不抛错）。"""
    broker = default_broker({})
    broker.register("x", {"api_key": SECRET})
    assert broker.has("x")


def test_相同明文两次注册密文不同() -> None:
    broker = CredentialBroker(CredentialCipher(b"wiring-key-000016"))
    broker.register("a", {"api_key": SECRET})
    broker.register("b", {"api_key": SECRET})
    a = broker.encrypted_fields("a")
    b = broker.encrypted_fields("b")
    assert a is not None and b is not None
    assert a["api_key"] != b["api_key"]


# ---------- 模型 Key 走 broker ----------


@pytest.mark.asyncio
async def test_导入的key加密保管_明文不入库() -> None:
    broker = default_broker({"WARDEN_CREDENTIAL_KEY": "unit-test-material"})
    app = _app(credential_broker=broker)
    async with _client(app) as c:
        r = await c.post("/models/select", json={"id": "deepseek", "api_key": SECRET})
        assert r.status_code == 200
    # broker 内部必须是密文
    assert broker.has("model:deepseek")
    fields = broker.encrypted_fields("model:deepseek")
    assert fields is not None and SECRET not in fields["api_key"]


@pytest.mark.asyncio
async def test_已导入的key可复用_无需重复导入() -> None:
    broker = default_broker({"WARDEN_CREDENTIAL_KEY": "unit-test-material"})
    app = _app(credential_broker=broker)
    async with _client(app) as c:
        await c.post("/models/select", json={"id": "deepseek", "api_key": SECRET})
        await c.post("/models/select", json={"id": "fake"})  # 切走
        # 再切回 deepseek 且不带 key：应从 broker 取回
        r = await c.post("/models/select", json={"id": "deepseek"})
        assert r.status_code == 200
        assert r.json()["current"] == "deepseek"


@pytest.mark.asyncio
async def test_未导入key时切换需要密钥的模型_报400() -> None:
    broker = default_broker({"WARDEN_CREDENTIAL_KEY": "unit-test-material"})
    app = _app(credential_broker=broker)
    async with _client(app) as c:
        r = await c.post("/models/select", json={"id": "deepseek"})
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_models的configured由broker决定() -> None:
    broker = default_broker({"WARDEN_CREDENTIAL_KEY": "unit-test-material"})
    app = _app(credential_broker=broker)
    async with _client(app) as c:
        before = {m["id"]: m["configured"] for m in (await c.get("/models")).json()["models"]}
        assert before["deepseek"] is False  # 没导入 → 未配置
        await c.post("/models/select", json={"id": "deepseek", "api_key": SECRET})
        after = {m["id"]: m["configured"] for m in (await c.get("/models")).json()["models"]}
        assert after["deepseek"] is True


@pytest.mark.asyncio
async def test_启动时的key也登记进broker() -> None:
    broker = default_broker({"WARDEN_CREDENTIAL_KEY": "unit-test-material"})
    app = _app(
        credential_broker=broker, model_id="deepseek", model_api_key=SECRET
    )
    async with _client(app) as c:
        models = {m["id"]: m["configured"] for m in (await c.get("/models")).json()["models"]}
        assert models["deepseek"] is True
    assert broker.has("model:deepseek")
    fields = broker.encrypted_fields("model:deepseek")
    assert fields is not None and SECRET not in fields["api_key"]


# ---------- 脱敏器 ----------


@pytest.mark.asyncio
async def test_脱敏器挂到app且抹掉密钥() -> None:
    broker = default_broker({"WARDEN_CREDENTIAL_KEY": "unit-test-material"})
    app = _app(credential_broker=broker)
    async with _client(app) as c:
        await c.post("/models/select", json={"id": "deepseek", "api_key": SECRET})
    redactor: SecretRedactor = app.state.secret_redactor
    text = f"调用失败 api_key={SECRET} 401"
    assert SECRET not in redactor.redact(text)
    assert "******" in redactor.redact(text)


def test_脱敏器空密钥不误伤() -> None:
    r = SecretRedactor()
    r.add("")  # 空串不能把整段文本替换没
    assert r.redact("正常文本") == "正常文本"
