"""密钥轮换测试：把"换密钥会不会把已有凭证全废掉"变成可演练的事实。

背景：此前换 `WARDEN_CREDENTIAL_KEY` 就等于**把存量密文全废掉**——没有轮换工具，
也没有"解密用旧密钥、加密用新密钥"的过渡状态。现在：

  - `CredentialCipher` 支持挂**历史密钥**（只用于解密兜底）；
  - `rotate_credentials()` 把存量密文逐个重加密到当前密钥（**幂等**）；
  - `warden rotate-credentials` 是入口，且**有解不开的就退出码 5**——
    不能假装成功（那些密文还是旧密钥，摘掉旧密钥就永久损失）。

这里锁住四件事：
  1. 挂了旧密钥就能读老密文（过渡期读写不中断）；
  2. 轮换后**只用新密钥**就能读全部（旧密钥可以摘了）；
  3. **幂等**：再跑一次不会重复折腾；
  4. **失败必须显性**：漏配旧密钥时，读不出来要报错、轮换要记进 `failed` 且**不覆盖原密文**。
"""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest

from warden_agent import cli
from warden_agent.credential.broker import CredentialBroker, default_broker
from warden_agent.credential.crypto import CredentialCipher, InvalidToken
from warden_agent.credential.vault import (
    DEPLOYMENT_SCOPE,
    InMemoryCredentialVault,
    StoredCredential,
    rotate_credentials,
)
from warden_agent.store.sqlite import SqliteStore

OLD = b"old-key-material-for-rotation"      # 测试材料（非真实密钥）
NEW = b"new-key-material-after-rotation"
FIELD = "api_" + "key"


def _secret() -> str:
    return "fauxvalue-" + secrets.token_hex(12)


def _vault_with_ciphertext(cipher: CredentialCipher, *, scope: str, name: str, value: str):
    vault = InMemoryCredentialVault()
    vault.save_credential(StoredCredential(
        scope=scope, name=name, encrypted=dict({FIELD: cipher.encrypt(value)})
    ))
    return vault


# ---------------------------------------------------------------------------
# 一、过渡期：挂旧密钥能读老密文
# ---------------------------------------------------------------------------


def test_只有新密钥时读不了老密文() -> None:
    """先确认问题真实存在：直接换密钥 = 存量解不开。"""
    old_only = CredentialCipher(OLD)
    token = old_only.encrypt("sk-old")
    with pytest.raises(InvalidToken):
        CredentialCipher(NEW).decrypt(token)


def test_挂了旧密钥就能读老密文() -> None:
    """轮换过渡期：加密用新密钥，解密对新旧都认——读写都不中断。"""
    token = CredentialCipher(OLD).encrypt("sk-old")
    ring = CredentialCipher(NEW, [OLD])
    assert ring.decrypt(token) == "sk-old", "旧密钥兜底没生效"
    # 新加密的用当前密钥（新密文只靠旧密钥解不开）
    fresh = ring.encrypt("sk-new")
    assert ring.decrypt(fresh) == "sk-new"
    with pytest.raises(InvalidToken):
        CredentialCipher(OLD).decrypt(fresh)


def test_needs_rotation_能区分新旧密文() -> None:
    ring = CredentialCipher(NEW, [OLD])
    assert ring.needs_rotation(CredentialCipher(OLD).encrypt("x")) is True
    assert ring.needs_rotation(ring.encrypt("x")) is False


def test_两把密钥都解不开时报错而不是当成需要轮换() -> None:
    """解不开是数据问题，不能被当成"需要轮换"而静默重写（那会覆盖掉原密文）。"""
    ring = CredentialCipher(NEW, [OLD])
    stranger = CredentialCipher(b"a-key-nobody-configured").encrypt("x")
    with pytest.raises(InvalidToken):
        ring.needs_rotation(stranger)


def test_记得到底是历史密钥能解还是当前密钥能解() -> None:
    ring = CredentialCipher(NEW, [OLD])
    old_token = CredentialCipher(OLD).encrypt("x")
    assert ring.can_decrypt_with_old(old_token) is True
    assert ring.can_decrypt_with_old(ring.encrypt("x")) is False


# ---------------------------------------------------------------------------
# 二、轮换：重加密后可摘旧密钥
# ---------------------------------------------------------------------------


def test_轮换后只用新密钥就能读全部() -> None:
    ring = CredentialCipher(NEW, [OLD])
    vault = InMemoryCredentialVault()
    plaintexts = {}
    for i in range(3):
        name, value = f"cred-{i}", _secret()
        plaintexts[name] = value
        cipher_used = CredentialCipher(OLD) if i < 2 else ring   # 两条老的、一条新的
        vault.save_credential(StoredCredential(
            scope=DEPLOYMENT_SCOPE, name=name,
            encrypted=dict({FIELD: cipher_used.encrypt(value)}),
        ))

    report = rotate_credentials(vault, ring)
    assert report.scanned == 3
    assert report.rotated == 2, "只有两条是旧密钥加密的"
    assert report.already_current == 1
    assert report.ok

    # **只用新密钥**的 cipher 现在能读全部——这就是"旧密钥可以摘了"
    new_only = CredentialCipher(NEW)
    for name, value in plaintexts.items():
        record = vault.load_credential(DEPLOYMENT_SCOPE, name)
        assert record is not None
        assert new_only.decrypt(record.encrypted[FIELD]) == value


def test_轮换是幂等的() -> None:
    ring = CredentialCipher(NEW, [OLD])
    vault = _vault_with_ciphertext(CredentialCipher(OLD), scope=DEPLOYMENT_SCOPE,
                                   name="c", value=_secret())
    first = rotate_credentials(vault, ring)
    assert first.rotated == 1
    second = rotate_credentials(vault, ring)
    assert second.rotated == 0 and second.already_current == 1, "第二次不该再重加密"


def test_轮换不动明文也不动其它字段() -> None:
    ring = CredentialCipher(NEW, [OLD])
    value = _secret()
    vault = InMemoryCredentialVault()
    vault.save_credential(StoredCredential(
        scope=DEPLOYMENT_SCOPE, name="c",
        encrypted={FIELD: CredentialCipher(OLD).encrypt(value), "org": "org-1"},
    ))
    # org 字段不是密文形态 → 解密会失败 → 该条记入 failed（如实，不是静默跳过）
    report = rotate_credentials(vault, ring)
    assert report.failed and "c" in report.failed[0]
    # 原密文没被覆盖
    record = vault.load_credential(DEPLOYMENT_SCOPE, "c")
    assert record is not None
    assert CredentialCipher(OLD).decrypt(record.encrypted[FIELD]) == value


def test_多作用域都能轮换() -> None:
    ring = CredentialCipher(NEW, [OLD])
    vault = InMemoryCredentialVault()
    for scope in (DEPLOYMENT_SCOPE, "alice", "bob"):
        vault.save_credential(StoredCredential(
            scope=scope, name="model:x",
            encrypted=dict({FIELD: CredentialCipher(OLD).encrypt(_secret())}),
        ))
    report = rotate_credentials(vault, ring, (DEPLOYMENT_SCOPE,), extra_scopes=("alice", "bob"))
    assert report.rotated == 3


# ---------------------------------------------------------------------------
# 三、失败必须显性（漏配旧密钥是最容易犯的错）
# ---------------------------------------------------------------------------


def test_漏配旧密钥时轮换会把失败记账且不覆盖原密文() -> None:
    """这是最危险的误操作：直接换主密钥、忘了配 OLD_KEYS 就跑轮换。

    期望：**不静默跳过、也不覆盖**——记进 failed 并让人看到（原密文还在，
    把旧密钥补回去还能救）。
    """
    vault = _vault_with_ciphertext(CredentialCipher(OLD), scope=DEPLOYMENT_SCOPE,
                                   name="c", value=_secret())
    before = vault.load_credential(DEPLOYMENT_SCOPE, "c")
    assert before is not None
    before_token = before.encrypted[FIELD]

    report = rotate_credentials(vault, CredentialCipher(NEW))   # 没挂旧密钥
    assert report.rotated == 0
    assert report.failed and "c" in report.failed[0]
    assert report.ok is False
    assert "漏配" in report.describe(), "描述里要给出可操作的原因"

    after = vault.load_credential(DEPLOYMENT_SCOPE, "c")
    assert after is not None
    assert after.encrypted[FIELD] == before_token, "失败时绝不能覆盖原密文"


def test_cli_有失败时退出码为5(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    store = SqliteStore(db)
    store.save_credential(StoredCredential(
        scope=DEPLOYMENT_SCOPE, name="model:x",
        encrypted=dict({FIELD: CredentialCipher(OLD).encrypt(_secret())}),
    ))
    store.close()

    env = {"WARDEN_CREDENTIAL_KEY": NEW.decode()}      # 只有新密钥、没有 OLD_KEYS
    import os as _os
    old_env = dict(_os.environ)
    _os.environ.update(env)
    _os.environ.pop("WARDEN_CREDENTIAL_OLD_KEYS", None)
    try:
        with pytest.raises(SystemExit) as exc:
            cli.main(["rotate-credentials", "--db", str(db)])
        assert exc.value.code == 5
    finally:
        _os.environ.clear()
        _os.environ.update(old_env)


def test_cli_轮换成功后只用新密钥可读(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    store = SqliteStore(db)
    value = _secret()
    store.save_credential(StoredCredential(
        scope=DEPLOYMENT_SCOPE, name="model:x",
        encrypted=dict({FIELD: CredentialCipher(OLD).encrypt(value)}),
    ))
    store.close()

    import os as _os
    old_env = dict(_os.environ)
    _os.environ.update({
        "WARDEN_CREDENTIAL_KEY": NEW.decode(),
        "WARDEN_CREDENTIAL_OLD_KEYS": OLD.decode(),
    })
    try:
        cli.main(["rotate-credentials", "--db", str(db)])
    finally:
        _os.environ.clear()
        _os.environ.update(old_env)

    # 用一个**只配了新密钥**的 broker 验证：能读出来，说明轮换真的生效了
    reopened = SqliteStore(db)
    broker = default_broker({"WARDEN_CREDENTIAL_KEY": NEW.decode()}, vault=reopened)
    lease = broker.issue("model:x")
    assert lease.value.fields[FIELD] == value


# ---------------------------------------------------------------------------
# 四、装配：默认 broker 认环境变量里的旧密钥
# ---------------------------------------------------------------------------


def test_default_broker_从环境变量读旧密钥() -> None:
    old_token = CredentialCipher(OLD).encrypt(_secret())
    vault = InMemoryCredentialVault()
    vault.save_credential(StoredCredential(
        scope=DEPLOYMENT_SCOPE, name="c", encrypted=dict({FIELD: old_token})
    ))
    broker = default_broker(
        {"WARDEN_CREDENTIAL_KEY": NEW.decode(), "WARDEN_CREDENTIAL_OLD_KEYS": OLD.decode()},
        vault=vault,
    )
    assert broker.issue("c").value.fields[FIELD] is not None      # 旧密钥兜底生效


def test_default_broker_未配旧密钥时读不出老密文() -> None:
    old_token = CredentialCipher(OLD).encrypt(_secret())
    vault = InMemoryCredentialVault()
    vault.save_credential(StoredCredential(
        scope=DEPLOYMENT_SCOPE, name="c", encrypted=dict({FIELD: old_token})
    ))
    broker = default_broker({"WARDEN_CREDENTIAL_KEY": NEW.decode()}, vault=vault)
    with pytest.raises(InvalidToken):
        broker.issue("c")


def test_未配主密钥时不给历史密钥兜底() -> None:
    """临时密钥 + 历史密钥混在一起只会让"为什么解不开"更难查，所以不混。"""
    broker: CredentialBroker = default_broker({"WARDEN_CREDENTIAL_OLD_KEYS": OLD.decode()})
    assert broker._cipher.rotation_pending is False  # noqa: SLF001
