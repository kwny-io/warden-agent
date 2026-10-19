"""安全监督：**所有**命令的唯一入口，拒绝必须带理由。

这条线为什么值得单独一层（而不是在反射环里写几个 if）：
  真机上"越界"从来不是单一条件——位置超限、单 tick 跳变、状态过期、命令刷屏、
  急停锁存、断连，各自有不同的处置和不同的证据。混在控制律里，出事故时你只能回答
  "它没动"，回答不了"它为什么没动"。这一层就是为了让第二个问题永远有答案。

三条 fail-closed 取向（与 `policy/` 同源）：
  1. **默认拒绝而非夹取**：越界目标直接拒（`mode="deny"`）。静默夹到边界会改变意图却留下
     "执行成功"的记录——那比拒绝更危险。确实想夹，显式选 `mode="clamp"`，且 `detail` 写明夹了。
  2. **状态过期即停**：看门狗只信状态自带的时钟；超过 `watchdog_s` 没收到底盘状态就锁存急停。
  3. **复位不等于恢复运动**：`reset()` 只清锁存；下一次目标仍要过限位与单 tick 跳变检查，
     所以"复位后立刻全速"做不到——这是有意的。
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable, Sequence

from warden_agent.embodiment.types import (
    Code,
    Command,
    CommandKind,
    DeviceState,
    Event,
    JointLimits,
    Verdict,
)

EventSink = Callable[[Event], None]

# 观测容差：编码器量化/积分噪声会让"刚好在边界"的值看起来越界一点点。
# 这里只放过噪声量级，不放"明显越界"——后者是设备真的出事了。
OBS_TOLERANCE = 1e-3


class SafetySupervisor:
    """把"能不能动"变成可判、可查、可复盘的裁决。"""

    def __init__(
        self,
        limits: JointLimits,
        joint_count: int,
        *,
        max_rate_hz: float = 20.0,
        watchdog_s: float = 0.5,
        mode: str = "deny",
        observe_limits: frozenset[str] | None = None,
        clock: Callable[[], float] = time.monotonic,
        on_event: EventSink | None = None,
    ) -> None:
        if joint_count <= 0:
            raise ValueError(f"joint_count 必须为正，得到 {joint_count!r}")
        if mode not in ("deny", "clamp"):
            raise ValueError(f"mode 只能是 deny/clamp，得到 {mode!r}")
        if max_rate_hz <= 0:
            raise ValueError(f"max_rate_hz 必须为正，得到 {max_rate_hz!r}")
        if watchdog_s <= 0:
            raise ValueError(f"watchdog_s 必须为正，得到 {watchdog_s!r}")
        armed = frozenset({"position"}) if observe_limits is None else frozenset(observe_limits)
        unknown = armed - {"position", "velocity", "torque"}
        if unknown:
            raise ValueError(
                f"observe_limits 只认 position/velocity/torque，得到 {sorted(unknown)}"
            )
        self._limits = limits
        self._joints = joint_count
        self._max_rate = max_rate_hz
        self._watchdog = watchdog_s
        self._mode = mode
        self._clock = clock
        self._on_event = on_event
        # 观测侧要查哪几维**必须显式选**：不同适配器报的 torques 语义不一样
        # （有的是指令力矩、有的是测量力矩），默认只查位置——位置超范围
        # 在任何语义下都说明"出事了"，力矩/速度则可能只是控制器的瞬时指令。
        self._observe_limits = armed
        self._stop: tuple[Code, str, str] | None = None   # (码, 理由, 谁锁的)
        self._last_target: tuple[float, ...] | None = None
        # 最近一次 observe 到的位姿。跳变检查需要一个"从哪儿跳"的参照：
        # 只有 _last_target 时，**下发第一条命令**这一路完全没有约束——
        # 而"从静止一下跳到极限位置"正是最该被拦住的那一次。
        self._last_pose: tuple[float, ...] | None = None
        self._accepted: deque[float] = deque()
        self._seen: dict[str, Code] = {}

    # ---- 只读视图（给运维/时间线用）----
    @property
    def joint_count(self) -> int:
        """受管的关节数。装配期就能核对「设备与裁决是不是同一台机器」。"""
        return self._joints

    @property
    def limits(self) -> JointLimits:
        """限位的**唯一来源**。反射环要按 max_torque 饱和，就得从这里取，不能自己再编一套。"""
        return self._limits

    @property
    def stopped(self) -> bool:
        return self._stop is not None

    @property
    def stop_reason(self) -> str:
        if self._stop is None:
            return ""
        code, detail, by = self._stop
        return f"{code.value}: {detail}（由 {by}）"

    # ---- 裁决 ----
    def submit(
        self, command: Command, *, now: float | None = None, internal: bool = False
    ) -> Verdict:
        """裁决一条命令。放行返回 `Code.OK`，拒绝返回**不会执行**的码与理由。"""
        t = self._clock() if now is None else now
        if self._stop is not None:
            return self._emit(t, command, Verdict(False, self._stop[0], self.stop_reason))
        if command.request_id:
            first = self._seen.get(command.request_id)
            if first is not None:
                return self._emit(
                    t,
                    command,
                    Verdict(
                        False,
                        Code.DUP,
                        f"request_id={command.request_id} 已处理过"
                        f"（首次裁决 {first.value}），不重复执行",
                    ),
                )
        checked = self._check_shape(command)
        if checked is not None:
            return self._emit(t, command, checked)
        target = command.target
        detail = ""
        if command.kind is not CommandKind.IDLE:
            if not all(math.isfinite(x) for x in target):
                return self._emit(t, command, Verdict(False, Code.NAN, "目标含 NaN/Inf：拒绝下发"))
            if command.kind is CommandKind.POSITIONS:
                violation = self._limits.axis_violation(target)
                if violation is not None:
                    if self._mode == "deny":
                        return self._emit(
                            t,
                            command,
                            Verdict(False, Code.LIMIT, violation + "（deny 模式：不夹取）"),
                        )
                    target = self._limits.clamp(target)
                    detail = violation + "（已夹到边界）"
                step_detail = self._step_violation(target)
                if step_detail is not None:
                    if self._mode == "deny":
                        return self._emit(t, command, Verdict(False, Code.STEP, step_detail))
                    target = self._clamp_step(target)
                    detail = step_detail + "（已按 max_step 限幅）"
            elif command.kind is CommandKind.TORQUES:
                # 力矩以前只过 shape/NaN：一个 1000 N·m 的目标能被原样下发，
                # 而 max_torque 就写在 JointLimits 里却从未参与裁决。
                torque_bad = self._limits.torque_violation(target)
                if torque_bad is not None:
                    if self._mode == "deny":
                        return self._emit(
                            t,
                            command,
                            Verdict(False, Code.LIMIT, torque_bad + "（deny 模式：不夹取）"),
                        )
                    target = self._limits.clamp_torque(target)
                    detail = torque_bad + "（已夹到 ±max_torque）"
        self._trim_rate(t)
        if not internal and len(self._accepted) >= self._max_rate:
            return self._emit(
                t,
                command,
                Verdict(
                    False,
                    Code.RATE,
                    f"命令频率超限：{self._max_rate:g}/s，窗口内已接受 {len(self._accepted)} 条",
                ),
            )
        if not internal:
            self._accepted.append(t)
        if command.request_id:
            self._seen[command.request_id] = Code.OK
        if command.kind is CommandKind.POSITIONS:
            self._last_target = target
        return self._emit(t, command, Verdict(True, Code.OK, detail, target))

    def observe(self, state: DeviceState, *, now: float | None = None) -> Verdict:
        """看一眼底盘状态：断连/故障/过期都会**锁存**急停（不是只返回一个告警）。"""
        t = self._clock() if now is None else now
        if not state.connected:
            return self._trip(t, Code.DISCONNECTED, "适配器报告断连：进入安全态", "watchdog")
        if state.fault:
            return self._trip(t, Code.FAULT, f"适配器报告故障：{state.fault}", "watchdog")
        self._last_pose = tuple(float(x) for x in state.positions)
        overrun = self._observed_violation(state)
        if overrun is not None:
            code, detail = overrun
            return self._trip(t, code, detail, "observer")
        age = t - state.t
        if age > self._watchdog:
            return self._trip(
                t, Code.STALE, f"状态过期 {age:.3f}s > 看门狗 {self._watchdog:.3f}s", "watchdog"
            )
        return Verdict(True, Code.OK, "")

    def _observed_violation(self, state: DeviceState) -> tuple[Code, str] | None:
        """设备**自己报回来**的越界。命令侧查的是"你想干什么"，这里查的是"它到底怎么了"。

        真机上这三类是同一场事故的不同阶段：撞到限位（位置越界）、被外力打飞（超速）、
        电机堵转还在出力（超力矩）。它们都不该等下一个命令才被发现——所以观测到就锁存。
        """
        lo, hi = self._limits.position
        if "position" in self._observe_limits:
            for i, x in enumerate(state.positions):
                if x < lo - OBS_TOLERANCE or x > hi + OBS_TOLERANCE:
                    return (
                        Code.LIMIT,
                        f"观测到关节 {i} 位置 {x:.4f} 超出硬限 [{lo}, {hi}]："
                        "设备自己越界（撞限位/编码器漂移），已锁存",
                    )
        for i, v in (enumerate(state.velocities) if "velocity" in self._observe_limits else ()):
            if abs(v) > self._limits.max_speed + OBS_TOLERANCE:
                return (
                    Code.LIMIT,
                    f"观测到关节 {i} 速度 {abs(v):.4f} 超过 max_speed "
                    f"{self._limits.max_speed:g}：已锁存",
                )
        for i, tau in (enumerate(state.torques) if "torque" in self._observe_limits else ()):
            if abs(tau) > self._limits.max_torque + OBS_TOLERANCE:
                return (
                    Code.LIMIT,
                    f"观测到关节 {i} 力矩 {abs(tau):.4f} 超过 max_torque "
                    f"{self._limits.max_torque:g}：疑似堵转/过载，已锁存",
                )
        return None

    def estop(self, reason: str, *, by: str, now: float | None = None) -> None:
        """人工/程序急停。**锁存**：不调用 `reset()` 不会自己好。"""
        self._trip(self._clock() if now is None else now, Code.ESTOP, reason, by)

    def reset(self, *, by: str, now: float | None = None) -> Verdict:
        """清锁存。只清锁存，不恢复运动——下一个目标仍要过全部检查。"""
        t = self._clock() if now is None else now
        if self._stop is None:
            return Verdict(True, Code.OK, "当前没有锁存，无需复位")
        was = self.stop_reason
        self._stop = None
        self._accepted.clear()
        self._emit_event(
            Event(
                t=t,
                kind="safety",
                code=Code.OK.value,
                detail=f"复位：{was}；由 {by} 执行",
                source="operator",
            )
        )
        return Verdict(True, Code.OK, f"已复位（原锁存：{was}）")

    # ---- 内部 ----
    def _check_shape(self, command: Command) -> Verdict | None:
        if command.kind is CommandKind.IDLE:
            return None
        if not command.target:
            return Verdict(False, Code.SHAPE, f"{command.kind.value} 命令缺少目标")
        if len(command.target) != self._joints:
            return Verdict(
                False,
                Code.SHAPE,
                f"目标关节数 {len(command.target)} != 底盘关节数 {self._joints}",
            )
        return None

    def _step_reference(self) -> tuple[float, ...] | None:
        """跳变的参照：优先上一个目标；没有目标就用最近观测到的位姿。

        两者都没有（既没 observe 过、也没下发过目标）说明我们对"现在在哪儿"
        一无所知——那时不做跳变判断，但也不假装检查过。
        """
        if self._last_target is not None:
            return self._last_target
        return self._last_pose

    def _step_violation(self, target: Sequence[float]) -> str | None:
        reference = self._step_reference()
        if reference is None:
            return None
        for i, (now_x, last_x) in enumerate(zip(target, reference, strict=True)):
            if abs(now_x - last_x) > self._limits.max_step:
                return (
                    f"关节 {i} 单 tick 跳变 {abs(now_x - last_x):.4f} > max_step "
                    f"{self._limits.max_step:.4f}"
                )
        return None

    def _clamp_step(self, target: Sequence[float]) -> tuple[float, ...]:
        reference = self._step_reference()
        assert reference is not None
        step = self._limits.max_step
        return tuple(
            last + min(step, max(-step, x - last))
            for x, last in zip(target, reference, strict=True)
        )

    def _trim_rate(self, now: float) -> None:
        while self._accepted and now - self._accepted[0] > 1.0:
            self._accepted.popleft()

    def _trip(self, t: float, code: Code, detail: str, by: str) -> Verdict:
        if self._stop is None:
            self._stop = (code, detail, by)
        self._emit_event(Event(t=t, kind="safety", code=code.value, detail=detail, source=by))
        return Verdict(False, code, detail)

    def _emit(self, t: float, command: Command, verdict: Verdict) -> Verdict:
        self._emit_event(
            Event(
                t=t,
                kind="command",
                code=verdict.code.value,
                detail=verdict.detail,
                source=command.source,
                request_id=command.request_id,
            )
        )
        return verdict

    def _emit_event(self, event: Event) -> None:
        if self._on_event is not None:
            self._on_event(event)
