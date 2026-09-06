"""凭证能力测试：AES-GCM 加密、短生命周期租约、Secret 脱敏。"""
from __future__ import annotations

import datetime as dt

import pytest

from warden_agent.credential.broker import CredentialBroker, SecretRedactor
from warden_agent.credential.crypto import CredentialCipher


@pytest.fixture
def cipher() -> CredentialCipher:
    return CredentialCipher(b"test-key-material-not-hardcoded-in-prod")


def test_加密后库里看不到明文(cipher: CredentialCipher) -> None:
    """库里存的必须是密文，绝不能是明文 api key。"""
    # 纯测试数据：拼接构造的假密钥，不含任何真实凭据
    dummy_key = "sk-" + "unit-test" * 3
    broker = CredentialBroker(cipher)
    broker.register("openai", {"api_key": dummy_key})

    stored = broker._secrets["openai"]["api_key"]  # 直接看"库底"
    assert dummy_key not in stored
    # 且是真正的加密：同一明文两次入库结果不同（带随机 nonce）
    broker.register("openai2", {"api_key": dummy_key})
    assert stored != broker._secrets["openai2"]["api_key"]


def test_租约拿回明文且可解密(cipher: CredentialCipher) -> None:
    broker = CredentialBroker(cipher)
    broker.register("openai", {"api_key": "sk-abc", "org": "org-1"})
    lease = broker.issue("openai")
    assert lease.value.fields["api_key"] == "sk-abc"
    assert broker.verify(lease)


def test_租约过期自动失效(cipher: CredentialCipher) -> None:
    # 用固定"时钟"控制时间，便于精确测试过期
    now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    broker = CredentialBroker(cipher, ttl_seconds=60, now=now)
    broker.register("openai", {"api_key": "sk-abc"})
    lease = broker.issue("openai", ttl_seconds=60)

    # 61 秒后：租约过期，verify 为 False，get 为 None
    broker._clock = now + dt.timedelta(seconds=61)
    assert lease.is_expired
    assert not broker.verify(lease)
    assert broker.get(lease.lease_id) is None


def test_租约可主动回收(cipher: CredentialCipher) -> None:
    broker = CredentialBroker(cipher)
    broker.register("openai", {"api_key": "sk-abc"})
    lease = broker.issue("openai")
    broker.revoke(lease.lease_id)
    assert not broker.verify(lease)


def test_未注册凭证发租约报错(cipher: CredentialCipher) -> None:
    broker = CredentialBroker(cipher)
    with pytest.raises(KeyError):
        broker.issue("no-such")


def test_SecretRedactor_抹掉密钥() -> None:
    redactor = SecretRedactor(["sk-live-top-secret"])
    text = "调用完成 api_key=sk-live-top-secret 200 OK"
    assert "sk-live-top-secret" not in redactor.redact(text)
    assert "******" in redactor.redact(text)


def test_篡改密文解密失败(cipher: CredentialCipher) -> None:
    """GCM 完整性：改一个字节的密文，解密必须抛错，不能解出坏数据继续用。"""
    broker = CredentialBroker(cipher)
    broker.register("openai", {"api_key": "sk-abc"})
    stored = broker._secrets["openai"]["api_key"]
    # 取中间字符替换为"必定不同"的字符：nonce 随机，若固定改首字符为 "A"，
    # 原首字符恰好是 "A" 时密文未变，解密不会抛错（概率 1/64，CI 曾踩中）。
    mid = len(stored) // 2
    replacement = "B" if stored[mid] != "B" else "C"
    tampered = stored[:mid] + replacement + stored[mid + 1:]
    assert tampered != stored
    with pytest.raises(Exception):
        cipher.decrypt(tampered)
