"""串口适配器：真机接入层。**本机没有真机，所以这里证明的是协议与失败路径，不是电机。**

为什么值得单独一层（而不是在适配器里直接 `serial.write(b"1.5,0.0")`）：
  总线上的失败有自己的分类——丢帧、CRC 坏、半包、设备不回、回复迟到。它们的处置不同：
  丢帧可以重试，CRC 坏说明物理层有问题（重试可能还是坏），迟到回复必须靠 **seq 对号**排除，
  连续失败就必须**停**而不是继续等。把这些混成"读超时"一个异常，上真机后无法定位。

三条硬取向：
  1. **读失败必须抛异常**，绝不返回上一次的缓存——状态是"现在的它"，不是"刚才的它"。
     反射环收到异常后会自己进安全态，这是它唯一需要知道的语义。
  2. **回复按 seq 对号**：超时重试时，上一次的迟到回复会被当成这一次的，如果不比对 seq，
     控制器就会拿旧状态算新的力矩。
  3. **连续失败就放弃**：重试是给偶发丢帧用的，不是给"设备已经死了"用的。

`LoopbackDevice` 是同一套协议的另一端（假设备），可以注入丢帧/坏 CRC/故障——
  真实硬件到货之前，它让"失败路径"变成可回归的测试，而不是等上机才发现。
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from warden_agent.embodiment.protocol import (
    KIND_ACK,
    KIND_COMMAND,
    KIND_POLL,
    KIND_STATE,
    Frame,
    FrameError,
    FrameParser,
    decode_command,
    decode_state,
    encode_command,
    encode_frame,
    encode_state,
)
from warden_agent.embodiment.types import Command, CommandKind, DeviceState, JointLimits

# 命令种类在总线上用单字节表达，与 CommandKind 解耦：
# 协议是线上格式，枚举是 Python 内部表示，二者绑定会让"改枚举"变成"改协议"。
_KIND_CODE = {CommandKind.POSITIONS: 0, CommandKind.TORQUES: 1, CommandKind.IDLE: 2}


@runtime_checkable
class Transport(Protocol):
    """字节流传输。真实实现是 pyserial；测试实现是 `LoopbackTransport`。"""

    def read(self, size: int) -> bytes:
        """读最多 size 字节。**没有数据时返回 b""**（不是阻塞到天荒地老）。"""
        ...

    def write(self, data: bytes) -> None:
        """写出全部字节。失败必须抛异常。"""
        ...

    def flush_input(self) -> None:
        """丢弃接收缓冲里的残留（重连后必须做，否则会拿上次的尾巴当这次的开头）。"""
        ...

    def close(self) -> None:
        """关掉；幂等。"""
        ...


class LoopbackTransport:
    """把 `LoopbackDevice` 包成 `Transport`：write 进去、read 出来。"""

    def __init__(self, device: LoopbackDevice) -> None:
        self._device = device
        self._out = bytearray()
        self.closed = False

    def read(self, size: int) -> bytes:
        if self.closed:
            return b""
        chunk = bytes(self._out[:size])
        del self._out[:size]
        return chunk

    def write(self, data: bytes) -> None:
        if self.closed:
            raise OSError("传输已关闭")
        self._out.extend(self._device.handle(bytes(data)))

    def flush_input(self) -> None:
        self._out.clear()
        self._device.parser = FrameParser()

    def close(self) -> None:
        self.closed = True


@dataclass
class LoopbackDevice:
    """假设备：解帧 → 推进一点点动力学 → 回状态帧。

    参数不是"随机故障"，而是**可复现的故障注入**：丢第 k 帧、把第 j 帧的 CRC 写坏。
    这样"重试能不能救回来""CRC 坏会不会被当成数据"都能在 CI 里回归。
    """

    joint_count: int
    limits: JointLimits = field(default_factory=lambda: JointLimits(position=(-1.4, 1.4)))
    dt: float = 0.005
    damping: float = 0.8
    kp: float = 8.0            # 位置命令下的小 P 控制器：让"下发位置"真的能推动关节
    # 注：这是阻尼二阶系统，damping ≈ 2√kp 才临界阻尼；默认值偏小，会振铃（演示里能看见摆动）。
    drop_every: int = 0        # >0：每第 n 个收到的帧直接不回
    corrupt_every: int = 0     # >0：每第 n 个回帧把 CRC 写坏
    deaf: bool = False         # True：彻底不回（模拟设备死掉/线掉了）

    t: float = 0.0
    positions: tuple[float, ...] = ()
    velocities: tuple[float, ...] = ()
    torques: tuple[float, ...] = ()
    parser: FrameParser = field(default_factory=FrameParser)
    received: int = 0
    dropped: int = 0
    corrupted: int = 0
    last_command: tuple[int, tuple[float, ...]] | None = None
    target: tuple[float, ...] | None = None

    def __post_init__(self) -> None:
        if not self.positions:
            self.positions = tuple(0.0 for _ in range(self.joint_count))
            self.velocities = tuple(0.0 for _ in range(self.joint_count))
            self.torques = tuple(0.0 for _ in range(self.joint_count))

    def handle(self, chunk: bytes) -> bytes:
        """收到一段字节，返回要发回的字节（可能为空：丢帧/装死）。"""
        out = bytearray()
        for frame in self.parser.feed(chunk):
            self.received += 1
            if self.deaf or (self.drop_every and self.received % self.drop_every == 0):
                self.dropped += 1
                continue
            self._apply(frame)
            if frame.kind is KIND_POLL:
                state = encode_state(self.t, self.positions, self.velocities, self.torques)
                reply = encode_frame(Frame(seq=frame.seq, kind=KIND_STATE, payload=state))
                if self.corrupt_every and self.received % self.corrupt_every == 0:
                    self.corrupted += 1
                    reply = reply[:-1] + bytes([reply[-1] ^ 0xFF])   # 只坏 CRC，长度不变
                out.extend(reply)
            elif frame.kind is KIND_COMMAND:
                out.extend(encode_frame(Frame(seq=frame.seq, kind=KIND_ACK)))
        return bytes(out)

    def _apply(self, frame: Frame) -> None:
        self.t += self.dt
        if frame.kind is KIND_COMMAND and frame.payload:
            try:
                kind, values = decode_command(frame.payload)
            except FrameError:
                return
            self.last_command = (kind, values)
            full = len(values) == self.joint_count
            if kind == 0 and full:
                # POSITIONS：记住目标，由下面的 P 控制器出力
                self.target = values
            elif kind == 1 and full:      # TORQUES：直接给力矩，撤掉位置目标
                self.target = None
                self.torques = values
            elif kind == 2:                                        # IDLE：松劲
                self.target = None
                self.torques = tuple(0.0 for _ in range(self.joint_count))
        if self.target is not None:
            self.torques = tuple(
                self.kp * (goal - pos)
                for goal, pos in zip(self.target, self.positions, strict=True)
            )
        acc = tuple(
            tau - self.damping * v for tau, v in zip(self.torques, self.velocities, strict=True)
        )
        self.velocities = tuple(v + a * self.dt for v, a in zip(self.velocities, acc, strict=True))
        self.positions = tuple(
            p + v * self.dt for p, v in zip(self.positions, self.velocities, strict=True)
        )


@dataclass(frozen=True, slots=True)
class BusCounters:
    """总线体检：上真机后先看这几个数，比看日志猜快得多。"""

    sent: int
    received: int
    retries: int
    timeouts: int
    crc_errors: int

    def as_text(self) -> str:
        return (f"发 {self.sent} / 收 {self.received} / 重试 {self.retries} / "
                f"超时 {self.timeouts} / CRC 坏 {self.crc_errors}")


class SerialAdapter:
    """实现 `DeviceAdapter` 契约的串口设备。"""

    def __init__(
        self,
        transport: Transport,
        joint_count: int,
        *,
        limits: JointLimits | None = None,
        timeout: float = 0.05,
        max_retries: int = 2,
        clock: Callable[[], float] = time.monotonic,
        wait_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if joint_count <= 0:
            raise ValueError(f"joint_count 必须为正，得到 {joint_count!r}")
        if timeout <= 0:
            raise ValueError(f"timeout 必须为正，得到 {timeout!r}")
        if max_retries < 0:
            raise ValueError(f"max_retries 不能为负，得到 {max_retries!r}")
        self._transport = transport
        self._joints = joint_count
        self._limits = limits or JointLimits(position=(-1.4, 1.4))
        self._timeout = timeout
        self._max_retries = max_retries
        self._clock = clock
        # 超时必须用**墙钟**：设备时钟可能因为"设备不回帧"而完全停住，
        # 拿它当 deadline 会让"超时"永远不到（这个坑在 --serial 演示里真踩到了，见 CHANGELOG）。
        self._wait_clock = wait_clock
        self._parser = FrameParser()
        self._seq = 0
        self._sent = 0
        self._recv = 0
        self._retries = 0
        self._timeouts = 0
        self._last_t: float | None = None
        self._last_positions: tuple[float, ...] = ()
        self._connected = False

    # ---- 契约 ----
    @property
    def joint_count(self) -> int:
        return self._joints

    @property
    def limits(self) -> JointLimits:
        return self._limits

    @property
    def positions(self) -> tuple[float, ...]:
        """最后一次读到的关节位置。**只是缓存给展示用**，控制决策不许读它——那正是"拿旧状态算新力矩"。"""
        return self._last_positions

    def now(self) -> float:
        """时间基准：设备回帧带的是**设备时钟**，看门狗必须用它，不能用主机墙钟。

        是**方法**而不是属性——与 `SimAdapter.now()` 一致，可以直接 `clock=adapter.now` 传给反射环。
        """
        return self._last_t if self._last_t is not None else self._clock()

    @property
    def counters(self) -> BusCounters:
        return BusCounters(
            sent=self._sent,
            received=self._recv,
            retries=self._retries,
            timeouts=self._timeouts,
            crc_errors=self._parser.errors["crc"],
        )

    def connect(self) -> None:
        """握手：必须拿到一帧状态才算连上。**不许"连上了但没通"**。"""
        self._transport.flush_input()
        self._parser = FrameParser()
        self._connected = True
        try:
            self.read_state()
        except OSError as exc:
            self._connected = False
            raise ConnectionError(f"握手失败：设备没有回状态帧（{exc}）") from exc

    def read_state(self) -> DeviceState:
        last_error = "没有收到任何字节"
        for attempt in range(self._max_retries + 1):
            if attempt:
                self._retries += 1
            seq = self._next_seq()
            self._write(Frame(seq=seq, kind=KIND_POLL))
            deadline = self._wait_clock() + self._timeout
            while self._wait_clock() < deadline:
                chunk = self._transport.read(4096)
                if not chunk:
                    continue
                for frame in self._parser.feed(chunk):
                    if frame.kind is not KIND_STATE:
                        continue
                    if frame.seq != seq:
                        # 迟到的上一帧：必须丢。拿它算力矩等于用"刚才的姿态"控制"现在"。
                        self._parser.errors["stale_seq"] += 1
                        continue
                    t, positions, velocities, torques = decode_state(frame.payload)
                    self._recv += 1
                    self._last_t = t
                    self._last_positions = positions
                    if len(positions) != self._joints:
                        raise OSError(
                            f"设备报 {len(positions)} 个关节，适配器按 {self._joints} 装配"
                        )
                    return DeviceState(
                        t=t, positions=positions, velocities=velocities, torques=torques,
                        connected=True,
                    )
                last_error = "对号成功但没有状态帧（可能全是 CRC 坏帧）"
        self._timeouts += 1
        raise OSError(
            f"读状态失败（重试 {self._max_retries} 次）：{last_error}；"
            f"总线计数 {self.counters.as_text()}"
        )

    def send(self, command: Command) -> None:
        if command.kind is CommandKind.IDLE:
            payload = encode_command(_KIND_CODE[CommandKind.IDLE], ())
        else:
            payload = encode_command(_KIND_CODE[command.kind], tuple(command.target))
        self._write(Frame(seq=self._next_seq(), kind=KIND_COMMAND, payload=payload))

    def safe_state(self) -> None:
        """力矩归零。**不得抛异常**：它常常是在异常处理里被调用的。"""
        # 安全态失败也只能咽下去：它常被放在 except 里，再抛会盖住原始异常。
        with contextlib.suppress(Exception):
            self._write(
                Frame(
                    seq=self._next_seq(),
                    kind=KIND_COMMAND,
                    payload=encode_command(_KIND_CODE[CommandKind.IDLE], ()),
                )
            )

    def close(self) -> None:
        self._connected = False
        with contextlib.suppress(Exception):
            self._transport.close()

    # ---- 内部 ----
    def _next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0xFF
        return self._seq

    def _write(self, frame: Frame) -> None:
        self._transport.write(encode_frame(frame))
        self._sent += 1


class PyserialTransport:
    """真串口（pyserial）。**本机没有串口，所以这条路径没有被真机验证过。**

    它只保证两件事：① 没装 pyserial 时报一条"照着做就能解决"的错，而不是 ImportError；
    ② 装了之后参数与适配器的假设一致——特别是 `timeout=0`：
    适配器自己用 deadline 轮询，驱动层再阻塞就会让"超时"变成"卡住"。
    """

    def __init__(self, port: str, *, baudrate: int = 115200, read_timeout: float = 0.0) -> None:
        try:
            # 可选依赖：不装也能用 Loopback 跑完所有测试；也不需要为它装 stubs。
            import serial  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - 只在缺依赖时触发
            raise RuntimeError(
                "要连真串口需要 pyserial：python -m pip install pyserial；"
                "不装也能用 --serial 走环回设备（LoopbackDevice）跑完整条链路"
            ) from exc
        self._serial = serial.Serial(
            port=port, baudrate=baudrate, timeout=read_timeout, write_timeout=1.0
        )

    def read(self, size: int) -> bytes:
        return self._serial.read(size) or b""

    def write(self, data: bytes) -> None:
        self._serial.write(data)
        self._serial.flush()

    def flush_input(self) -> None:
        self._serial.reset_input_buffer()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._serial.close()
