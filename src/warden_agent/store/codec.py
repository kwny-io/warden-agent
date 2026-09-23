"""版本化 Payload Codec —— 让"存进去的每一条数据"都自带版本号。

  - 数据库 schema 的版本由各存储自身的 `__schema_version__` 记录（见 sqlite.py / postgres.py 的
    `_init_migrations` / `_init_schema`）；
  - PayloadCodec 管的是"表里某一行内容"的版本。
  - 同一个表里的老行（旧结构）和新行（新结构）可以共存，各自按自己的版本去读。

为什么要分两套版本：
  表结构迁移是一次性的（ALTER TABLE 把整列升级）。
  但"内容格式"往往改不动老数据——比如 messages.content 从纯文本变成 JSON，
  你没法用一条 DDL 把历史所有行都改掉。这时给每条数据标个 version，
  读的时候按版本选 codec 解码，老数据永不为难你。

设计：
  - 写：encode(payload) -> (version, bytes)
  - 读：按版本从注册表选出 codec 解码
  - 每新增一种格式，加一个更高版本号的 codec，历史 codec 只读不改。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, TypeVar

T = TypeVar("T")


class Codec(Protocol[T]):
    """一种"内容格式"的编解码器。version 越高越新。"""

    version: int

    def encode(self, payload: T) -> str: ...
    def decode(self, data: str) -> T: ...


@dataclass(frozen=True)
class Wrapped:
    """读出来的东西：原始内容 + 它当初是哪个版本写的。"""

    version: int
    payload: object


class JsonCodec(Codec[object]):
    """v1：最朴素的 JSON 编解码（现在 messages.tool_call / arguments 就是这种）。"""

    version = 1

    def encode(self, payload: object) -> str:
        return json.dumps(payload, ensure_ascii=False)

    def decode(self, data: str) -> object:
        return json.loads(data)


class VersionedCodecRegistry:
    """按版本号挑 codec 的注册表。命令：写用最新，读按历史版本回退。"""

    def __init__(self, codecs: list[Codec[object]] | None = None) -> None:
        self._by_version: dict[int, Codec[object]] = {}
        for c in codecs or []:
            self.register(c)

    def register(self, codec: Codec[object]) -> None:
        self._by_version[codec.version] = codec

    def latest_version(self) -> int:
        if not self._by_version:
            raise KeyError("未注册任何 codec")
        return max(self._by_version)

    def encode(self, version: int | None, payload: object) -> tuple[int, str]:
        """按指定版本（默认最新）编码，返回 (版本号, 内容)。"""
        v = version if version is not None else self.latest_version()
        codec = self._by_version.get(v)
        if codec is None:
            raise KeyError(f"没有注册版本 {v} 的 codec")
        return v, codec.encode(payload)

    def decode(self, version: int, data: str) -> object:
        """按版本号解码。老版本数据用老 codec 读，类型安全。"""
        codec = self._by_version.get(version)
        if codec is None:
            raise KeyError(f"没有注册版本 {version} 的 codec，无法解码历史数据")
        return codec.decode(data)


DEFAULT_CODEC_REGISTRY = VersionedCodecRegistry([JsonCodec()])

# ---------------------------------------------------------------------------
# 跨后端统一的版本化落盘格式
# ---------------------------------------------------------------------------
#
# 为什么要有这几个自由函数（而不是各存储各写一遍）：
#   同一个 payload（tool_call / 待审批 arguments / checkpoint）可能先写进 SQLite、
#   后来系统切到 PostgreSQL（或反之）。只要两边的落盘格式必须逐字节一致，
#   就必须共用同一段编码逻辑——否则一边带 `v{n}:` 版本前缀、一边裸 JSON，
#   跨库读回时版本契约就丢了。
#
# 格式：`v{版本号}:{codec 编码后的内容}`。无前缀的历史数据按 v1 兜底。


def encode_versioned(
    payload: object, registry: VersionedCodecRegistry | None = None
) -> str:
    """按注册表最新版本编码成 `v{n}:{内容}`。两个后端共用同一格式。"""
    reg = registry if registry is not None else DEFAULT_CODEC_REGISTRY
    ver, encoded = reg.encode(None, payload)
    return f"v{ver}:{encoded}"


def split_versioned(raw: str) -> tuple[int, str]:
    """拆分 `v{n}:` 版本前缀；没有前缀（老数据）按 v1 兜底。"""
    if raw.startswith("v") and ":" in raw:
        head, _, body = raw.partition(":")
        if head[1:].isdigit():
            return int(head[1:]), body
    return 1, raw


def decode_versioned(
    raw: str, registry: VersionedCodecRegistry | None = None
) -> object:
    """按版本前缀解码。未知版本抛 `KeyError`，由调用方决定兜底策略。"""
    reg = registry if registry is not None else DEFAULT_CODEC_REGISTRY
    ver, data = split_versioned(raw)
    return reg.decode(ver, data)


# ---------------------------------------------------------------------------
# 时间归一化
# ---------------------------------------------------------------------------
#
# 不变量：所有保留/过期时间比较，都必须在"UTC 感知"的同一基准上进行。
# 只要生产方可能写进 naive datetime（无 tzinfo），字符串比较就会因缺少偏移量而
# 误判（例如 `2026-01-01T00:00:00` 与 `2026-01-01T00:00:00+00:00`）。
# 因此比较前一律先用本函数归一化，两个后端共用。


def normalize_utc_iso(value: str | datetime) -> str:
    """把 ISO 字符串 / datetime 统一成 UTC-aware 的 ISO 字符串。"""
    moment = datetime.fromisoformat(value) if isinstance(value, str) else value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()
