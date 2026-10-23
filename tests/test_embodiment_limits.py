"""限位到底管不管用：力矩命令与"设备自己越界"这两条以前是空的。

这两块是同一类缺陷的两半：
  · 命令侧：`max_torque` 写在 `JointLimits` 里，却从未参与任何裁决——
    一个 1000 N·m 的力矩目标可以原样下发（真机上等于让电机烧掉）。
  · 观测侧：`observe()` 只查断连/故障/过期，**不看数字本身**——
    撞到限位、被外力打飞、电机堵转还在出力，这三种事故都不会被锁存。
"""

from __future__ import annotations

import pytest

from warden_agent.embodiment.reflex import ReflexLoop
from warden_agent.embodiment.safety import SafetySupervisor
from warden_agent.embodiment.sim import SimAdapter
from warden_agent.embodiment.types import Code, Command, CommandKind, DeviceState, JointLimits

LIMITS = JointLimits(position=(-1.0, 1.0), max_speed=3.0, max_torque=5.0, max_step=0.2)


def _sup(mode: str = "deny", observe: frozenset[str] | None = None) -> SafetySupervisor:
    return SafetySupervisor(LIMITS, 2, mode=mode, observe_limits=observe, clock=lambda: 0.0)


def _state(**kw: object) -> DeviceState:
    base: dict[str, object] = {"t": 0.0, "positions": (0.0, 0.0)}
    base.update(kw)
    return DeviceState(**base)  # type: ignore[arg-type]


# ---- 命令侧：力矩 ----
def test_力矩超限被拒且理由带数字() -> None:
    sup = _sup()
    verdict = sup.submit(Command(CommandKind.TORQUES, (1.0, 6.0)))
    assert verdict.denied and verdict.code is Code.LIMIT
    assert "6.0000" in verdict.detail and "5" in verdict.detail


def test_力矩在限内放行() -> None:
    sup = _sup()
    verdict = sup.submit(Command(CommandKind.TORQUES, (5.0, -5.0)))
    assert verdict.allowed and verdict.target == (5.0, -5.0)


def test_clamp_模式下力矩被夹到边界并说明() -> None:
    sup = _sup(mode="clamp")
    verdict = sup.submit(Command(CommandKind.TORQUES, (9.0, -0.5)))
    assert verdict.allowed
    assert verdict.target == (5.0, -0.5)
    assert "夹到" in verdict.detail


# ---- 观测侧：设备自己越界 ----
def test_观测到位置越界就锁存而不是等下一个命令() -> None:
    sup = _sup()
    verdict = sup.observe(_state(positions=(1.5, 0.0)))
    assert verdict.denied and verdict.code is Code.LIMIT
    assert sup.stopped and "观测到" in sup.stop_reason
    # 锁存后任何命令都不再放行；返回的码是**锁存原因**（这里就是那次观测越界），
    # 不是笼统的 estop——复盘时要能回答"它为什么停"，不能只回答"它停了"。
    later = sup.submit(Command(CommandKind.TORQUES, (1.0, 1.0)))
    assert later.denied and later.code is Code.LIMIT and "由 observer" in later.detail


def test_观测到超速锁存() -> None:
    sup = _sup(observe=frozenset({"velocity"}))
    verdict = sup.observe(_state(velocities=(0.0, 4.0)))
    assert verdict.denied and "速度" in verdict.detail


def test_观测到超力矩锁存() -> None:
    sup = _sup(observe=frozenset({"torque"}))
    verdict = sup.observe(_state(torques=(6.0, 0.0)))
    assert verdict.denied and "力矩" in verdict.detail


@pytest.mark.parametrize(
    "state",
    [
        {"positions": (1.0, -1.0)},
        {"velocities": (3.0, -3.0)},
        {"torques": (5.0, -5.0)},
    ],
)
def test_贴边不算越界(state: dict[str, object]) -> None:
    sup = _sup(observe=frozenset({"position", "velocity", "torque"}))
    assert sup.observe(_state(**state)).allowed
    assert not sup.stopped


def test_默认只查位置_不查速度与力矩() -> None:
    """默认口径：位置超范围才算事故。速度/力矩要显式开启——适配器语义不同。"""
    sup = _sup()
    assert sup.observe(_state(velocities=(99.0, 0.0), torques=(99.0, 0.0))).allowed
    assert not sup.stopped


def test_观测维度的取值校验() -> None:
    with pytest.raises(ValueError, match="observe_limits"):
        SafetySupervisor(LIMITS, 2, observe_limits=frozenset({"torque_"}))  # type: ignore[arg-type]


def test_复位后再次观测越界会重新锁存() -> None:
    sup = _sup()
    assert sup.observe(_state(positions=(2.0, 0.0))).denied
    assert sup.reset(by="operator").allowed
    assert sup.observe(_state(positions=(0.0, 0.0))).allowed
    assert sup.observe(_state(positions=(0.0, 2.0))).denied
    assert sup.stopped

# ---- 反射环：PD 输出必须按额定力矩饱和，不能"每拍被拒" ----
def test_PD_输出按_max_torque_饱和且不产生拒绝() -> None:
    """kp=20、误差 0.6 rad ⇒ 原始力矩 12 N·m，而额定 5 N·m。

    没有饱和的话，每一拍都会被裁决拒掉：环空转、关节不动、日志里全是 deny，
    看起来"安全系统在工作"，实际上整台机器一动不动。饱和才是对的行为。
    """
    sim = SimAdapter(2, dt=0.005, damping=0.5)
    sim.connect()
    events: list[str] = []
    sup2 = SafetySupervisor(
        LIMITS, 2, max_rate_hz=1000.0, watchdog_s=1.0, observe_limits=None,
        clock=sim.now, on_event=lambda e: events.append(f"{e.kind}/{e.code}"),
    )
    loop = ReflexLoop(
        sim, sup2, hz=200.0, kp=20.0, kd=1.0, clock=sim.now,
        on_event=lambda e: events.append(f"{e.kind}/{e.code}"),
    )
    assert loop.set_target((0.6, -0.6)).allowed
    for _ in range(10):
        loop.step()
    metrics = loop.metrics()
    assert metrics.denies == 0 and metrics.sends == 10
    assert metrics.saturations == 10
    assert all(abs(tau) <= LIMITS.max_torque + 1e-9 for tau in sim.read_state().torques)
    assert any(e == "reflex/saturate" for e in events)
    assert not sup2.stopped
