"""凭证管理 —— CredentialBroker（短生命周期租约）+ 加密存储 + Secret 脱敏。

  - CredentialBroker：凭证统一入口，往外发的是"短生命周期 Lease（租约）"，
    而不是永久的明文密钥。用的人用完就丢，过期即失效。
  - SecretRedactor：在日志/输出里抹掉密钥明文，防止密钥被打印到日志里泄漏。
  - CredentialCipher：落库前的 AES-GCM 加密。
  - CredentialVault：密文与租约的持久化落点（见 vault.py）——没有它，"加密"只是
    进程内的一次表演：进程一退凭证就没了。broker 默认用进程内 vault，
    传一个 SQLite/PostgreSQL 存储即可真正落库。

为什么 Lease（租约）是关键：
  直接给各模块一把永久 API Key，等于把钥匙撒得到处都是 —— 丢了无从回收。
  Lease 模型：凭证被"借出"一小段时间(ttl)，到期自动作废；
  每个 Lease 带着唯一 id 和 usage 审计，出事了能定位"谁在用、什么时候用的"。

典型链路（呈现给上层 Agent 使用）：
    broker = CredentialBroker(cipher, ttl_seconds=300)
    broker.register("openai", {"api_key": "sk-..."})      # 加密落库
    lease = broker.issue("openai")                        # 借出 300 秒租约
    lease.value.api_key                                     # 用明文
    broker.revoke(lease.id)                               # 提前回收
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass

from warden_agent.credential.crypto import CredentialCipher
from warden_agent.credential.vault import (
    DEPLOYMENT_SCOPE,
    CredentialVault,
    InMemoryCredentialVault,
    StoredCredential,
    StoredLease,
)

logger = logging.getLogger("warden_agent.credential")


@dataclass(frozen=True)
class Credential:
    """一条凭证的明文视图（只在租约短暂有效期内出现）。"""

    name: str
    fields: Mapping[str, str]


@dataclass(frozen=True)
class CredentialLease:
    """借出的一纸准证：到期自动作废，可提前 revoke。"""

    lease_id: str
    name: str
    value: Credential
    issued_at: _dt.datetime
    expires_at: _dt.datetime

    @property
    def is_expired(self) -> bool:
        return _dt.datetime.now(_dt.UTC) >= self.expires_at


class CredentialBroker:
    """凭证的管理中枢：注册/借出/回收/校验，全部走加密存储 + 租约。

    存储后端是 `vault`：默认进程内（演示/测试），传 SQLite/PostgreSQL 存储则落库。
    每条凭证归属一个 `scope`（作用域）：默认 `DEPLOYMENT_SCOPE`（部署级、全体共用），
    调用方可按身份传自己的 scope 以隔离彼此的凭证。
    """

    def __init__(
        self,
        cipher: CredentialCipher,
        ttl_seconds: int = 300,
        now: _dt.datetime | None = None,
        vault: CredentialVault | None = None,
        scope: str = DEPLOYMENT_SCOPE,
    ) -> None:
        self._cipher = cipher
        self._ttl = _dt.timedelta(seconds=ttl_seconds)
        self._vault: CredentialVault = vault or InMemoryCredentialVault()
        self._scope = scope
        self._clock = now

    def _now(self) -> _dt.datetime:
        return self._clock or _dt.datetime.now(_dt.UTC)

    def _scope_for(self, scope: str | None) -> str:
        """解析作用域：显式传入优先，否则用 broker 的默认 scope。"""
        return self._scope if scope is None else scope

    # ---- 注册 / 加密落库 ----
    def register(
        self, name: str, fields: Mapping[str, str], scope: str | None = None
    ) -> None:
        """把一条凭证（如 api_key）加密后存进库里。明文不留存。"""
        encrypted = {
            key: self._cipher.encrypt(value) for key, value in fields.items()
        }
        self._vault.save_credential(
            StoredCredential(scope=self._scope_for(scope), name=name, encrypted=encrypted)
        )

    def has(self, name: str, scope: str | None = None) -> bool:
        """是否登记过该凭证（不触碰明文，只看密文是否存在）。"""
        return self._vault.load_credential(self._scope_for(scope), name) is not None

    def encrypted_fields(
        self, name: str, scope: str | None = None
    ) -> Mapping[str, str] | None:
        """取回一条凭证的**密文**（供审计/测试核对"库里没有明文"）。未登记返回 None。"""
        record = self._vault.load_credential(self._scope_for(scope), name)
        return None if record is None else dict(record.encrypted)

    # ---- 租约 ----
    def issue(
        self, name: str, ttl_seconds: int | None = None, scope: str | None = None
    ) -> CredentialLease:
        """借出一条凭证的临时租约。返回的 value 是解密后的明文。

        租约记录（不含明文）落进 vault：进程重启后仍能校验"这张租约还在不在有效期内"，
        明文则在 `get()` 时按 name 回查密文现解——所以持久化不等于明文落盘。
        """
        target = self._scope_for(scope)
        record = self._vault.load_credential(target, name)
        if record is None:
            raise KeyError(f"没有注册凭证: {name!r}")
        now = self._now()
        ttl = (
            _dt.timedelta(seconds=ttl_seconds)
            if ttl_seconds is not None
            else self._ttl
        )
        plain = {
            key: self._cipher.decrypt(value) for key, value in record.encrypted.items()
        }
        lease = CredentialLease(
            lease_id=f"lease-{secrets.token_hex(8)}",
            name=name,
            value=Credential(name=name, fields=plain),
            issued_at=now,
            expires_at=now + ttl,
        )
        self._vault.save_credential_lease(
            StoredLease(
                scope=target,
                lease_id=lease.lease_id,
                name=name,
                issued_at=now,
                expires_at=lease.expires_at,
            )
        )
        # 惰性清理：顺手把该作用域下已过期的租约记录删掉，免得表只增不减。
        self._vault.purge_expired_credential_leases(target, now)
        return lease

    def revoke(self, lease_id: str, scope: str | None = None) -> None:
        """主动回收一张租约（用完即弃 / 提前撤销）。"""
        self._vault.delete_credential_lease(self._scope_for(scope), lease_id)

    def get(self, lease_id: str, scope: str | None = None) -> CredentialLease | None:
        """取回一张未过期的租约。过期 / 已回收 / 凭证已被删 均返回 None。"""
        target = self._scope_for(scope)
        record = self._vault.load_credential_lease(target, lease_id)
        if record is None:
            return None
        if record.expires_at <= self._now():
            self._vault.delete_credential_lease(target, lease_id)
            return None
        stored = self._vault.load_credential(target, record.name)
        if stored is None:  # 凭证在租约有效期内被删除 → 租约随之失效
            self._vault.delete_credential_lease(target, lease_id)
            return None
        plain = {
            key: self._cipher.decrypt(value) for key, value in stored.encrypted.items()
        }
        return CredentialLease(
            lease_id=record.lease_id,
            name=record.name,
            value=Credential(name=record.name, fields=plain),
            issued_at=record.issued_at,
            expires_at=record.expires_at,
        )

    def verify(self, lease: CredentialLease) -> bool:
        """校验一张租约此刻是否仍有效可用。"""
        return self.get(lease.lease_id) is not None


class SecretRedactor:
    """把字符串里的密钥明文抹掉，防泄漏到日志/输出。"""

    _placeholder = "******"

    def __init__(self, secrets: list[str] | None = None) -> None:
        self._secrets: list[str] = []
        for s in secrets or []:
            self.add(s)

    def add(self, secret: str) -> None:
        if secret and secret not in self._secrets:
            self._secrets.append(secret)

    def redact(self, text: str) -> str:
        """把 text 里出现的任一已登记密钥替换成 ******。"""
        out = text
        for secret in self._secrets:
            if secret:
                out = out.replace(secret, self._placeholder)
        return out


def default_broker(
    env: Mapping[str, str] | None = None,
    *,
    ttl_seconds: int = 300,
    vault: CredentialVault | None = None,
    scope: str = DEPLOYMENT_SCOPE,
) -> CredentialBroker:
    """按环境变量造一个凭证 broker（产品路径的默认入口）。

    `WARDEN_CREDENTIAL_KEY` 提供密钥材料——**推荐每个部署单独生成一把**，
    且**只从环境变量读取**（代码里不写死，也不接受调用方传入）。
    未配置时退化为"进程内临时密钥"：加解密仍然是真加密（不是明文躺内存），
    只是密钥随机生成、不落盘、进程退出即失效。这种情况明确打 warning，
    不制造"已持久化加密"的假象。

    `vault` 决定密文与租约存哪：不传 = 进程内（重启即丢）；传 SQLite/PostgreSQL
    存储 = 真落库（见 vault.py）。产品路径由 build_app 自动把 store 传进来。
    """
    src: Mapping[str, str] = env if env is not None else os.environ
    material = src.get("WARDEN_CREDENTIAL_KEY")
    # 密钥轮换：主密钥之外可挂历史密钥（逗号分隔），**只用于解密兜底**。
    # 轮换流程见 vault.rotate_credentials：配新主密钥 + 旧密钥进 OLD_KEYS → 重加密 → 摘掉旧密钥。
    old_materials = [
        item.strip().encode("utf-8")
        for item in (src.get("WARDEN_CREDENTIAL_OLD_KEYS") or "").split(",")
        if item.strip()
    ]
    if material:
        cipher = CredentialCipher(material.encode("utf-8"), old_materials)
    else:
        logger.warning(
            "未配置 WARDEN_CREDENTIAL_KEY：凭证以**进程内临时密钥**加密"
            "（进程退出即失效，不落盘）。生产环境请为每个部署配置独立密钥。"
        )
        # 临时密钥下不给历史密钥兜底：两者混在一起只会让"为什么解不开"更难查
        cipher = CredentialCipher(secrets.token_bytes(32))
    if old_materials:
        logger.info(
            "凭证密钥轮换：已挂 %d 把历史密钥（仅用于解密）。"
            "跑 `warden rotate-credentials` 完成重加密后即可摘掉它们。",
            len(old_materials),
        )
    return CredentialBroker(
        cipher, ttl_seconds=ttl_seconds, vault=vault, scope=scope
    )
