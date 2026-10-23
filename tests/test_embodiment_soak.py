"""长跑体检单的回归：**它不能因为"跑得久"就变得不可测。**

三条硬要求：① 拍数/下发数对得上；② 200Hz 下不出现超时拍；③ 总线出问题时能被记下来。
"""

from __future__ import annotations

import pytest

from warden_agent.embodiment.safety import SafetySupervisor
from warden_agent.embodiment.serial_adapter import (
    LoopbackDevice,
    LoopbackTransport,
    SerialAdapter,
)
from warden_agent.embodiment.soak import SoakReport, main, run_soak
from warden_agent.embodiment.types import JointLimits

LIMITS = JointLimits(position=(-1.4, 1.4), max_speed=50.0, max_step=0.5)


def _rig(*, drop_every: int = 0, timeout: float = 0.02, max_retries: int = 2,
         joints: int = 3) -> tuple[SerialAdapter, SafetySupervisor, object, list[str]]:
    from warden_agent.embodiment.reflex import ReflexLoop

    events: list[str] = []
    device = LoopbackDevice(joint_count=joints, limits=LIMITS, drop_every=drop_every)
    adapter = SerialAdapter(LoopbackTransport(device), joints, limits=LIMITS,
                            timeout=timeout, max_retries=max_retries)
    adapter.connect()
    supervisor = SafetySupervisor(LIMITS, joints, max_rate_hz=1000.0, mode="deny",
                                  on_event=lambda e: events.append(f"{e.kind}/{e.code}"))
    loop = ReflexLoop(adapter, supervisor, hz=200.0, clock=adapter.now)
    return adapter, supervisor, loop, events


def test_长跑拍数与下发数对得上() -> None:
    adapter, supervisor, loop, events = _rig()
    report = run_soak(adapter, supervisor, loop, ticks=300)   # type: ignore[arg-type]
    assert isinstance(report, SoakReport)
    assert report.ticks == 300
    assert report.sends == 300
    assert report.denies == 0
    assert report.deadline_misses == 0
    assert report.bus.startswith("发 ")


def test_目标收敛且没有漂移() -> None:
    adapter, supervisor, loop, events = _rig()
    report = run_soak(adapter, supervisor, loop, ticks=600,
                      target=(0.2, 0.1, 0.0))     # type: ignore[arg-type]
    assert max(abs(d) for d in report.drift) < 0.02


def test_丢帧会被记进总线计数而不崩() -> None:
    adapter, supervisor, loop, events = _rig(drop_every=25, timeout=0.005, max_retries=3)
    report = run_soak(adapter, supervisor, loop, ticks=200)   # type: ignore[arg-type]
    assert "重试" in report.bus
    assert report.ticks == 200


def test_越界目标会被拒而环照常跑完() -> None:
    # 越界目标在 set_target 就被 supervisor 拒掉（这是事件，不是拍级拒绝），
    # 环继续用上一个合法目标跑——所以体检单里 denies 是 0，但事件流里必须有这条拒绝。
    adapter, supervisor, loop, events = _rig()
    report = run_soak(adapter, supervisor, loop, ticks=50,
                      target=(9.9, 0.0, 0.0))     # type: ignore[arg-type]
    assert report.ticks == 50
    assert any("limit" in e for e in events)


def test_ticks非正数直接拒绝() -> None:
    adapter, supervisor, loop, events = _rig()
    with pytest.raises(ValueError):
        run_soak(adapter, supervisor, loop, ticks=0)          # type: ignore[arg-type]


def test_命令行能跑通并打印读法(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--sim", "--ticks", "50"]) == 0
    out = capsys.readouterr().out
    assert "长跑：50 拍" in out
    assert "超时拍 0" in out
    assert "它不证明的事" in out
