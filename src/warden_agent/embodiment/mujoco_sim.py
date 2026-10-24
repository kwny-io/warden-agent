"""MuJoCo 适配器：把同一套裁决/反射环接到**真物理**上（重力、接触、阻尼都在）。

为什么需要它：`SimAdapter` 是手写的二阶系统，没有重力、没有接触，它只能证明管线通。
真机之前最接近真实的一步是**物理引擎**——参数写错、PD 增益不对、限位设反，在这里都会
以"掉下去/发散/撞穿地面"的形式暴露，而不会被一个理想化的模型掩盖。

它同样是 `DeviceAdapter`：控制律、裁决、工具卡、审计一行都不用改，换的只是这一层。

诚实的边界（写在代码里而不是只写在文档里）：
- 它**不**做 sim2real 标定，也**不**证明真机能站稳：模型里的质量/摩擦/电机常数都是编的。
- 它**不**做接触力/碰撞检测上报：`DeviceState.fault` 只反映"数值发散"这一种物理故障。
- `mujoco` 是**可选依赖**：没装就不该 import 本模块（`__init__` 不导出它）。

时钟口径：`DeviceState.t` 用 `time.monotonic`（与真机一致）；物理时间由 `mj_step` 推进，
两者不混用——把仿真时间当看门狗时间会让"状态过期"永远不触发。
"""

from __future__ import annotations

import math
import time
from typing import Any

from warden_agent.embodiment.types import Command, CommandKind, DeviceState, JointLimits

__all__ = ["MujocoAdapter"]


def _model_xml(joint_count: int) -> str:
    """一条平面多关节臂 + 地面。每节 0.2m，够看出"重力把它压下去"和"PD 把它顶起来"。"""
    bodies: list[str] = []
    joints: list[str] = []
    motors: list[str] = []
    for i in range(joint_count):
        pos = "0 0 0.25" if i == 0 else "0.2 0 0"
        bodies.append(
            f'<body name="l{i}" pos="{pos}">'
            f'<joint name="j{i}" type="hinge" axis="0 1 0" range="-1.4 1.4" damping="0.08"/>'
            f'<geom type="capsule" fromto="0 0 0 0.2 0 0" size="0.028" mass="0.4"/>'
        )
        joints.append(f"j{i}")
        motors.append(f'<motor joint="j{i}" ctrlrange="-20 20"/>')
    close = "</body>" * joint_count
    inner = "".join(bodies) + close
    return (
        # `angle="radian"` 不是可选优化：MuJoCo 默认按**度**解释 `range`，
        # 少了这一行 `range="-1.4 1.4"` 会被读成 ±1.4°，限位比噪声还窄（实测 ±0.0244 rad）。
        '<mujoco model="warden-arm">'
        '<compiler angle="radian" autolimits="true"/>'
        '<option timestep="0.002" gravity="0 0 -9.81" integrator="implicitfast"/>'
        f'<worldbody><geom name="floor" type="plane" size="3 3 0.1"/>{inner}</worldbody>'
        f'<actuator>{"".join(motors)}</actuator>'
        "</mujoco>"
    )


class MujocoAdapter:
    """真物理适配器。**每读一次状态推进一个 `dt`**，与 `SimAdapter` 同一套语义。"""

    def __init__(
        self,
        joint_count: int = 3,
        *,
        dt: float = 0.005,
        position_kp: float = 8.0,
        position_kd: float = 0.6,
        clock: Any = time.monotonic,
    ) -> None:
        if joint_count <= 0:
            raise ValueError(f"joint_count 必须为正，得到 {joint_count!r}")
        if dt <= 0:
            raise ValueError(f"dt 必须为正，得到 {dt!r}")
        import mujoco  # 可选依赖：没装就在这里报清楚

        self._mujoco = mujoco
        self._model = mujoco.MjModel.from_xml_string(_model_xml(joint_count))
        self._data = mujoco.MjData(self._model)
        self._joints = joint_count
        self._dt = dt
        self._kp = position_kp
        self._kd = position_kd
        self._clock = clock
        self._connected = False
        self._fault: str | None = None
        self.sent = 0
        self.safe_calls = 0
        self.steps = 0

    # ---- DeviceAdapter ----
    @property
    def joint_count(self) -> int:
        return self._joints

    def connect(self) -> None:
        self._connected = True
        self._fault = None

    def read_state(self) -> DeviceState:
        if not self._connected:
            raise RuntimeError("MujocoAdapter 未连接：read_state 不许返回缓存")
        self._advance(self._dt)
        qpos = tuple(float(self._data.qpos[i]) for i in range(self._joints))
        qvel = tuple(float(self._data.qvel[i]) for i in range(self._joints))
        if not all(math.isfinite(x) for x in qpos):
            self._fault = "物理数值发散（qpos 出现 NaN/Inf）"
        return DeviceState(
            t=self._clock(),
            positions=qpos,
            velocities=qvel,
            torques=tuple(float(self._data.ctrl[i]) for i in range(self._joints)),
            connected=True,
            fault=self._fault,
        )

    def send(self, command: Command) -> None:
        if not self._connected:
            raise RuntimeError("MujocoAdapter 未连接：不许下发")
        self.sent += 1
        if command.kind is CommandKind.IDLE:
            self._torques([0.0] * self._joints)
            return
        if command.kind is CommandKind.TORQUES:
            self._torques([float(x) for x in command.target])
            return
        # 位置模式：折成一次 PD 力矩（真机的伺服盒子就是这么做的）
        tau = []
        for i, goal in enumerate(command.target):
            q = float(self._data.qpos[i])
            v = float(self._data.qvel[i])
            tau.append(self._kp * (goal - q) - self._kd * v)
        self._torques(tau)

    def safe_state(self) -> None:
        """安全态 = 力矩归零（重力仍在，所以这是"松劲"，不是"悬停"）。幂等、不抛异常。"""
        self.safe_calls += 1
        self._torques([0.0] * self._joints)

    def close(self) -> None:
        self._connected = False

    # ---- 测试/演示用 ----
    def advance(self, seconds: float) -> None:
        self._advance(seconds)

    @property
    def positions(self) -> tuple[float, ...]:
        return tuple(float(self._data.qpos[i]) for i in range(self._joints))

    def now(self) -> float:
        """实时步进：设备时钟就是墙钟（`DeviceState.t` 同源，看门狗用它才一致）。"""
        return time.monotonic()

    @property
    def limits(self) -> JointLimits:
        lo = min(float(self._model.jnt_range[i][0]) for i in range(self._joints))
        hi = max(float(self._model.jnt_range[i][1]) for i in range(self._joints))
        return JointLimits(position=(lo, hi))

    def _torques(self, tau: list[float]) -> None:
        for i, value in enumerate(tau[: self._joints]):
            self._data.ctrl[i] = value

    def _advance(self, seconds: float) -> None:
        step = float(self._model.opt.timestep)
        count = max(1, int(round(seconds / step)))
        for _ in range(count):
            self._mujoco.mj_step(self._model, self._data)
        self.steps += count
