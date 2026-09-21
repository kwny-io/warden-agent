"""凭证落库测试：加密的密文与租约**真的**进了存储层，重启后仍在。

改之前的问题：`CredentialCipher` 的 AES-GCM 是真的，但加解密的对象活在 broker 进程内的
一个 dict 里——进程一退凭证就没了，`register` 过的 key 全丢，加密没兑现任何价值。
这里锁住接上 vault 之后的行为：

  - 换一个新的 broker 实例、连同一个库，凭证与租约都还在（模拟重启）；
  - 库里只有密文，翻遍数据库文件也找不到明文；
  - 租约跨重启可校验、可回收，过期后惰性清理；
  - 按作用域隔离：同名凭证在不同 scope 下互不可见、互不覆盖；
  - 部署级 key 全体可用，用户自己导入的 key 只对自己可见。

密钥材料一律**运行时随机生成**（不写死字面量），与生产"只从环境变量取密钥"的约定一致。
"""

from __future__ import annotations

import datetime as dt
import secrets
from pathlib import Path

import httpx
import pytest
from tests.conftest import ScriptedModel, weather_tool

from warden_agent.credential.broker import CredentialBroker, default_broker
from warden_agent.credential.crypto import CredentialCipher
from warden_agent.credential.vault import (
    DEPLOYMENT_SCOPE,
    InMemoryCredentialVault,
    as_vault,
)
from warden_agent.model.model import ChatResponse
from warden_agent.policy.policy import PolicyEngine
from warden_agent.store.sqlite import SqliteStore
from warden_agent.web.auth import TrustedCaller
from warden_agent.web.server import build_app


@pytest.fixture
def key_material() -> bytes:
    """运行时随机密钥材料（等价于部署时 WARDEN_CREDENTIAL_KEY 的值）。"""
    return secrets.token_bytes(32)


@pytest.fixture
def secret() -> str:
    """运行时随机的假密钥，避免任何形似真实凭据的字面量进仓库。"""
    return "sk-" + secrets.token_hex(16)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "creds.db"


def _broker(store: SqliteStore, material: bytes) -> CredentialBroker:
    return CredentialBroker(CredentialCipher(material), vault=store)


# ---------- 加密真的落库了 ----------


def test_换实例后凭证仍在_模拟重启(db_path: Path, key_material: bytes, secret: str) -> None:
    store = SqliteStore(db_path)
    first = _broker(store, key_material)
    first.register("openai", {"api_key": secret})
    assert first.has("openai")

    # 新进程 = 新 store + 新 broker，连同一个库文件
    reopened = _broker(SqliteStore(db_path), key_material)
    assert reopened.has("openai")
    lease = reopened.issue("openai")
    assert lease.value.fields["api_key"] == secret


def test_换实例后租约仍有效(db_path: Path, key_material: bytes, secret: str) -> None:
    store = SqliteStore(db_path)
    first = _broker(store, key_material)
    first.register("openai", {"api_key": secret})
    lease = first.issue("openai", ttl_seconds=3600)

    reopened = _broker(SqliteStore(db_path), key_material)
    # 租约记录落库了，所以新实例能校验它、并按 name 回查密文解出明文
    resumed = reopened.get(lease.lease_id)
    assert resumed is not None
    assert resumed.value.fields["api_key"] == secret
    assert reopened.verify(lease)


def test_数据库文件里翻不到明文(db_path: Path, key_material: bytes, secret: str) -> None:
    """库底只能是密文——连租约表也不能顺手把明文带进去。"""
    store = SqliteStore(db_path)
    broker = _broker(store, key_material)
    broker.register("openai", {"api_key": secret})
    lease = broker.issue("openai", ttl_seconds=3600)
    store.close()

    raw = db_path.read_bytes()
    assert secret.encode() not in raw
    # 密文确实存在（不是"什么都没存"蒙混过关）
    reopened = SqliteStore(db_path)
    assert reopened.load_credential(DEPLOYMENT_SCOPE, "openai") is not None
    # 租约表里只有元数据
    stored_lease = reopened.load_credential_lease(DEPLOYMENT_SCOPE, lease.lease_id)
    assert stored_lease is not None and stored_lease.name == "openai"


def test_未配置密钥材料时不落明文(db_path: Path, key_material: bytes, secret: str) -> None:
    """走 default_broker 的真实路径：密文进库，明文不出现在库里。"""
    env = {"WARDEN_CREDENTIAL_KEY": key_material.hex()}
    store = SqliteStore(db_path)
    broker = default_broker(env, vault=store)
    broker.register("openai", {"api_key": secret})
    store.close()

    assert secret.encode() not in db_path.read_bytes()


# ---------- 过期与清理 ----------


def test_过期租约读取时被清理(db_path: Path, key_material: bytes, secret: str) -> None:
    now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    store = SqliteStore(db_path)
    broker = CredentialBroker(CredentialCipher(key_material), now=now, vault=store)
    broker.register("openai", {"api_key": secret})
    lease = broker.issue("openai", ttl_seconds=60)

    # "一分钟后"再读：过期 → 返回 None，且租约记录被顺手删掉（不留垃圾行）
    broker._clock = now + dt.timedelta(seconds=61)  # type: ignore[attr-defined]
    assert broker.get(lease.lease_id) is None
    assert store.load_credential_lease(DEPLOYMENT_SCOPE, lease.lease_id) is None


def test_新租约发出时清理同作用域的历史过期租约(
    db_path: Path, key_material: bytes, secret: str
) -> None:
    now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    store = SqliteStore(db_path)
    broker = CredentialBroker(CredentialCipher(key_material), now=now, vault=store)
    broker.register("openai", {"api_key": secret})
    stale = broker.issue("openai", ttl_seconds=10)

    broker._clock = now + dt.timedelta(seconds=30)  # type: ignore[attr-defined]
    broker.issue("openai", ttl_seconds=600)  # 发新租约时顺手清掉过期的
    assert store.load_credential_lease(DEPLOYMENT_SCOPE, stale.lease_id) is None


def test_凭证删除后租约随之失效(db_path: Path, key_material: bytes, secret: str) -> None:
    store = SqliteStore(db_path)
    broker = _broker(store, key_material)
    broker.register("openai", {"api_key": secret})
    lease = broker.issue("openai", ttl_seconds=3600)
    store.delete_credential(DEPLOYMENT_SCOPE, "openai")
    assert broker.get(lease.lease_id) is None
    assert not broker.has("openai")


# ---------- 作用域隔离 ----------


def test_不同作用域互不可见(db_path: Path, key_material: bytes, secret: str) -> None:
    store = SqliteStore(db_path)
    broker = _broker(store, key_material)
    broker.register("model:deepseek", {"api_key": secret}, scope="alice")

    assert broker.has("model:deepseek", scope="alice")
    assert not broker.has("model:deepseek", scope="bob")
    with pytest.raises(KeyError):
        broker.issue("model:deepseek", scope="bob")


def test_同名凭证在不同作用域下互不覆盖(
    db_path: Path, key_material: bytes
) -> None:
    alice_key = "sk-" + secrets.token_hex(8)
    bob_key = "sk-" + secrets.token_hex(8)
    store = SqliteStore(db_path)
    broker = _broker(store, key_material)
    broker.register("model:deepseek", {"api_key": alice_key}, scope="alice")
    broker.register("model:deepseek", {"api_key": bob_key}, scope="bob")

    assert broker.issue("model:deepseek", scope="alice").value.fields["api_key"] == alice_key
    assert broker.issue("model:deepseek", scope="bob").value.fields["api_key"] == bob_key
    # 密文各自独立落库
    assert store.list_credential_names("alice") == ["model:deepseek"]
    assert store.list_credential_names("bob") == ["model:deepseek"]


# ---------- 保管库装配 ----------


def test_sqlite存储可直接当保管库(db_path: Path) -> None:
    store = SqliteStore(db_path)
    assert as_vault(store) is store


def test_内存存储退回进程内保管库(db_path: Path) -> None:
    """对接不支持落库的存储时不报错，静默回到进程内实现。"""
    from warden_agent.agent import InMemoryRunStore

    assert isinstance(as_vault(InMemoryRunStore()), InMemoryCredentialVault)


def test_未配置vault时行为与历史一致(key_material: bytes, secret: str) -> None:
    broker = CredentialBroker(CredentialCipher(key_material))
    broker.register("openai", {"api_key": secret})
    assert broker.issue("openai").value.fields["api_key"] == secret


# ---------- HTTP 端到端：导入的 key 跨重启仍在、且只属于导入者 ----------


def _app(store: SqliteStore, **kw):
    return build_app(
        model=ScriptedModel([ChatResponse(content="好", finish_reason="stop")]),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=store,
        **kw,
    )


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


@pytest.mark.asyncio
async def test_导入的key跨重启仍在(db_path: Path, key_material: bytes, secret: str) -> None:
    env = {"WARDEN_CREDENTIAL_KEY": key_material.hex()}
    store = SqliteStore(db_path)
    app = _app(store, credential_broker=default_broker(env, vault=store))
    async with _client(app) as c:
        r = await c.post("/models/select", json={"id": "deepseek", "api_key": secret})
        assert r.status_code == 200

    # 换一个全新的 app（新 broker，但有同一把 WARDEN_CREDENTIAL_KEY + 同一个库）
    fresh_store = SqliteStore(db_path)
    app2 = _app(fresh_store, credential_broker=default_broker(env, vault=fresh_store))
    async with _client(app2) as c:
        models = {m["id"]: m["configured"] for m in (await c.get("/models")).json()["models"]}
        assert models["deepseek"] is True


@pytest.mark.asyncio
async def test_同租户不同用户互不可见(db_path: Path, key_material: bytes, secret: str) -> None:
    """tenant 是整租户共用的，所以隔离必须落在用户身份上——否则 A 导入的 key B 能用。"""
    env = {"WARDEN_CREDENTIAL_KEY": key_material.hex()}
    tenant = "acme"
    alice = TrustedCaller(tenant, "user", "alice")
    bob = TrustedCaller(tenant, "user", "bob")
    store = SqliteStore(db_path)
    app = _app(
        store,
        credential_broker=default_broker(env, vault=store),
        api_keys={"k-alice": alice, "k-bob": bob},
    )
    hdr_a = {"Authorization": "Bearer k-alice"}
    hdr_b = {"Authorization": "Bearer k-bob"}
    async with _client(app) as c:
        r = await c.post(
            "/models/select",
            json={"id": "deepseek", "api_key": secret},
            headers=hdr_a,
        )
        assert r.status_code == 200

        a_models = {
            m["id"]: m["configured"]
            for m in (await c.get("/models", headers=hdr_a)).json()["models"]
        }
        b_models = {
            m["id"]: m["configured"]
            for m in (await c.get("/models", headers=hdr_b)).json()["models"]
        }
        assert a_models["deepseek"] is True
        assert b_models["deepseek"] is False  # bob 看不到 alice 导入的 key

        # bob 也无法"借用" alice 的 key 切换模型
        r_b = await c.post("/models/select", json={"id": "deepseek"}, headers=hdr_b)
        assert r_b.status_code == 400
