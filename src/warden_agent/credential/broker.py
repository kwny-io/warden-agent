"""凭证管理 —— CredentialBroker（短生命周期租约）+ 加密存储 + Secret 脱敏。

  - CredentialBroker：凭证统一入口，往外发的是"短生命周期 Lease（租约）"，
    而不是永久的明文密钥。用的人用完就丢，过期即失效。
  - SecretRedactor：在日志/输出里抹掉密钥明文，防止密钥被打印到日志里泄漏。
  - CredentialCipher：落库前的 AES-GCM 加密。

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
import secrets
from collections.abc import Mapping
from dataclasses import dataclass

from warden_agent.credential.crypto import CredentialCipher


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
    """凭证的管理中枢：注册/借出/回收/校验，全部走加密存储 + 租约。"""

    def __init__(
        self,
        cipher: CredentialCipher,
        ttl_seconds: int = 300,
        now: _dt.datetime | None = None,
    ) -> None:
        self._cipher = cipher
        self._ttl = _dt.timedelta(seconds=ttl_seconds)
        self._secrets: dict[str, dict[str, str]] = {}   # name -> 加密后的字段
        self._leases: dict[str, CredentialLease] = {}
        self._clock = now

    def _now(self) -> _dt.datetime:
        return self._clock or _dt.datetime.now(_dt.UTC)

    # ---- 注册 / 加密落库 ----
    def register(self, name: str, fields: Mapping[str, str]) -> None:
        """把一条凭证（如 api_key）加密后存进库里。明文不留存。"""
        encrypted = {
            key: self._cipher.encrypt(value) for key, value in fields.items()
        }
        self._secrets[name] = encrypted

    # ---- 租约 ----
    def issue(self, name: str, ttl_seconds: int | None = None) -> CredentialLease:
        """借出一条凭证的临时租约。返回的 value 是解密后的明文。"""
        encrypted = self._secrets.get(name)
        if encrypted is None:
            raise KeyError(f"没有注册凭证: {name!r}")
        now = self._now()
        ttl = (
            _dt.timedelta(seconds=ttl_seconds)
            if ttl_seconds is not None
            else self._ttl
        )
        plain = {key: self._cipher.decrypt(value) for key, value in encrypted.items()}
        lease = CredentialLease(
            lease_id=f"lease-{secrets.token_hex(8)}",
            name=name,
            value=Credential(name=name, fields=plain),
            issued_at=now,
            expires_at=now + ttl,
        )
        self._leases[lease.lease_id] = lease
        return lease

    def revoke(self, lease_id: str) -> None:
        """主动回收一张租约（用完即弃 / 提前撤销）。"""
        self._leases.pop(lease_id, None)

    def get(self, lease_id: str) -> CredentialLease | None:
        """取回一张未过期的租约。过期 / 已回收返回 None。"""
        lease = self._leases.get(lease_id)
        if lease is None:
            return None
        if lease.is_expired:
            self._leases.pop(lease_id, None)
            return None
        return lease

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
