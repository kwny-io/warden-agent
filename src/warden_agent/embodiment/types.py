"""具身接入层的公共类型：命令 / 状态 / 限位 / 裁决 / 事件。

为什么"命令"和"裁决"要分开（而不是让 `send()` 直接抛异常）：
  物理世界里**被拒绝的命令本身是证据**——事故复盘要回答"当时谁下了什么、为什么没执行"。
  抛异常会把这条证据压成一行栈，落不进审计链；所以拒绝走 `Verdict`，调用方必须显式处理。
  这条取向与 `policy/` 的 ALLOW/ASK/DENY 一致：**"没做"和"做了"一样要留痕**。

为什么 `DeviceState` 带时间戳：看门狗判的是"这条状态有多旧"，不是"它看起来对不对"。
  没有时间戳的看门狗只能靠调用频率推断，而那正是多副本/卡顿下最先失真的东西。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum


class CommandKind(StrEnum):
    """命令种类。只保留三类：位置目标、力矩、停住（idle=松劲/保持由适配器定义）。"""

    POSITIONS = "positions"
    TORQUES = "torques"
    IDLE = "idle"


class Code(StrEnum):
    """裁决码。**机器可判**（测试断言的锚），`detail` 才给人读。"""

    OK = "ok"
    SHAPE = "shape"                 # 关节数不对 / 目标为空
    NAN = "nan"                     # 非有限值
    LIMIT = "limit"                 # 超出位置/速度/力矩限
    STEP = "step"                   # 单 tick 目标跳变过大
    RATE = "rate"                   # 命令频率超限
    DUP = "dup"                     # request_id 重复（幂等：同一件事不执行第二次）
    ESTOP = "estop"                 # 急停已锁存
    STALE = "stale"                 # 状态过期（看门狗）
    DISCONNECTED = "disconnected"   # 适配器报告断连
    FAULT = "fault"                 # 适配器报告故障


@dataclass(frozen=True, slots=True)
class JointLimits:
    """同构关节的限位（先按"每个关节同一套限位"建模；异构留给下一批）。

    限位不是建议，是**拒绝的理由来源**：`max_step` 防的是"上位机一次下发 180° 跳变"，
    那在真机上等于让电机瞬间出力打到位——机械结构会先坏，代码不会。
    """

    position: tuple[float, float]
    max_speed: float = 3.0
    max_torque: float = 20.0
    max_step: float = 0.15

    def __post_init__(self) -> None:
        lo, hi = self.position
        if not (lo < hi):
            raise ValueError(f"限位区间必须 lo < hi，得到 {self.position!r}")
        for name in ("max_speed", "max_torque", "max_step"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} 必须为正，得到 {getattr(self, name)!r}")

    def clamp(self, target: Sequence[float]) -> tuple[float, ...]:
        lo, hi = self.position
        return tuple(min(hi, max(lo, float(x))) for x in target)

    def axis_violation(self, target: Sequence[float]) -> str | None:
        """方向无关的越界描述（"贴边"不算越界：夹到边界是合法目标）。"""
        lo, hi = self.position
        for i, x in enumerate(target):
            if x < lo - 1e-9 or x > hi + 1e-9:
                return f"关节 {i} 目标 {x:.4f} 超出 [{lo}, {hi}]"
        return None


@dataclass(frozen=True, slots=True)
class Command:
    """一条待执行命令。`why` 与 `request_id` 是为审计与幂等留下的钩子。"""

    kind: CommandKind
    target: tuple[float, ...] = ()
    source: str = "agent"
    why: str = ""
    request_id: str = ""


@dataclass(frozen=True, slots=True)
class DeviceState:
    """适配器读回的状态。`t` 是**单调时钟秒数**，看门狗只信它。"""

    t: float
    positions: tuple[float, ...]
    velocities: tuple[float, ...] = ()
    torques: tuple[float, ...] = ()
    connected: bool = True
    fault: str | None = None


@dataclass(frozen=True, slots=True)
class Verdict:
    """一次裁决：放行或拒绝，附机器可判的码与人可读的理由。"""

    allowed: bool
    code: Code
    detail: str = ""
    target: tuple[float, ...] = ()

    @property
    def denied(self) -> bool:
        return not self.allowed


@dataclass(frozen=True, slots=True)
class Event:
    """发往下游（审计链 / SSE 时间线 / 遥测）的一条结构化事实。"""

    t: float
    kind: str
    code: str
    detail: str = ""
    source: str = ""
    request_id: str = ""
