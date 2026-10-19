"""确定性仿真底盘：每个关节一个带阻尼的二阶系统，**零外部依赖**。

为什么不直接依赖 MuJoCo：CI 里装不动、也不该装。这一层的用途是让
"反射环 + 安全监督 + 幂等 + 审计"这些**不依赖物理保真度**的部分可以被确定性地测。
真机/高保真仿真接进来只换适配器（`DeviceAdapter`），控制律与安全层一行不改。

一个刻意保留的缺陷（写在这里而不是藏起来）：这个模型**没有重力、没有摩擦、没有延迟**，
所以它稳不代表真机稳。它证明的是"闭环管线通"，不是"机器人站得住"。
"""

from __future__ import annotations

from warden_agent.embodiment.types import Command, CommandKind, DeviceState, JointLimits


class SimAdapter:
    """仿真臂。时间由 `now()` 给出，**与反射环共用同一个时钟**（见 `reflex` 的时钟口径）。"""

    def __init__(
        self,
        joint_count: int = 12,
        *,
        dt: float = 0.005,
        damping: float = 1.2,
        gravity: float = 0.0,
        limits: JointLimits | None = None,
        position_kp: float = 20.0,
    ) -> None:
        if joint_count <= 0:
            raise ValueError(f"joint_count 必须为正，得到 {joint_count!r}")
        if dt <= 0:
            raise ValueError(f"dt 必须为正，得到 {dt!r}")
        self._joints = joint_count
        self._dt = dt
        self._damping = damping
        self._gravity = gravity
        self._limits = limits or JointLimits(position=(-2.6, 2.6))
        self._position_kp = position_kp
        self._t = 0.0
        self._positions = [0.0] * joint_count
        self._velocities = [0.0] * joint_count
        self._torques = [0.0] * joint_count
        self._connected = False
        self._fault: str | None = None
        self._torque_only = True
        self.sent = 0
        self.safe_calls = 0
        self.last_command: Command | None = None

    # ---- DeviceAdapter ----
    @property
    def joint_count(self) -> int:
        return self._joints

    def now(self) -> float:
        return self._t

    def connect(self) -> None:
        self._connected = True
        self._fault = None

    def read_state(self) -> DeviceState:
        if not self._connected:
            raise RuntimeError("SimAdapter 未连接：read_state 不许返回缓存")
        self._integrate(self._dt)
        return DeviceState(
            t=self._t,
            positions=tuple(self._positions),
            velocities=tuple(self._velocities),
            torques=tuple(self._torques),
            connected=True,
            fault=self._fault,
        )

    def send(self, command: Command) -> None:
        if not self._connected:
            raise RuntimeError("SimAdapter 未连接：不许下发")
        self.sent += 1
        self.last_command = command
        if command.kind is CommandKind.IDLE:
            self._torque_only = False
            self._torques = [0.0] * self._joints
            return
        self._torque_only = False
        if command.kind is CommandKind.TORQUES:
            self._torques = [float(x) for x in command.target]
            return
        # 位置模式：把目标折成一次 P 控制力矩（真机上的伺服盒子会这么做）
        self._torques = [
            self._position_kp * (goal - pos)
            for goal, pos in zip(command.target, self._positions, strict=True)
        ]

    def safe_state(self) -> None:
        """安全态 = 力矩归零（本模型无抱闸，松劲即安全）。**幂等、不抛异常**。"""
        self.safe_calls += 1
        self._torques = [0.0] * self._joints

    def estop(self) -> None:
        """仿真里模拟"急停按钮被按下"：置故障位，让监督层的看门狗真的能看见。"""
        self._fault = "estop 已按下"
        self.safe_state()

    def close(self) -> None:
        self._connected = False

    # ---- 测试/演示用 ----
    def advance(self, seconds: float) -> None:
        """推进仿真时间（内部按 dt 分步，保证与 read_state 的一拍一致）。"""
        remaining = seconds
        while remaining > 1e-12:
            self._integrate(min(self._dt, remaining))
            remaining -= self._dt

    @property
    def positions(self) -> tuple[float, ...]:
        return tuple(self._positions)

    def _integrate(self, dt: float) -> None:
        for i in range(self._joints):
            accel = self._torques[i] - self._damping * self._velocities[i] - self._gravity
            self._velocities[i] += accel * dt
            self._positions[i] += self._velocities[i] * dt
            lo, hi = self._limits.position
            if self._positions[i] < lo:
                self._positions[i] = lo
                self._velocities[i] = 0.0
            elif self._positions[i] > hi:
                self._positions[i] = hi
                self._velocities[i] = 0.0
        self._t += dt
