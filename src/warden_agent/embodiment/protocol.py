"""总线帧格式：真机接入的第一道工程门槛，也是最容易被跳过的一段。

为什么不用 JSON/文本协议：串口上**粘包、半包、噪声字节**是常态，不是异常。
  文本协议要靠换行符找边界，一旦丢一个字节，后面整段流都被误解析，
  而且误解析出来的东西看起来"像"命令——这种错误最贵。
  固定帧头 + 长度 + 序号 + CRC，才能在**收到一半**的时候就知道"还差几个字节"，
  在**收到噪声**的时候立刻丢弃而不是继续猜。

帧格式（小端，除 CRC 外）::

    AA 55 | len(2) | seq(1) | kind(1) | payload(len) | crc16(2)

  · len 是 payload 长度（不含头尾），上限 64：超过就是流已经乱了，直接丢。
  · seq 由主机自增；设备回帧带**同一个 seq**，用来把回复和请求对上（否则超时重试时
    会把上一次的迟到回复当成这一次的）。
  · crc16 用 CCITT-FALSE（多项式 0x1021，初值 0xFFFF，不反转、不异或输出）——
    选它是因为几乎所有 MCU 的硬件 CRC 都能配成这一档，而不只是软件里算得对。

解析器是**流式**的：`feed()` 可以喂任意切分的字节块，返回这一刻能凑出的完整帧。
"""

from __future__ import annotations

import struct
from collections import Counter
from dataclasses import dataclass

SOF = b"\xAA\x55"
MAX_PAYLOAD = 64
HEADER = 6          # SOF(2) + len(2) + seq(1) + kind(1)
TRAILER = 2         # crc16

KIND_POLL = 0x01    # 主机 → 设备：要一帧状态
KIND_STATE = 0x02   # 设备 → 主机：状态
KIND_COMMAND = 0x03 # 主机 → 设备：下发
KIND_ACK = 0x04     # 设备 → 主机：确认收到下发


class FrameError(ValueError):
    """帧层面的错误（超长/CRC 错）。解析器内部消化，不往外抛——流不能因为一帧坏掉就停。"""


def crc16_ccitt(data: bytes) -> int:
    """CCITT-FALSE：poly 0x1021 / init 0xFFFF / 不反转 / 不 xorout。"""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


@dataclass(frozen=True, slots=True)
class Frame:
    seq: int
    kind: int
    payload: bytes = b""


def encode_frame(frame: Frame) -> bytes:
    payload = frame.payload
    if len(payload) > MAX_PAYLOAD:
        raise FrameError(f"payload {len(payload)} 超过上限 {MAX_PAYLOAD}：拒绝发出")
    body = struct.pack("<HBB", len(payload), frame.seq & 0xFF, frame.kind & 0xFF) + payload
    return SOF + body + struct.pack("<H", crc16_ccitt(body))


class FrameParser:
    """流式解帧。坏帧只计数、不抛异常——串口流不能因为一帧坏了就停。"""

    def __init__(self) -> None:
        self._buf = bytearray()
        self.errors: Counter[str] = Counter()
        self.frames = 0

    def feed(self, chunk: bytes) -> list[Frame]:
        self._buf.extend(chunk)
        out: list[Frame] = []
        while True:
            start = self._buf.find(SOF)
            if start < 0:
                # 没有帧头：只留最后一个字节，可能是被切开的 0xAA
                if len(self._buf) > 1:
                    self._buf = self._buf[-1:]
                break
            if start > 0:
                self.errors["garbage"] += 1
                del self._buf[:start]
            if len(self._buf) < HEADER:
                break
            (length,) = struct.unpack_from("<H", self._buf, 2)
            if length > MAX_PAYLOAD:
                self.errors["length"] += 1
                del self._buf[:1]      # 头部是假的：挪一个字节重新找帧头
                continue
            total = HEADER + length + TRAILER
            if len(self._buf) < total:
                break                   # 半包：等下一块
            body = bytes(self._buf[2:HEADER + length])
            (crc,) = struct.unpack_from("<H", self._buf, HEADER + length)
            if crc != crc16_ccitt(body):
                self.errors["crc"] += 1
                del self._buf[:1]
                continue
            # body = len(2) | seq(1) | kind(1) | payload —— 注意偏移是 2/3，不是 1/2。
            seq, kind = body[2], body[3]
            out.append(Frame(seq=seq, kind=kind, payload=body[4:]))
            self.frames += 1
            del self._buf[:total]
        return out


# ---- 载荷编解码：命令与状态都只有几个 float，用 struct 而不是 json ----

def encode_command(kind: int, values: tuple[float, ...]) -> bytes:
    if len(values) > MAX_PAYLOAD // 4 - 1:
        raise FrameError(f"关节数 {len(values)} 太多，一帧放不下")
    return struct.pack("<BB", kind, len(values)) + struct.pack(f"<{len(values)}f", *values)


def decode_command(payload: bytes) -> tuple[int, tuple[float, ...]]:
    if len(payload) < 2:
        raise FrameError(f"命令载荷太短：{len(payload)} 字节")
    kind, n = payload[0], payload[1]
    need = 2 + 4 * n
    if len(payload) != need:
        raise FrameError(f"命令载荷长度 {len(payload)} != 2 + 4×{n}")
    return kind, struct.unpack(f"<{n}f", payload[2:need])


def encode_state(t: float, positions: tuple[float, ...], velocities: tuple[float, ...],
                 torques: tuple[float, ...]) -> bytes:
    n = len(positions)
    head = struct.pack("<dBBB", t, n, len(velocities), len(torques))
    return head + struct.pack(f"<{n}f", *positions) + struct.pack(
        f"<{len(velocities)}f", *velocities) + struct.pack(f"<{len(torques)}f", *torques)


def decode_state(
    payload: bytes,
) -> tuple[float, tuple[float, ...], tuple[float, ...], tuple[float, ...]]:
    if len(payload) < 11:
        raise FrameError(f"状态载荷太短：{len(payload)} 字节")
    t, n, nv, nt = struct.unpack_from("<dBBB", payload, 0)
    expect = 11 + 4 * (n + nv + nt)
    if len(payload) != expect:
        raise FrameError(f"状态载荷长度 {len(payload)} != {expect}")
    off = 11

    def take(count: int) -> tuple[float, ...]:
        nonlocal off
        vals = struct.unpack_from(f"<{count}f", payload, off) if count else ()
        off += 4 * count
        return tuple(float(x) for x in vals)

    return t, take(n), take(nv), take(nt)
