"""`store/codec.py` 版本化注册表的错误分支：缺失版本必须**显式失败**，不能静默。"""

from __future__ import annotations

import pytest

from warden_agent.store.codec import JsonCodec, VersionedCodecRegistry


def test_空注册表取最新版本_抛KeyError() -> None:
    with pytest.raises(KeyError):
        VersionedCodecRegistry([]).latest_version()


def test_编码未注册版本_抛KeyError() -> None:
    reg = VersionedCodecRegistry([JsonCodec()])
    with pytest.raises(KeyError):
        reg.encode(999, {"a": 1})


def test_解码未注册版本_抛KeyError() -> None:
    reg = VersionedCodecRegistry([JsonCodec()])
    with pytest.raises(KeyError):
        reg.decode(999, "{}")


def test_默认版本编码_往返一致() -> None:
    reg = VersionedCodecRegistry([JsonCodec()])
    version, encoded = reg.encode(None, {"a": 1})
    assert version == 1
    assert reg.decode(version, encoded) == {"a": 1}
