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
from collections.abc import Iterable, Mapping
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


class CorruptCredentialError(ValueError):
    """凭证存在，但落库的密文 JSON 已损坏。

    必须与"凭证不存在"区分开：前者是真数据损坏（要人介入），后者只是没登记。
    """


def decode_fields(raw: object) -> dict[str, str]:
    """把落库的 JSON 文本还原成密文字典。

    坏数据**显式抛 `CorruptCredentialError`**，不再返回空字典。返回空字典曾把"数据损坏"
    伪装成正常：`has()` 说"有"、`issue()` 发出一张**空凭证**（字段全无）却被当成可用凭证，
    比直接报错危险得多。"""
    try:
        obj = json.loads(str(raw))
    except (json.JSONDecodeError, TypeError) as e:
        raise CorruptCredentialError(f"凭证密文不是合法 JSON: {e}") from e
    if not isinstance(obj, dict):
        raise CorruptCredentialError("凭证密文 JSON 不是对象")
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


@dataclass(frozen=True)
class RotationReport:
    """一次密钥轮换的结果（可打印、可审计）。"""

    scanned: int
    rotated: int
    already_current: int
    failed: tuple[str, ...]  # 解不开的凭证名（**必须报出来**，不能静默跳过）

    @property
    def ok(self) -> bool:
        return not self.failed

    def describe(self) -> str:
        lines = [
            f"密钥轮换：扫描 {self.scanned} 条，重加密 {self.rotated} 条，"
            f"已是新密钥 {self.already_current} 条"
        ]
        if self.failed:
            lines.append(
                f"⚠️ 有 {len(self.failed)} 条**任何密钥都解不开**（未改动，数据保持原样）："
                + "、".join(self.failed)
                + "\n   常见原因：轮换时漏配 WARDEN_CREDENTIAL_OLD_KEYS，或密文被损坏。"
            )
        return "\n".join(lines)


def rotate_credentials(
    vault: object,
    cipher: object,
    scopes: Iterable[str] = (DEPLOYMENT_SCOPE,),
    *,
    extra_scopes: Iterable[str] = (),
) -> RotationReport:
    """把存量凭证密文从旧密钥重加密到当前密钥。

    为什么需要它：换了 `WARDEN_CREDENTIAL_KEY` 之后，**旧密文用新密钥解不开**——
    没有这个步骤，轮换就等于把已有凭证全废掉。

    做法：逐个读出密文 → 判断是否还是旧密钥（`needs_rotation`）→ 解出明文 →
    用当前密钥重新加密写回。**只改密文，不动明文、不动其它字段。**

    安全与诚实：
      - 解不开的凭证**原样保留**并记进 `failed`，不会被静默删掉或覆盖；
      - 轮换是**幂等**的：已经用当前密钥加密的会直接跳过（`already_current`）；
      - 轮换期间旧密钥仍在 `old_key_materials` 里，所以**读写都不会中断**；
        全部重加密完再摘掉旧密钥即可。
    """
    scanned = rotated = already = 0
    failed: list[str] = []
    all_scopes = tuple(scopes) + tuple(extra_scopes)

    for scope in all_scopes:
        for name in vault.list_credential_names(scope):  # type: ignore[attr-defined]
            scanned += 1
            try:
                record = vault.load_credential(scope, name)  # type: ignore[attr-defined]
                if record is None:       # 刚好被删了
                    continue
                encrypted = dict(record.encrypted)
                # 判断是否需要轮换；"谁也解不开"会在这里抛错（数据问题，不能当"无需轮换"）
                if not any(
                    cipher.needs_rotation(token) for token in encrypted.values()  # type: ignore[attr-defined]
                ):
                    already += 1
                    continue
                # 用旧密钥解出明文，再用当前密钥重新加密（密文换了，明文一字不动）
                refreshed = {
                    field: cipher.encrypt(cipher.decrypt(token))  # type: ignore[attr-defined]
                    for field, token in encrypted.items()
                }
            except Exception:  # noqa: BLE001 - 解不开/密文损坏就如实记账，绝不覆盖
                failed.append(f"{scope or '<部署级>'}/{name}")
                continue
            vault.save_credential(  # type: ignore[attr-defined]
                StoredCredential(scope=scope, name=name, encrypted=refreshed)
            )
            rotated += 1

    return RotationReport(
        scanned=scanned, rotated=rotated, already_current=already, failed=tuple(failed)
    )
