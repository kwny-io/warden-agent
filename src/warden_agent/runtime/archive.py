"""审计归档：**只追加**的证据包 + 完整性清单（软件层的防篡改归档）。

目的：把审计链归档成"长期留存 / 取证"的证据包。做法：

  - 每次归档写一个**新文件**（内容 = audit-export 的 JSONL，**带链字段** id/prev_hash/hash），
    **绝不覆盖已有文件**（重名直接报错，而不是默默替换）；
  - 同时往 `manifest.jsonl` **追加**一条清单记录：文件名 + 文件 sha256 + 前一清单项的哈希；
  - `verify_archive()` 校验：清单链完整 **且** 每个文件仍在、sha256 与清单一致。

为什么清单也要链起来：只给文件算 sha256、清单本身却能随便改，等于给"篡改"留后门——
改文件后连清单一起改就行。清单**串成链**（每条含前一条的哈希，用 HMAC 键），
于是改任何一个文件或任何一条清单记录都会在 verify 时暴露。

**诚实边界（务必如实说）**：这是"**软件层**的只追加"，不是真正的 WORM（Write Once Read Many）。
真 WORM 需要存储层能力——S3 Object Lock（合规模式）、WORM 设备、或不可变快照。
本模块保证的是："归档一旦写下，此后任何改动都会在 verify 时被发现"。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

MANIFEST_NAME = "manifest.jsonl"


def _now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_hash(
    key: bytes | None, seq: int, prev_hash: str, entry: Mapping[str, Any]
) -> str:
    """算一条清单记录的链哈希：覆盖文件身份 + 前驱 + 序号。"""
    payload = json.dumps(
        {
            "seq": seq,
            "file": entry.get("file"),
            "sha256": entry.get("sha256"),
            "bytes": entry.get("bytes"),
            "at": entry.get("at"),
        },
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    message = f"{seq}|{prev_hash}|{payload}".encode()
    if key is not None:
        return hmac.new(key, message, hashlib.sha256).hexdigest()
    return hashlib.sha256(message).hexdigest()


def _chain_key(chain_key: bytes | str | None) -> bytes | None:
    if chain_key == "env":
        # 与审计链共用同一份密钥材料，读取口径收在 tier-0 的 secret_bytes
        # （runtime 是 tier 2，不能反向 import tier 3 的 web/audit.py）
        from warden_agent.core.settings import secret_bytes

        return secret_bytes("WARDEN_AUDIT_KEY")
    if isinstance(chain_key, str):
        return chain_key.encode("utf-8")
    return chain_key


def _last_manifest(directory: Path) -> tuple[int, str]:
    """读清单最后一条，返回 (seq, hash)；无清单 → (0, "")。"""
    path = directory / MANIFEST_NAME
    if not path.is_file():
        return 0, ""
    seq, digest = 0, ""
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        seq, digest = int(row.get("seq", seq)), str(row.get("hash", ""))
    return seq, digest


def archive_records(
    directory: str | Path,
    rows: Iterable[Mapping[str, Any]],
    *,
    name: str | None = None,
    chain_key: bytes | str | None = "env",
    now_iso: str | None = None,
) -> dict[str, Any]:
    """把一批审计记录（`export_records` 的输出）写成一个**新**归档文件并登记清单。

    - 目标文件已存在 → 抛 `FileExistsError`（**绝不覆盖**已归档的证据）。
    - 返回 `{file, sha256, bytes, seq, manifest}`。
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = now_iso or _now_iso()
    filename = name or f"audit-{stamp.replace(':', '').replace('-', '')}.jsonl"
    if "/" in filename or "\\" in filename or ".." in filename:
        raise ValueError(f"归档文件名必须是纯文件名：{filename!r}")
    target = directory / filename
    if target.exists():
        raise FileExistsError(f"归档文件已存在，拒绝覆盖：{target}")

    with target.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(dict(row), ensure_ascii=False) + "\n")

    digest = _sha256_file(target)
    size = target.stat().st_size

    seq, prev_hash = _last_manifest(directory)
    seq += 1
    entry: dict[str, Any] = {
        "seq": seq,
        "file": filename,
        "sha256": digest,
        "bytes": size,
        "at": stamp,
        "prev_hash": prev_hash,
    }
    entry["hash"] = _manifest_hash(_chain_key(chain_key), seq, prev_hash, entry)
    with (directory / MANIFEST_NAME).open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    return {"file": filename, "sha256": digest, "bytes": size, "seq": seq,
            "manifest": str(directory / MANIFEST_NAME)}


def verify_archive(
    directory: str | Path, *, chain_key: bytes | str | None = "env"
) -> tuple[bool, str]:
    """校验归档：清单链完整 **且** 每个文件存在、sha256 与清单一致。"""
    directory = Path(directory)
    manifest = directory / MANIFEST_NAME
    if not manifest.is_file():
        return False, f"没有清单文件：{manifest}"
    key = _chain_key(chain_key)
    prev_hash = ""
    count = 0
    for index, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            return False, f"清单第 {index} 行不是合法 JSON——清单被改动过"
        seq = int(entry.get("seq", 0))
        stored_prev, stored_hash = str(entry.get("prev_hash", "")), str(entry.get("hash", ""))
        if stored_prev != prev_hash:
            return False, (
                f"清单链在 seq={seq} 处断开：prev_hash 与上一条的 hash 不一致"
                "（有清单记录被删/被改）"
            )
        expect = _manifest_hash(key, seq, prev_hash, entry)
        if expect != stored_hash:
            return False, f"清单 seq={seq} 的内容与它的哈希不符——这条清单被改动过"
        target = directory / str(entry.get("file", ""))
        if not target.is_file():
            return False, f"清单登记的文件不存在：{target}（归档文件被删除）"
        if _sha256_file(target) != str(entry.get("sha256", "")):
            return False, f"归档文件 {target.name} 的 sha256 与清单不符——文件被改动过"
        prev_hash = stored_hash
        count += 1
    return True, f"归档完整：{count} 个文件，清单链头 {prev_hash[:12]}…"
