"""凭证保管库（Vault）——把"加密后的密文"和"租约"真正落库。

为什么需要这一层：
  `CredentialCipher` 的 AES-GCM 加密本身是真的，但如果加解密的对象活在进程内的一个
  dict 里，进程一退就没了 —— 加密也就没兑现任何价值（重启后凭证还得重新导入）。
  Vault 把密文与租约记录交给存储层保管，**重启后仍在**，加密才真正有意义。

两条硬约束：
  1. **明文绝不落库**。Vault 只存密文（`encrypted` 字段）。租约记录只存
     `name / issued_at / expires_at` 这类元数据，取租约时再按 name 取密文解密 ——
     所以即使有人翻库，也只能看到密文与"某凭证被借出过"的事实，看不到密钥。
  2. **按 scope 隔离**。每条凭证都带一个作用域标识（部署级/租户级/用户级），
     不同 scope 之间互不可见 —— 同一台服务上 A 用户导入的 key，B 用户读不到。

实现形态：
  - `InMemoryCredentialVault`：进程内实现，行为与接入前一致（默认，用于演示/测试）。
  - `SqliteStore` / `PostgresStore`：**结构化满足本协议**（方法名与签名一致即可），
    所以存储层不需要反向 import 本模块，依赖方向保持单向。
"""

from __future__ import annotations

import datetime as _dt
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

# 部署级作用域：由环境/启动参数提供、全体调用者共用的凭证（如部署时配的模型 key）。
# 其余作用域由调用方传入（本产品用调用者身份，见 web/server.py）。
DEPLOYMENT_SCOPE = ""


@dataclass(frozen=True)
class StoredCredential:
    """一条落库的凭证：只有密文，没有任何明文。"""

    scope: str
    name: str
    encrypted: Mapping[str, str]  # 字段名 -> 密文


@dataclass(frozen=True)
class StoredLease:
    """一条落库的租约记录：只有元数据，没有明文。

    明文不随租约落库——取租约时按 `name` 回查密文再解密，
    这样"租约可跨重启存活"与"明文不落盘"两件事可以并存。
    """

    scope: str
    lease_id: str
    name: str
    issued_at: _dt.datetime
    expires_at: _dt.datetime


class CredentialVault(Protocol):
    """凭证与租约的持久化接口（结构化协议，存储层按此实现）。"""

    def save_credential(self, credential: StoredCredential) -> None: ...

    def load_credential(self, scope: str, name: str) -> StoredCredential | None: ...

    def delete_credential(self, scope: str, name: str) -> None: ...

    def list_credential_names(self, scope: str) -> list[str]: ...

    def save_credential_lease(self, lease: StoredLease) -> None: ...

    def load_credential_lease(self, scope: str, lease_id: str) -> StoredLease | None: ...

    def delete_credential_lease(self, scope: str, lease_id: str) -> None: ...

    def purge_expired_credential_leases(self, scope: str, now: _dt.datetime) -> int: ...


class InMemoryCredentialVault:
    """进程内保管库：不落盘，进程退出即丢（默认；多副本不共享）。"""

    def __init__(self) -> None:
        self._credentials: dict[tuple[str, str], dict[str, str]] = {}
        self._leases: dict[tuple[str, str], StoredLease] = {}

    def save_credential(self, credential: StoredCredential) -> None:
        self._credentials[(credential.scope, credential.name)] = dict(
            credential.encrypted
        )

    def load_credential(self, scope: str, name: str) -> StoredCredential | None:
        encrypted = self._credentials.get((scope, name))
        if encrypted is None:
            return None
        return StoredCredential(scope=scope, name=name, encrypted=dict(encrypted))

    def delete_credential(self, scope: str, name: str) -> None:
        self._credentials.pop((scope, name), None)

    def list_credential_names(self, scope: str) -> list[str]:
        return sorted(name for (s, name) in self._credentials if s == scope)

    def save_credential_lease(self, lease: StoredLease) -> None:
        self._leases[(lease.scope, lease.lease_id)] = lease

    def load_credential_lease(self, scope: str, lease_id: str) -> StoredLease | None:
        return self._leases.get((scope, lease_id))

    def delete_credential_lease(self, scope: str, lease_id: str) -> None:
        self._leases.pop((scope, lease_id), None)

    def purge_expired_credential_leases(self, scope: str, now: _dt.datetime) -> int:
        expired = [
            key for key, lease in self._leases.items()
            if key[0] == scope and lease.expires_at <= now
        ]
        for key in expired:
            del self._leases[key]
        return len(expired)


def encode_fields(encrypted: Mapping[str, str]) -> str:
    """把密文字典编码成落库用的 JSON 文本（存储层共用，避免各写一遍）。"""
    return json.dumps(dict(encrypted), ensure_ascii=False)


def decode_fields(raw: object) -> dict[str, str]:
    """把落库的 JSON 文本还原成密文字典；坏数据返回空字典（不抛，避免一条脏行拖垮读取）。"""
    try:
        obj = json.loads(str(raw))
    except json.JSONDecodeError:
        return {}
    if not isinstance(obj, dict):
        return {}
    return {str(k): str(v) for k, v in obj.items()}


def as_vault(store: object) -> CredentialVault:
    """把一个存储对象当成保管库用；不支持则退回进程内实现。

    这样 `build_app` 可以无条件把 `store` 传进来：SQLite / PostgreSQL 会真落库，
    而内存版存储（演示/测试）自动退回进程内，不会因为"少实现一个接口"而崩。
    """
    required = (
        "save_credential",
        "load_credential",
        "delete_credential",
        "list_credential_names",
        "save_credential_lease",
        "load_credential_lease",
        "delete_credential_lease",
        "purge_expired_credential_leases",
    )
    if all(callable(getattr(store, name, None)) for name in required):
        return store  # type: ignore[return-value]  # 结构化满足协议
    return InMemoryCredentialVault()
