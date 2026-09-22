"""凭证主密钥的托管方式：把"密钥材料从哪来"从代码里解耦出来。

背景（为什么要这一层）：
  原先密钥材料只有一个来源——环境变量 `WARDEN_CREDENTIAL_KEY`。它的问题不是"不安全"
  （好歹是外部注入、不落代码），而是**不是托管**：密钥以明文躺在部署的 env/配置文件里，
  谁有机器/能看启动参数就能拿到 KEK；而且轮换要人工搬密钥。

  企业交付要求的"KMS/HSM 托管"意思是：**根密钥待在专门的密钥服务里，永远不出现**，
  应用只在需要时请求它解开一段**被包装的数据密钥（DEK）**——这就是**信封加密**
  （envelope encryption）：
      KEK 在 KMS/HSM 里（不出门） + DEK 被 KEK 包装后随配置分发 + 应用解开 DEK 加密数据。
  轮换时只换 DEK 并重新包装，KEK 不动；KEK 轮换由 KMS/HSM 自己负责。

本模块提供：
  - `KeyProvider` 协议：一个 provider 只需回答"当前密钥材料"和"历史密钥材料"；
  - `EnvKeyProvider`：材料直接来自环境变量（= 演进前的行为，保留为默认/本地开发）；
  - `AwsKmsKeyProvider`：用 AWS KMS 的 `Decrypt` 解开被包装的 DEK（需要可选依赖 boto3）；
  - `VaultTransitKeyProvider`：用 HashiCorp Vault 的 transit 引擎解密（走 httpx，无新依赖）；
  - `resolve_key_provider`：按 `WARDEN_KMS_PROVIDER` 选择，未配置则返回 None（回落到 env 模式）。

**诚实边界**：本模块做的是"接入 KMS/HSM 的形状与逻辑"。要真用起来，你需要一个可用的
KMS（AWS KMS / Vault），部署侧用 AWS CLI / vault CLI 生成并包装一次 DEK（命令见
`docs/operations.md`）。仓库里的测试用桩替身验证逻辑，**不联真实云**。
"""

from __future__ import annotations

import base64
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from warden_agent.core.settings import env_str


@runtime_checkable
class KeyProvider(Protocol):
    """密钥材料来源。实现者只需回答两个问题。"""

    name: str

    def current_key(self) -> bytes:
        """当前用于**加密**的密钥材料（信封模式下 = 解开后的 DEK）。"""
        ...

    def historical_keys(self) -> list[bytes]:
        """轮换期用于**解密兜底**的历史密钥材料（没有则空列表）。"""
        ...


class KeyProviderError(RuntimeError):
    """密钥托管相关的失败（配置缺失 / KMS 拒绝 / 返回不可用）。"""


@dataclass
class StaticKeyProvider:
    """把给定材料当作密钥来源。用于测试，以及"我就是想直接给材料"的显式场景。"""

    material: bytes
    old_materials: list[bytes] = field(default_factory=list)
    name: str = "static"

    def current_key(self) -> bytes:
        return self.material

    def historical_keys(self) -> list[bytes]:
        return list(self.old_materials)


@dataclass
class EnvKeyProvider:
    """密钥材料直接来自环境变量（演进前的行为，保留为默认）。

    读 `WARDEN_CREDENTIAL_KEY`（主）与 `WARDEN_CREDENTIAL_OLD_KEYS`（历史，逗号分隔）。
    """

    env: Mapping[str, str]
    name: str = "env"

    def current_key(self) -> bytes:
        material = self.env.get("WARDEN_CREDENTIAL_KEY")
        if not material:
            raise KeyProviderError(
                "env 模式需要 WARDEN_CREDENTIAL_KEY（或改用 WARDEN_KMS_PROVIDER 接 KMS）"
            )
        return material.encode("utf-8")

    def historical_keys(self) -> list[bytes]:
        return [
            item.strip().encode("utf-8")
            for item in (self.env.get("WARDEN_CREDENTIAL_OLD_KEYS") or "").split(",")
            if item.strip()
        ]


@dataclass
class AwsKmsKeyProvider:
    """AWS KMS 信封模式：用 KMS 解开被包装的 DEK。

    `kms_client` 是任意提供 `decrypt(CiphertextBlob=<bytes>) -> {"Plaintext": <bytes>}`
    的客户端（生产用 boto3.client("kms")；测试用桩）。不直接依赖 boto3，
    所以本模块在没有 boto3 的环境里也能被导入、被测。
    """

    kms_client: Any
    wrapped_key: bytes
    wrapped_old_keys: list[bytes] = field(default_factory=list)
    name: str = "aws-kms"

    def current_key(self) -> bytes:
        return self._unwrap(self.wrapped_key)

    def historical_keys(self) -> list[bytes]:
        return [self._unwrap(blob) for blob in self.wrapped_old_keys]

    def _unwrap(self, blob: bytes) -> bytes:
        try:
            result = self.kms_client.decrypt(CiphertextBlob=blob)
        except Exception as e:  # noqa: BLE001 - 统一转成可读的托管错误
            raise KeyProviderError(f"AWS KMS 解包密钥失败：{e}") from e
        plaintext = result.get("Plaintext") if isinstance(result, dict) else None
        if not plaintext:
            raise KeyProviderError("AWS KMS 未返回明文密钥（返回为空）")
        return bytes(plaintext)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> AwsKmsKeyProvider:
        """从环境变量构造：`WARDEN_KMS_WRAPPED_KEY` = base64(被包装的密钥)。

        boto3 在**这时**才导入——没有 boto3 而误配了 aws-kms 会得到明确报错，
        而不是 import 期就把整个应用带崩。
        """
        blob = env.get("WARDEN_KMS_WRAPPED_KEY")
        if not blob:
            raise KeyProviderError(
                "aws-kms 模式需要 WARDEN_KMS_WRAPPED_KEY（base64 的被包装 DEK；"
                "生成方式见 docs/operations.md）"
            )
        try:
            import boto3  # type: ignore[import-not-found]
        except ModuleNotFoundError as e:
            raise KeyProviderError(
                "aws-kms 模式需要可选依赖 boto3（pip install 'warden-agent[aws]'）"
            ) from e
        # 区域交给 boto3 自己解析（AWS_REGION / profile / 实例元数据），本模块不读区域变量——
        # 少一处"另一个模块也读配置"的口子，也避免和配置注册表的授权范围打架。
        return cls(kms_client=boto3.client("kms"), wrapped_key=_b64decode(blob))


@dataclass
class VaultTransitKeyProvider:
    """HashiCorp Vault transit 信封模式：密文交给 Vault 解，密钥不出 Vault。

    用 httpx 直接调 transit 的 decrypt 接口（项目已依赖 httpx，不引入 hvac）。
    Vault 返回的明文是 base64。
    """

    client: Any  # httpx.Client（测试可注入 MockTransport）
    addr: str
    key_name: str
    wrapped_key: str
    wrapped_old_keys: list[str] = field(default_factory=list)
    name: str = "vault-transit"

    def current_key(self) -> bytes:
        return self._decrypt(self.wrapped_key)

    def historical_keys(self) -> list[bytes]:
        return [self._decrypt(ciphertext) for ciphertext in self.wrapped_old_keys]

    def _decrypt(self, ciphertext: str) -> bytes:
        url = f"{self.addr.rstrip('/')}/v1/transit/decrypt/{self.key_name}"
        try:
            resp = self.client.post(url, json={"ciphertext": ciphertext})
        except Exception as e:  # noqa: BLE001 - 统一转成托管错误
            raise KeyProviderError(f"Vault transit 请求失败：{e}") from e
        if resp.status_code != 200:
            raise KeyProviderError(
                f"Vault transit 解密被拒（HTTP {resp.status_code}）：{resp.text[:200]}"
            )
        body = resp.json()
        plaintext_b64 = (body.get("data") or {}).get("plaintext")
        if not plaintext_b64:
            raise KeyProviderError("Vault transit 未返回明文")
        return base64.b64decode(plaintext_b64)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> VaultTransitKeyProvider:
        addr = env.get("WARDEN_VAULT_ADDR")
        token = env.get("WARDEN_VAULT_TOKEN")
        key_name = env.get("WARDEN_VAULT_KEY_NAME")
        wrapped = env.get("WARDEN_KMS_WRAPPED_KEY")
        missing = [
            n
            for n, v in (
                ("WARDEN_VAULT_ADDR", addr),
                ("WARDEN_VAULT_TOKEN", token),
                ("WARDEN_VAULT_KEY_NAME", key_name),
                ("WARDEN_KMS_WRAPPED_KEY", wrapped),
            )
            if not v
        ]
        if missing:
            raise KeyProviderError(f"vault-transit 模式缺少配置：{', '.join(missing)}")
        import httpx

        client = httpx.Client(headers={"X-Vault-Token": str(token)}, timeout=10.0)
        return cls(
            client=client,
            addr=str(addr),
            key_name=str(key_name),
            wrapped_key=str(wrapped),
        )


def resolve_key_provider(env: Mapping[str, str] | None = None) -> KeyProvider | None:
    """按 `WARDEN_KMS_PROVIDER` 选一个 provider。

    未配置 / `env` → 返回 None，由调用方回落到原来的环境变量分支（保持向后兼容）。
    其它取值不认识 → 直接报错，**不静默回落到 env**（那等于"以为在托管、其实没托管"）。
    """
    src: Mapping[str, str] = env if env is not None else os.environ
    kind = env_str("WARDEN_KMS_PROVIDER", "", src).strip().lower()
    if kind in ("", "env"):
        return None
    if kind == "aws-kms":
        return AwsKmsKeyProvider.from_env(src)
    if kind == "vault-transit":
        return VaultTransitKeyProvider.from_env(src)
    raise KeyProviderError(
        f"未知的 WARDEN_KMS_PROVIDER={kind!r}；支持：env / aws-kms / vault-transit"
    )


def _b64decode(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except Exception as e:  # noqa: BLE001 - 配置格式错误要给明确提示
        raise KeyProviderError("WARDEN_KMS_WRAPPED_KEY 不是合法的 base64") from e
