"""长跑（soak）：把"跑 1 秒"变成"跑 1 万拍，并且能说清有没有漂"。

为什么硬件赛道必须有这一页：评委（和真实用户）问的从来不是"能不能动一下"，
而是**"能连续跑多久、跑久了会怎样"**。一次 1 秒的演示回答不了这个问题，
而"跑了 200 拍没报错"更回答不了——要的是：拍间隔有没有抖、超时有没有累积、
位置有没有漂、总线有没有开始丢帧。

口径（诚实）：
  · 这里量的是**控制环与链路**，不是机器人姿态质量。位置能收敛只说明链路与控制器对得上。
  · 仿真/环回后端的时间轴由步进推进，所以 `duration_s` 是**墙钟耗时**（真实耗时），
    而"仿真时间"另算——两者不要混着说。
  · 没有真机，所以没有温漂、没有电池压降、没有电机发热——这些是 soak 的**下一层**。
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable
from dataclasses import dataclass

from warden_agent.embodiment.device import DeviceAdapter
from warden_agent.embodiment.reflex import ReflexLoop
from warden_agent.embodiment.safety import SafetySupervisor
from warden_agent.embodiment.types import JointLimits


@dataclass(frozen=True, slots=True)
class SoakReport:
    """一次长跑的体检单。字段少而硬：每一条都能被判"过/不过"。"""

    ticks: int
    sends: int
    denies: int
    deadline_misses: int
    p99_ms: float
    max_gap_ms: float
    drift: tuple[float, ...]
    duration_s: float
    bus: str

    def as_text(self) -> str:
        worst = max((abs(d) for d in self.drift), default=0.0)
        return (
            f"拍数 {self.ticks} / 下发 {self.sends} / 拒绝 {self.denies} / "
            f"超时拍 {self.deadline_misses} / 单拍 p99 {self.p99_ms * 1000:.0f}µs / "
            f"最大拍间隔 {self.max_gap_ms:.2f}ms / 最大偏差 {worst:.4f} rad / "
            f"墙钟耗时 {self.duration_s:.2f}s / 总线 {self.bus}"
        )


def run_soak(
    adapter: DeviceAdapter,
    supervisor: SafetySupervisor,
    loop: ReflexLoop,
    *,
    ticks: int = 10_000,
    target: tuple[float, ...] | None = None,
) -> SoakReport:
    """手工步进 ticks 拍（不走线程：线程化会让"抖动"混入调度噪声，测不准）。"""
    if ticks <= 0:
        raise ValueError(f"ticks 必须为正，得到 {ticks!r}")
    goal = target if target is not None else tuple(0.2 for _ in range(adapter.joint_count))
    loop.set_target(goal, why=f"soak：{ticks} 拍稳定目标")
    gaps: list[float] = []
    started = time.perf_counter()
    last = started
    for _ in range(ticks):
        loop.step()
        now = time.perf_counter()
        gaps.append((now - last) * 1000.0)
        last = now
    duration = time.perf_counter() - started
    metrics = loop.metrics()
    positions = tuple(getattr(adapter, "positions", ()) or ())
    drift = tuple(g - p for g, p in zip(goal, positions, strict=False))
    counters = getattr(adapter, "counters", None)
    bus = counters.as_text() if counters is not None else "（该后端没有总线计数）"
    return SoakReport(
        ticks=metrics.ticks,
        sends=metrics.sends,
        denies=metrics.denies,
        deadline_misses=metrics.deadline_misses,
        p99_ms=metrics.p99_ms,
        max_gap_ms=max(gaps) if gaps else 0.0,
        drift=drift,
        duration_s=duration,
        bus=bus,
    )


def _build(
    argv: list[str],
) -> tuple[DeviceAdapter, SafetySupervisor, ReflexLoop, argparse.Namespace]:
    parser = argparse.ArgumentParser(prog="warden_agent.embodiment.soak",
                                     description="控制环长跑：量拍间隔、超时累积、位置漂移、总线健康")
    parser.add_argument("--sim", action="store_true", help="手写仿真（默认；写出来只为对称）")
    parser.add_argument("--serial", action="store_true", help="走串口协议（环回设备，不需要真机）")
    parser.add_argument("--mujoco", action="store_true", help="用 mujoco 真物理")
    parser.add_argument("--ticks", type=int, default=10_000)
    parser.add_argument("--hz", type=float, default=200.0)
    args = parser.parse_args(argv)
    limits = JointLimits(position=(-1.4, 1.4), max_speed=50.0, max_step=0.5)
    clock: Callable[[], float] = time.monotonic
    if args.serial:
        from warden_agent.embodiment.serial_adapter import (
            LoopbackDevice,
            LoopbackTransport,
            SerialAdapter,
        )

        device = LoopbackDevice(joint_count=3, limits=limits)
        adapter: DeviceAdapter = SerialAdapter(LoopbackTransport(device), 3, limits=limits,
                                               timeout=0.02, max_retries=2)
        adapter.connect()
        clock = adapter.now
    elif args.mujoco:
        from warden_agent.embodiment.mujoco_sim import MujocoAdapter

        adapter = MujocoAdapter(joint_count=3)
        adapter.connect()
    else:
        from warden_agent.embodiment.sim import SimAdapter

        adapter = SimAdapter(joint_count=3, limits=limits)
        adapter.connect()
        clock = adapter.now
    supervisor = SafetySupervisor(limits, adapter.joint_count, max_rate_hz=1000.0, mode="deny")
    loop = ReflexLoop(adapter, supervisor, hz=args.hz, clock=clock)
    return adapter, supervisor, loop, args


def main(argv: list[str] | None = None) -> int:
    from warden_agent.core.console import force_utf8_stdio

    force_utf8_stdio()
    adapter, supervisor, loop, args = _build(list(argv or []))
    report = run_soak(adapter, supervisor, loop, ticks=args.ticks)
    print("=" * 78)
    print(f"长跑：{report.ticks} 拍（控制频率 {args.hz:.0f} Hz）")
    print("=" * 78)
    print(f"  {report.as_text()}")
    print(f"  急停：{supervisor.stopped}（理由：{supervisor.stop_reason or '无'}）")
    print()
    print("怎么读这张单子：")
    print("  · 超时拍 0     → 没有一拍因为读状态/下发而错过周期（链路跟得上控制频率）")
    print("  · 最大拍间隔   → 抖动；远大于周期说明有阻塞（GC、串口阻塞读、锁竞争）")
    print("  · 最大偏差     → 稳态误差；随拍数**增长**才叫漂移，恒定偏差只是 P 控制的余差")
    print("  · 总线计数     → 重试/超时/CRC 坏随拍数增长，说明物理层在恶化")
    print()
    print("它不证明的事：温漂、电池压降、电机发热、机械回差——这些要真机长跑。")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(__import__("sys").argv[1:]))
