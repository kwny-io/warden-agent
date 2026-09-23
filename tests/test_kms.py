"""密钥托管（KMS/HSM 信封加密）：provider 抽象与两条真实接入路径。

测试用**桩替身**（假的 KMS client / 假的 Vault HTTP 响应），因此全程离线、确定：
验证的是"接入形状与逻辑"——解开被包装的 DEK、历史密钥兜底、配置错误要报错而非静默回落。
真实云 KMS 的联调需要凭据，仓库里不做（见模块与运维手册的说明）。
"""

from __future__ import annotations

import base64

import httpx
import pytest

from warden_agent.credential.broker import default_broker
from warden_agent.credential.crypto import InvalidToken
from warden_agent.credential.kms import (
    AwsKmsKeyProvider,
    EnvKeyProvider,
    KeyProviderError,
    StaticKeyProvider,
    VaultTransitKeyProvider,
    resolve_key_provider,
)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


# ---------- provider 基础 ----------


def test_static_provider_原样返回材料() -> None:
    p = StaticKeyProvider(b"k1", [b"old1", b"old2"])
    assert p.current_key() == b"k1"
    assert p.historical_keys() == [b"old1", b"old2"]


def test_env_provider_读环境变量并解析历史密钥() -> None:
    p = EnvKeyProvider({"WARDEN_CREDENTIAL_KEY": "main", "WARDEN_CREDENTIAL_OLD_KEYS": " a , b "})
    assert p.current_key() == b"main"
    assert p.historical_keys() == [b"a", b"b"]


def test_env_provider_缺主密钥时报错() -> None:
    with pytest.raises(KeyProviderError):
        EnvKeyProvider({}).current_key()


# ---------- provider 选择 ----------


def test_未配置时回落到env模式() -> None:
    assert resolve_key_provider({}) is None
    assert resolve_key_provider({"WARDEN_KMS_PROVIDER": "env"}) is None


def test_未知provider直接报错不静默回落() -> None:
    # 关键：写错了要炸，不能"以为在托管、其实没托管"
    with pytest.raises(KeyProviderError):
        resolve_key_provider({"WARDEN_KMS_PROVIDER": "awskms"})


# ---------- AWS KMS ----------


class _FakeKms:
    """假的 KMS 客户端：把 blob 当明文直接返回（模拟解密成功）。"""

    def __init__(self, mapping: dict[bytes, bytes] | None = None, fail: bool = False) -> None:
        self._mapping = mapping or {}
        self._fail = fail

    def decrypt(self, CiphertextBlob: bytes) -> dict[str, bytes]:  # noqa: N803 - 对齐 boto3 参数名
        if self._fail:
            raise RuntimeError("KMS 拒绝（AccessDenied）")
        return {"Plaintext": self._mapping.get(CiphertextBlob, b"")}


def test_awskms_解开当前与历史密钥() -> None:
    p = AwsKmsKeyProvider(
        kms_client=_FakeKms({b"wrapped": b"dek-material", b"wrapped-old": b"old-dek"}),
        wrapped_key=b"wrapped",
        wrapped_old_keys=[b"wrapped-old"],
    )
    assert p.current_key() == b"dek-material"
    assert p.historical_keys() == [b"old-dek"]


def test_awskms_解包失败转成托管错误() -> None:
    p = AwsKmsKeyProvider(kms_client=_FakeKms(fail=True), wrapped_key=b"x")
    with pytest.raises(KeyProviderError):
        p.current_key()


def test_awskms_缺配置时报错() -> None:
    with pytest.raises(KeyProviderError):
        AwsKmsKeyProvider.from_env({})


def test_awskms_配置非法base64时报错() -> None:
    with pytest.raises(KeyProviderError):
        AwsKmsKeyProvider.from_env({"WARDEN_KMS_WRAPPED_KEY": "not base64!!"})


# ---------- Vault transit ----------


def _vault_client(dek: bytes, status: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, text="permission denied")
        return httpx.Response(200, json={"data": {"plaintext": _b64(dek)}})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_vault_解开密钥() -> None:
    p = VaultTransitKeyProvider(
        client=_vault_client(b"vault-dek"),
        addr="http://vault:8200",
        key_name="warden",
        wrapped_key="vault:v1:abc",
    )
    assert p.current_key() == b"vault-dek"


def test_vault_被拒时转成托管错误() -> None:
    p = VaultTransitKeyProvider(
        client=_vault_client(b"", status=403),
        addr="http://vault:8200",
        key_name="warden",
        wrapped_key="vault:v1:abc",
    )
    with pytest.raises(KeyProviderError) as e:
        p.current_key()
    # 错误消息只带状态码，不得回显 Vault 响应体（响应体可能含敏感信息）
    assert "403" in str(e.value)
    assert "permission denied" not in str(e.value)


def test_vault_缺配置时列出缺了哪些() -> None:
    with pytest.raises(KeyProviderError) as e:
        VaultTransitKeyProvider.from_env({"WARDEN_VAULT_ADDR": "http://v"})
    assert "WARDEN_VAULT_TOKEN" in str(e.value)


# ---------- 与 broker 的接线 ----------


def _issue_and_get(broker, name: str) -> str:  # type: ignore[no-untyped-def]
    broker.register(name, {"api_key": "s3cret"})
    lease = broker.issue(name)
    return lease.value.fields["api_key"]


def test_broker_用托管provider加解密闭环() -> None:
    broker = default_broker(env={}, key_provider=StaticKeyProvider(b"dek-1-material-0001"))
    assert _issue_and_get(broker, "openai") == "s3cret"


def test_broker_历史密钥能解开旧密文_实现轮换兜底() -> None:
    """轮换场景：旧密文用旧密钥加密；新 provider 提供了历史密钥 → 仍能解开。"""
    from warden_agent.credential.vault import InMemoryCredentialVault

    vault = InMemoryCredentialVault()  # 两个 broker 共用同一份密文存储
    old = default_broker(
        env={}, key_provider=StaticKeyProvider(b"dek-old-material-000"), vault=vault)
    old.register("openai", {"api_key": "legacy"})
    stored = old.encrypted_fields("openai")
    assert stored is not None

    rotated = default_broker(
        env={},
        key_provider=StaticKeyProvider(
            b"dek-new-material-000", old_materials=[b"dek-old-material-000"]),
        vault=vault,
    )
    # 新 broker 用新 DEK，但靠历史密钥仍能读旧密文
    assert rotated.issue("openai").value.fields["api_key"] == "legacy"


def test_broker_没有历史密钥时旧密文解不开() -> None:
    from warden_agent.credential.vault import InMemoryCredentialVault

    vault = InMemoryCredentialVault()
    old = default_broker(
        env={}, key_provider=StaticKeyProvider(b"dek-old-material-000"), vault=vault)
    old.register("openai", {"api_key": "legacy"})
    fresh = default_broker(
        env={}, key_provider=StaticKeyProvider(b"dek-only-new-000000"), vault=vault)
    with pytest.raises(InvalidToken):
        fresh.issue("openai")
