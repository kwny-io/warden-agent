"""版本化 Payload Codec —— 让"存进去的每一条数据"都自带版本号。

  - 数据库迁移（migrations.py）管的是"表结构"的版本；
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
