"""凭证能力：加密存储 + 短生命周期租约 + Secret 脱敏。

  - crypto.CredentialCipher   —— AES-GCM 落库加密
  - broker.CredentialBroker    —— 租约管理中枢
  - broker.SecretRedactor      —— 日志/输出脱敏
  - broker.default_broker      —— 按环境变量装配（产品路径默认入口）
  - vault.CredentialVault      —— 密文/租约的持久化落点（不传则进程内）
"""

from warden_agent.credential.broker import (
    Credential,
    CredentialBroker,
    CredentialLease,
    SecretRedactor,
    default_broker,
)
from warden_agent.credential.crypto import CredentialCipher, InvalidToken, derive_key_from_env
from warden_agent.credential.vault import (
    DEPLOYMENT_SCOPE,
    CredentialVault,
    InMemoryCredentialVault,
    StoredCredential,
    StoredLease,
    as_vault,
)

__all__ = [
    "CredentialCipher",
    "InvalidToken",
    "derive_key_from_env",
    "Credential",
    "CredentialBroker",
    "CredentialLease",
    "SecretRedactor",
    "default_broker",
    "DEPLOYMENT_SCOPE",
    "CredentialVault",
    "InMemoryCredentialVault",
    "StoredCredential",
    "StoredLease",
    "as_vault",
]
