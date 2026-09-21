"""AES-GCM 加密原语 —— 凭证落库前先加密，市面上常见的"密钥明文躺数据库"问题。

  - AES-GCM：公认的认证加密模式。除了加密，还带完整性校验——
    被篡改的密文在解密时会主动抛错，而不会解出一段"坏数据"继续用。
  - 密钥来自外部（环境变量 / KMS / 托管），代码里不落硬编码密钥。
  - 每个密文带独立随机 nonce（盐），同样的明文两次加密结果不同，防重放。
  - **支持密钥轮换**：主密钥之外可挂若干"历史密钥"（只用于解密兜底）。
    轮换流程 = 配新主密钥 + 把旧密钥放进 `WARDEN_CREDENTIAL_OLD_KEYS` →
    跑 `rotate_credentials()` 把存量密文重加密 → 摘掉旧密钥。

为什么用 GCM 而不是更"简单"的 XOR / Base64：
  XOR 和 Base64 只是"编码"，不是加密——拿到的人一秒就能还原。
  AES-GCM 是真正的密码学加密：没有密钥，拿到密文也还原不出明文。
  安全的最低门槛。
"""

from __future__ import annotations

import base64
import hashlib
import os
from collections.abc import Iterable

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class CredentialCipher:
    """用一把外部提供的密钥做 AES-GCM 加解密。

    密钥 Key 由外部传入（典型来自环境变量），可以是任意字节——我们会用它
    派生出一个 32 字节的 AES-256 密钥（SHA-256 拉伸），保证强度一致。

    `old_key_materials` 是**密钥轮换**用的历史密钥：只在**解密**时兜底尝试，
    加密永远用当前密钥。用法：先把新密钥配成主密钥、旧密钥放进 old，跑一遍
    `rotate_credentials()` 把存量密文重加密，然后就可以把旧密钥摘掉。
    """

    def __init__(
        self,
        key_material: bytes,
        old_key_materials: Iterable[bytes] = (),
    ) -> None:
        if not key_material:
            raise ValueError("密钥不能为空")
        self._aes_key = hashlib.sha256(key_material).digest()  # 32 字节 AES-256
        # 历史密钥同理派生；解密时逐个尝试（GCM 是认证加密，密钥不对会直接校验失败，
        # 所以"试"是安全的——不会解出一段看似成功的坏数据）
        self._old_keys = [hashlib.sha256(m).digest() for m in old_key_materials if m]

    @property
    def rotation_pending(self) -> bool:
        """是否还挂着历史密钥（挂着一般意味着"还没跑完轮换"）。"""
        return bool(self._old_keys)

    def encrypt(self, plaintext: str) -> str:
        """加密字符串，返回 Base64( nonce || ciphertext || tag )。"""
        return self._encrypt_with(self._aes_key, plaintext)

    def decrypt(self, token: str) -> str:
        """解密。先试当前密钥，再试历史密钥（密钥轮换期间两者并存）。

        全部失败时抛 `InvalidToken`——包含"密文被篡改"与"没有任何一把密钥能解"两种情况。
        后者的典型场景是：轮换时把**解密用**的旧密钥漏配了。
        """
        try:
            return self._decrypt_with(self._aes_key, token)
        except Exception:  # noqa: BLE001 - 换历史密钥再试
            last: Exception | None = None
        for old_key in self._old_keys:
            try:
                return self._decrypt_with(old_key, token)
            except Exception as e:  # noqa: BLE001 - 继续试下一把
                last = e
        raise InvalidToken(
            "密文无法解密：当前密钥与所有历史密钥都解不开"
            "（密文被改过，或轮换时漏配了 WARDEN_CREDENTIAL_OLD_KEYS）"
        ) from last

    def needs_rotation(self, token: str) -> bool:
        """该密文是否还是旧密钥加密的（轮换时用来判断要不要重写）。

        三种情况必须分开，**不能把第三种混进"不需要轮换"里**：
          - 当前密钥能解 → False（已经是最新的）；
          - 只有历史密钥能解 → True（需要重加密）；
          - **都解不开 → 抛 `InvalidToken`**。这是数据问题（漏配旧密钥 / 密文损坏），
            如果返回 False 让它"跳过"，就等于**把故障伪装成正常**——
            而轮换恰恰是最不能静默漏掉的场景（漏掉的那条，摘掉旧密钥后就永久解不开了）。
        """
        try:
            self._decrypt_with(self._aes_key, token)
            return False       # 当前密钥就能解 → 不需要轮换
        except Exception:  # noqa: BLE001 - 落到历史密钥检查
            pass
        if self.can_decrypt_with_old(token):
            return True
        raise InvalidToken(
            "密文既不能被当前密钥、也不能被任何历史密钥解开——不能当成「无需轮换」跳过："
            "那会把「数据有问题」伪装成「一切正常」。常见原因：轮换时漏配 "
            "WARDEN_CREDENTIAL_OLD_KEYS，或密文已损坏。"
        )

    def can_decrypt_with_old(self, token: str) -> bool:
        """是否能用**历史密钥**解开（校验轮换有无遗漏时用）。"""
        for old_key in self._old_keys:
            try:
                self._decrypt_with(old_key, token)
                return True
            except Exception:  # noqa: BLE001 - 继续试
                continue
        return False

    @staticmethod
    def _encrypt_with(aes_key: bytes, plaintext: str) -> str:
        aesgcm = AESGCM(aes_key)
        nonce = os.urandom(12)  # GCM 标准 96-bit nonce
        ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
        return base64.b64encode(nonce + ct).decode("ascii")

    @staticmethod
    def _decrypt_with(aes_key: bytes, token: str) -> str:
        raw = base64.b64decode(token.encode("ascii"))
        nonce, ct = raw[:12], raw[12:]
        aesgcm = AESGCM(aes_key)
        return aesgcm.decrypt(nonce, ct, None).decode("utf-8")


def derive_key_from_env() -> bytes:
    """从环境变量 WARDEN_CREDENTIAL_KEY 取密钥材料。

    没配置时不允许加密落库（宁可抛错，也不要有默认弱密钥悄悄上线）。
    """
    key = os.environ.get("WARDEN_CREDENTIAL_KEY")
    if not key:
        raise RuntimeError(
            "未配置 WARDEN_CREDENTIAL_KEY：凭证加密需要外部密钥。"
            "请为每个部署环境生成并配置一把独立密钥。"
        )
    return key.encode("utf-8")


class InvalidToken(Exception):
    """密文无法解密 / 完整性校验失败。"""
