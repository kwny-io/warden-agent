"""AES-GCM 加密原语 —— 凭证落库前先加密，市面上常见的"密钥明文躺数据库"问题。

  - AES-GCM：公认的认证加密模式。除了加密，还带完整性校验——
    被篡改的密文在解密时会主动抛错，而不会解出一段"坏数据"继续用。
  - 密钥来自外部（环境变量 / KMS / 托管），代码里不落硬编码密钥。
  - 每个密文带独立随机 nonce（盐），同样的明文两次加密结果不同，防重放。

为什么用 GCM 而不是更"简单"的 XOR / Base64：
  XOR 和 Base64 只是"编码"，不是加密——拿到的人一秒就能还原。
  AES-GCM 是真正的密码学加密：没有密钥，拿到密文也还原不出明文。
  安全的最低门槛。
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class CredentialCipher:
    """用一把外部提供的密钥做 AES-GCM 加解密。

    密钥 Key 由外部传入（典型来自环境变量），可以是任意字节——我们会用它
    派生出一个 32 字节的 AES-256 密钥（SHA-256 拉伸），保证强度一致。
    """

    def __init__(self, key_material: bytes) -> None:
        if not key_material:
            raise ValueError("密钥不能为空")
        self._aes_key = hashlib.sha256(key_material).digest()  # 32 字节 AES-256

    def encrypt(self, plaintext: str) -> str:
        """加密字符串，返回 Base64( nonce || ciphertext || tag )。"""
        aesgcm = AESGCM(self._aes_key)
        nonce = os.urandom(12)  # GCM 标准 96-bit nonce
        ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
        return base64.b64encode(nonce + ct).decode("ascii")

    def decrypt(self, token: str) -> str:
        """解密上面格式的密文。完整性校验失败(auth tag 不符)会抛 InvalidToken。"""
        raw = base64.b64decode(token.encode("ascii"))
        nonce, ct = raw[:12], raw[12:]
        aesgcm = AESGCM(self._aes_key)
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
