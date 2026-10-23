"""串口适配器的失败路径：**没有真机时，这层就是唯一能证明"坏总线不会骗控制器"的东西。**

每条用例对应一种真机上会遇到的总线故障，而不是"覆盖率凑数"：
  粘包/半包/噪声、CRC 坏、设备装死、丢帧、迟到回复。
"""

from __future__ import annotations

import struct

import pytest

from warden_agent.embodiment.protocol import (
    KIND_ACK,
    KIND_COMMAND,
    KIND_POLL,
    KIND_STATE,
    Frame,
    FrameError,
    FrameParser,
    crc16_ccitt,
    decode_command,
    decode_state,
    encode_command,
    encode_frame,
    encode_state,
)
from warden_agent.embodiment.serial_adapter import (
    LoopbackDevice,
    LoopbackTransport,
    SerialAdapter,
)
from warden_agent.embodiment.types import Command, CommandKind


class FakeTransport:
    """把字节按脚本喂回来：用来测"读到一半""读到旧帧"这类时序问题。"""

    def __init__(self, *chunks: bytes) -> None:
        self._chunks = list(chunks)
        self.written: list[bytes] = []
        self.closed = False

    def read(self, size: int) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)[:size]

    def write(self, data: bytes) -> None:
        self.written.append(bytes(data))

    def flush_input(self) -> None:
        self._chunks.clear()

    def close(self) -> None:
        self.closed = True


def _state_frame(seq: int, t: float = 1.0, n: int = 2) -> bytes:
    return encode_frame(
        Frame(seq=seq, kind=KIND_STATE,
              payload=encode_state(t, tuple(0.1 for _ in range(n)),
                                   tuple(0.0 for _ in range(n)), tuple(0.0 for _ in range(n))))
    )


def test_crc16_ccitt_用标准校验值() -> None:
    # CRC-16/CCITT-FALSE 的 check 值是 0x29B1：写死它，防止"改完还是自洽但别处不认"。
    assert crc16_ccitt(b"123456789") == 0x29B1


def test_帧编解码往返() -> None:
    raw = encode_frame(Frame(seq=9, kind=KIND_COMMAND, payload=b"abc"))
    frames = FrameParser().feed(raw)
    assert [(f.seq, f.kind, f.payload) for f in frames] == [(9, KIND_COMMAND, b"abc")]


def test_解析器能吃半包() -> None:
    raw = encode_frame(Frame(seq=1, kind=KIND_POLL))
    parser = FrameParser()
    assert parser.feed(raw[:3]) == []
    assert parser.feed(raw[3:7]) == []
    assert [f.seq for f in parser.feed(raw[7:])] == [1]


def test_解析器丢掉噪声并继续解帧() -> None:
    raw = encode_frame(Frame(seq=2, kind=KIND_POLL))
    parser = FrameParser()
    frames = parser.feed(b"\x00\x13\x37" + raw)
    assert [f.seq for f in frames] == [2]
    assert parser.errors["garbage"] == 1


def test_超长长度字段被丢弃且不中断流() -> None:
    bogus = struct.pack("<H", 999) + b"\x00\x01"
    parser = FrameParser()
    assert parser.feed(b"\xAA\x55" + bogus) == []
    assert parser.errors["length"] == 1
    # 坏帧之后的好帧仍然解得出来：一帧坏了不能让整条流停摆。
    assert [f.seq for f in parser.feed(encode_frame(Frame(seq=5, kind=KIND_POLL)))] == [5]


def test_crc坏帧被计数且不出帧() -> None:
    payload = encode_state(0.0, (0.0,), (0.0,), (0.0,))
    raw = bytearray(encode_frame(Frame(seq=3, kind=KIND_STATE, payload=payload)))
    raw[-1] ^= 0xFF
    parser = FrameParser()
    assert parser.feed(bytes(raw)) == []
    assert parser.errors["crc"] == 1


def test_状态载荷往返() -> None:
    payload = encode_state(12.5, (0.25, -0.5), (1.0, 2.0), (0.0, 0.0))
    t, pos, vel, tau = decode_state(payload)
    assert t == pytest.approx(12.5)
    assert pos == pytest.approx((0.25, -0.5))
    assert vel == pytest.approx((1.0, 2.0))
    assert tau == pytest.approx((0.0, 0.0))


def test_命令载荷往返与长度校验() -> None:
    payload = encode_command(1, (3.0, -2.0))
    assert decode_command(payload) == (1, pytest.approx((3.0, -2.0)))
    with pytest.raises(FrameError):
        decode_command(payload + b"\x00")


def test_健康设备能连上并读到状态() -> None:
    device = LoopbackDevice(3)
    adapter = SerialAdapter(LoopbackTransport(device), 3)
    adapter.connect()
    state = adapter.read_state()
    assert state.connected is True
    assert len(state.positions) == 3
    assert adapter.counters.sent >= 2
    assert adapter.counters.received == 2


def test_装死设备握手必须失败() -> None:
    # 不是"连上了但读不到"，而是**连不上**：上层据此决定不进控制环。
    adapter = SerialAdapter(LoopbackTransport(LoopbackDevice(2, deaf=True)), 2,
                            timeout=0.001, max_retries=1)
    with pytest.raises(ConnectionError, match="握手失败"):
        adapter.connect()
    assert adapter.counters.timeouts == 1


def test_丢帧能靠重试救回来() -> None:
    device = LoopbackDevice(2, drop_every=2)
    device.received = 1          # 让第一帧落在"被丢"的那一格
    adapter = SerialAdapter(LoopbackTransport(device), 2, timeout=0.001, max_retries=3)
    adapter.connect()
    assert adapter.counters.retries >= 1


def test_一直丢帧就报错并带上总线计数() -> None:
    adapter = SerialAdapter(LoopbackTransport(LoopbackDevice(2, drop_every=1)), 2,
                            timeout=0.001, max_retries=2)
    adapter._connected = True   # 跳过握手，直接测读状态
    with pytest.raises(OSError, match="总线计数"):
        adapter.read_state()
    assert adapter.counters.timeouts == 1


def test_迟到的回复按seq丢掉() -> None:
    # 上一次超时的回复现在才到：不比对 seq 就会拿旧状态算新力矩。
    stale = _state_frame(seq=77, t=99.0)
    good = _state_frame(seq=1, t=1.0)
    adapter = SerialAdapter(FakeTransport(stale + good), 2, timeout=0.05)
    adapter._connected = True
    state = adapter.read_state()
    assert state.t == pytest.approx(1.0)
    assert adapter._parser.errors["stale_seq"] == 1


def test_关节数不符要直接报错() -> None:
    adapter = SerialAdapter(FakeTransport(_state_frame(seq=1, n=4)), 2, timeout=0.05)
    adapter._connected = True
    with pytest.raises(OSError, match="4 个关节"):
        adapter.read_state()


def test_下发命令会真的到达设备() -> None:
    device = LoopbackDevice(2)
    adapter = SerialAdapter(LoopbackTransport(device), 2)
    adapter.connect()
    adapter.send(Command(CommandKind.TORQUES, (2.0, -1.0)))
    assert device.last_command is not None
    assert device.last_command[0] == 1
    assert device.last_command[1] == pytest.approx((2.0, -1.0))


def test_安全态在传输已关时也不抛() -> None:
    transport = FakeTransport()
    adapter = SerialAdapter(transport, 2)
    adapter.close()
    transport.closed = True
    adapter.safe_state()        # 收尾路径不许抛：它常在 except 里被调用
    adapter.close()             # 幂等


def test_now跟着设备时钟走() -> None:
    # 反射环的看门狗必须用设备时间轴；拿主机墙钟会立刻判 stale。
    device = LoopbackDevice(2)
    adapter = SerialAdapter(LoopbackTransport(device), 2, clock=lambda: 1e6)
    adapter.connect()
    state = adapter.read_state()
    assert adapter.now() == pytest.approx(state.t)


def test_位置命令真的把关节推过去() -> None:
    # 环回设备在小 P 控制器下把"下发位置"变成力矩：否则 --serial 演示只是发帧不发动作。
    # damping ≈ 2√kp 才是临界阻尼：默认 0.8 会振铃（实测 300 拍在 0.19~0.69 之间来回摆）。
    device = LoopbackDevice(2, kp=20.0, damping=9.0)
    adapter = SerialAdapter(LoopbackTransport(device), 2)
    adapter.connect()
    adapter.send(Command(CommandKind.POSITIONS, (0.4, 0.0)))
    for _ in range(300):
        adapter.read_state()
    assert adapter.positions[0] == pytest.approx(0.4, abs=0.03)


def test_没有pyserial时报一条能照做的错(monkeypatch) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "serial", None)      # 让 import serial 抛 ImportError
    from warden_agent.embodiment.serial_adapter import PyserialTransport

    with pytest.raises(RuntimeError, match="pip install pyserial"):
        PyserialTransport("COM3")


def test_ack帧不被当成状态帧() -> None:
    ack = encode_frame(Frame(seq=1, kind=KIND_ACK))
    adapter = SerialAdapter(FakeTransport(ack), 2, timeout=0.02)
    adapter._connected = True
    with pytest.raises(OSError):
        adapter.read_state()
