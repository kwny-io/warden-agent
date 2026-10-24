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


_PERIOD_MS = 5.0          # 200Hz
_MISS_RATIO = 0.01        # 容忍 ≤1% 的孤立越界


def _timing_violations(report: SoakReport) -> list[str]:
    """「有没有系统性阻塞」的判据。返回空列表 = 没有。

    为什么不是 `deadline_misses == 0`：那个数由 `reflex._record` 按**墙钟**统计单拍耗时，
    墙钟数的是「整台机器当时忙不忙」，不是控制环写得好不好。实测同一份代码：
    空载 p99 = 322~721µs（截止预算 5ms × 1.5 = 7.5ms），全量套件并发时 p99 = 4.72ms，
    300 拍里偶发 1 拍越界。钉 0 会让交付门禁**随机**红，而红的原因与被测代码无关——
    随机红的门禁会被关掉，等于没有门禁。

    换成两条都指向「没有阻塞」的判据：
      · p99 落在**周期以内**——99% 的拍跟得上控制频率；
      · 超时拍占比 ≤ 1%——容忍孤立抖动，不容忍持续越界。
    真阻塞（一次读卡几十毫秒）会让每一拍都超、p99 远超周期 ⇒ 两条同时报。
    `deadline_miss` 的计数语义另由 `tests/test_embodiment.py::test_单拍超时能被记成_deadline_miss`
    用假时钟确定性钉住（不依赖负载）。

    放宽必须自带反证（`test_系统性阻塞会被这把尺子抓住`）：把阈值调松到抓不住，
    那条反证立刻红。
    """
    out: list[str] = []
    if report.p99_ms >= _PERIOD_MS:
        out.append(
            f"p99 已越过 200Hz 的周期（{report.p99_ms:.2f}ms ≥ {_PERIOD_MS:g}ms）"
            f"⇒ 不是抖动而是跟不上"
        )
    allowed = int(report.ticks * _MISS_RATIO)
    if report.deadline_misses > allowed:
        out.append(
            f"超时拍 {report.deadline_misses}/{report.ticks} 超过 {_MISS_RATIO:.0%}"
            f"（容许 {allowed} 拍）⇒ 持续越界，不是孤立的调度抖动"
        )
    return out


def test_长跑拍数与下发数对得上() -> None:
    adapter, supervisor, loop, events = _rig()
    report = run_soak(adapter, supervisor, loop, ticks=300)   # type: ignore[arg-type]
    assert isinstance(report, SoakReport)
    assert report.ticks == 300
    assert report.sends == 300
    assert report.denies == 0
    assert not _timing_violations(report), "；".join(_timing_violations(report))
    assert report.bus.startswith("发 ")


def test_系统性阻塞会被这把尺子抓住() -> None:
    """反证：把单拍人为拖慢到超出周期 ⇒ `_timing_violations` 必须两条都报。

    没有这一条，上面从 `== 0` 放宽成「p99 < 周期 且 占比 ≤ 1%」就是纯放行：
    阈值被改坏（比如把 5.0 写成 5000.0）也没人知道。这里**真的**睡 8ms，
    必然同时越过 7.5ms 的截止预算与 5ms 的周期；而且走的是**同一条**判据函数，
    所以改阈值会同时影响两边 —— 这才是反证该有的耦合。
    """
    import time as _time

    adapter, supervisor, loop, events = _rig()
    real_read = adapter.read_state

    def _slow_read():  # type: ignore[no-untyped-def]
        _time.sleep(0.008)   # 8ms > 周期 5ms，也 > 截止预算 7.5ms
        return real_read()

    adapter.read_state = _slow_read                        # type: ignore[method-assign]
    report = run_soak(adapter, supervisor, loop, ticks=20)  # type: ignore[arg-type]
    violations = _timing_violations(report)
    assert len(violations) == 2, (
        f"阻塞摆在眼前却只报出 {len(violations)} 条（{violations}）"
        f"⇒ 判据被改松了，放宽变成了放行"
    )


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
