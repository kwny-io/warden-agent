"""具身接入层的判定：**拒绝要带理由、停止要能锁住、拍子要能被看见**。

这份测试刻意不测"机器人站得稳不稳"——那需要真机或高保真仿真。
它测的是三件可以确定性判定、且出事时最贵的事：
  1. 越界/跳变/断连/过期**真的**会被拒，并且拒的理由可读（不是静默 clamp）；
  2. 急停**锁存**，复位不等于恢复运动；
  3. 反射环的每一拍耗时可度量，读/写失败会进安全态而不是抛栈。
"""

from __future__ import annotations

import pytest

from warden_agent.embodiment import (
    Code,
    Command,
    CommandKind,
    DeviceState,
    JointLimits,
    ReflexLoop,
    SafetySupervisor,
    SimAdapter,
)

LIMITS = JointLimits(position=(-1.0, 1.0), max_speed=3.0, max_torque=5.0, max_step=0.2)


class ManualClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeAdapter:
    """最小适配器：记录下发、可注入读/写异常、时间由外部时钟给。"""

    def __init__(self, clock: ManualClock, joints: int = 2, *, read_error: bool = False,
                 send_error: bool = False, state_age: float = 0.0) -> None:
        self.clock = clock
        self._joints = joints
        self.read_error = read_error
        self.send_error = send_error
        self.state_age = state_age
        self.sent: list[Command] = []
        self.safe_calls = 0
        self.connected = True

    @property
    def joint_count(self) -> int:
        return self._joints

    def connect(self) -> None:
        self.connected = True

    def read_state(self) -> DeviceState:
        if self.read_error:
            raise OSError("总线掉了")
        return DeviceState(
            t=self.clock.t - self.state_age,
            positions=(0.0,) * self._joints,
            velocities=(0.0,) * self._joints,
            connected=True,
        )

    def send(self, command: Command) -> None:
        if self.send_error:
            raise OSError("写超时")
        self.sent.append(command)

    def safe_state(self) -> None:
        self.safe_calls += 1

    def close(self) -> None:
        self.connected = False


# ---------------- 限位与裁决 ----------------

def test_限位区间非法必须当场报错() -> None:
    with pytest.raises(ValueError, match="lo < hi"):
        JointLimits(position=(1.0, 1.0))
    with pytest.raises(ValueError, match="max_step"):
        JointLimits(position=(0.0, 1.0), max_step=0.0)
    with pytest.raises(ValueError, match="max_speed"):
        JointLimits(position=(0.0, 1.0), max_speed=-1.0)


def test_贴边不算越界而真越界要指出关节号() -> None:
    assert LIMITS.axis_violation((1.0, -1.0)) is None
    detail = LIMITS.axis_violation((0.0, 1.5))
    assert detail is not None and "关节 1" in detail and "1.5" in detail


def test_放行时回填目标且裁决可判() -> None:
    sup = SafetySupervisor(LIMITS, 2)
    verdict = sup.submit(Command(CommandKind.POSITIONS, (0.3, -0.3)))
    assert verdict.allowed and verdict.code is Code.OK and verdict.target == (0.3, -0.3)
    assert verdict.denied is False


def test_关节数不对或缺目标都拒() -> None:
    sup = SafetySupervisor(LIMITS, 2)
    assert sup.submit(Command(CommandKind.POSITIONS)).code is Code.SHAPE
    assert sup.submit(Command(CommandKind.POSITIONS, (1.0, 2.0, 3.0))).code is Code.SHAPE
    # IDLE 不需要目标
    assert sup.submit(Command(CommandKind.IDLE)).allowed


def test_nan_目标拒而不是夹取() -> None:
    sup = SafetySupervisor(LIMITS, 2)
    verdict = sup.submit(Command(CommandKind.POSITIONS, (0.1, float("nan"))))
    assert verdict.code is Code.NAN and "NaN" in verdict.detail


def test_越界默认拒_显式夹取模式才夹() -> None:
    deny = SafetySupervisor(LIMITS, 2)
    v = deny.submit(Command(CommandKind.POSITIONS, (0.5, 2.0)))
    assert v.code is Code.LIMIT and v.denied and "deny" in v.detail

    clamp = SafetySupervisor(LIMITS, 2, mode="clamp")
    v2 = clamp.submit(Command(CommandKind.POSITIONS, (0.5, 2.0)))
    assert v2.allowed and v2.target == (0.5, 1.0) and "已夹到边界" in v2.detail


def test_单拍跳变默认拒_夹取模式下按_max_step_限幅() -> None:
    deny = SafetySupervisor(LIMITS, 2)
    assert deny.submit(Command(CommandKind.POSITIONS, (0.0, 0.0))).allowed
    v = deny.submit(Command(CommandKind.POSITIONS, (0.9, 0.0)))
    assert v.code is Code.STEP and "max_step" in v.detail

    clamp = SafetySupervisor(LIMITS, 2, mode="clamp")
    assert clamp.submit(Command(CommandKind.POSITIONS, (0.0, 0.0))).allowed
    v2 = clamp.submit(Command(CommandKind.POSITIONS, (0.9, 0.0)))
    assert v2.allowed and abs(v2.target[0] - 0.2) < 1e-9


def test_频率上限拦住慢脑连发而内部定频命令不算刷屏() -> None:
    clock = ManualClock()
    sup = SafetySupervisor(LIMITS, 2, max_rate_hz=2.0, clock=clock)
    assert sup.submit(Command(CommandKind.TORQUES, (0.0, 0.0))).allowed
    assert sup.submit(Command(CommandKind.TORQUES, (0.0, 0.0))).allowed
    burst = sup.submit(Command(CommandKind.TORQUES, (0.0, 0.0)))
    assert burst.code is Code.RATE and "2/s" in burst.detail
    # 反射环的定频命令不受这条约束；但限位照样管
    assert sup.submit(Command(CommandKind.TORQUES, (0.0, 0.0)), internal=True).allowed
    bad = sup.submit(Command(CommandKind.POSITIONS, (5.0, 0.0)), internal=True)
    assert bad.code is Code.LIMIT
    clock.advance(1.1)
    assert sup.submit(Command(CommandKind.TORQUES, (0.0, 0.0))).allowed


def test_同一_request_id_不执行第二次() -> None:
    sup = SafetySupervisor(LIMITS, 2)
    first = sup.submit(Command(CommandKind.POSITIONS, (0.1, 0.1), request_id="req-1"))
    assert first.allowed
    again = sup.submit(Command(CommandKind.POSITIONS, (0.1, 0.1), request_id="req-1"))
    assert again.code is Code.DUP and "req-1" in again.detail


def test_急停锁存_复位只清锁存不恢复运动() -> None:
    sup = SafetySupervisor(LIMITS, 2)
    sup.estop("人工拍下", by="operator")
    blocked = sup.submit(Command(CommandKind.POSITIONS, (0.1, 0.1)))
    assert sup.stopped and blocked.code is Code.ESTOP and "人工拍下" in blocked.detail
    again = sup.reset(by="operator")
    assert again.allowed and "已复位" in again.detail
    assert not sup.stopped
    # 复位不清"上一次目标"：从 0 跳到 0.9 仍被 max_step 拦
    assert sup.submit(Command(CommandKind.POSITIONS, (0.0, 0.0))).allowed
    assert sup.submit(Command(CommandKind.POSITIONS, (0.9, 0.0))).code is Code.STEP
    assert sup.reset(by="operator").allowed  # 没锁存时复位是幂等的


def test_观察到断连_故障_过期都锁存急停() -> None:
    clock = ManualClock()
    sup = SafetySupervisor(LIMITS, 2, watchdog_s=0.5, clock=clock)
    down = sup.observe(DeviceState(t=0.0, positions=(0.0, 0.0), connected=False))
    assert down.code is Code.DISCONNECTED and sup.stopped

    sup2 = SafetySupervisor(LIMITS, 2, watchdog_s=0.5, clock=clock)
    fault = sup2.observe(DeviceState(t=0.0, positions=(0.0, 0.0), fault="过流"))
    assert fault.code is Code.FAULT and "过流" in fault.detail

    sup3 = SafetySupervisor(LIMITS, 2, watchdog_s=0.5, clock=clock)
    clock.t = 1.0
    stale = sup3.observe(DeviceState(t=0.0, positions=(0.0, 0.0)))
    assert stale.code is Code.STALE and "看门狗" in stale.detail

    sup4 = SafetySupervisor(LIMITS, 2, watchdog_s=0.5, clock=clock)
    assert sup4.observe(DeviceState(t=clock.t, positions=(0.0, 0.0))).allowed


def test_事件流带得走_request_id_与来源() -> None:
    events: list[str] = []
    def collect(e: object) -> None:
        item: object = e
        events.append(
            f"{getattr(item, 'kind')}|{getattr(item, 'code')}|"
            f"{getattr(item, 'source')}|{getattr(item, 'request_id')}"
        )

    sup = SafetySupervisor(LIMITS, 2, on_event=collect)
    sup.submit(Command(CommandKind.POSITIONS, (0.1, 0.1), source="agent", request_id="r-9"))
    assert events == ["command|ok|agent|r-9"]


def test_构造期参数写错要报错() -> None:
    with pytest.raises(ValueError, match="joint_count"):
        SafetySupervisor(LIMITS, 0)
    with pytest.raises(ValueError, match="mode"):
        SafetySupervisor(LIMITS, 2, mode="auto")
    with pytest.raises(ValueError, match="max_rate_hz"):
        SafetySupervisor(LIMITS, 2, max_rate_hz=0)
    with pytest.raises(ValueError, match="watchdog_s"):
        SafetySupervisor(LIMITS, 2, watchdog_s=0)


# ---------------- 仿真适配器 ----------------

def test_仿真适配器要求先连接() -> None:
    sim = SimAdapter(2)
    with pytest.raises(RuntimeError, match="未连接"):
        sim.read_state()
    with pytest.raises(RuntimeError, match="未连接"):
        sim.send(Command(CommandKind.IDLE))
    sim.connect()
    assert sim.read_state().positions == (0.0, 0.0)


def test_仿真_位置模式转力矩_安全态零力矩且幂等() -> None:
    sim = SimAdapter(2, damping=0.1)
    sim.connect()
    sim.send(Command(CommandKind.POSITIONS, (0.5, 0.5)))
    assert sim.last_command is not None
    assert all(t > 0 for t in sim.read_state().torques)
    sim.safe_state()
    sim.safe_state()
    assert sim.safe_calls == 2
    assert sim.read_state().torques == (0.0, 0.0)


def test_仿真_急停置故障位可被监督层看见() -> None:
    sim = SimAdapter(2)
    sim.connect()
    sim.estop()
    state = sim.read_state()
    assert state.fault is not None and "estop" in state.fault
    sim.close()
    with pytest.raises(RuntimeError, match="未连接"):
        sim.read_state()


def test_仿真_advance_按_dt_分步且不越界() -> None:
    sim = SimAdapter(1, dt=0.01, damping=0.0, limits=JointLimits(position=(-0.1, 0.1)))
    sim.connect()
    sim.send(Command(CommandKind.TORQUES, (50.0,)))
    sim.advance(1.0)
    assert sim.positions[0] <= 0.1 + 1e-9


# ---------------- 反射环 ----------------

def _loop(
    joints: int = 2, **kwargs: object
) -> tuple[ReflexLoop, FakeAdapter, SafetySupervisor, ManualClock]:
    clock = ManualClock()
    adapter = FakeAdapter(clock, joints)
    sup = SafetySupervisor(LIMITS, joints, clock=clock, max_rate_hz=1000.0, watchdog_s=0.5)
    loop = ReflexLoop(adapter, sup, hz=200.0, clock=clock, **kwargs)  # type: ignore[arg-type]
    return loop, adapter, sup, clock


def test_反射环_PD_把关节推向目标() -> None:
    sim = SimAdapter(2, dt=0.005, damping=0.5)
    sim.connect()
    sup = SafetySupervisor(LIMITS, 2, clock=sim.now, max_rate_hz=1000.0, watchdog_s=1.0)
    loop = ReflexLoop(sim, sup, hz=200.0, kp=20.0, kd=1.0, clock=sim.now)
    assert loop.set_target((0.6, -0.6)).allowed
    for _ in range(300):
        loop.step()
    halfway = abs(sim.positions[0] - 0.6)
    for _ in range(900):
        loop.step()
    positions = sim.positions
    # 不是"一步到位"：先证明在收敛，再证明收敛到了容差内
    assert halfway < 0.35 and abs(positions[0] - 0.6) < 0.05 and abs(positions[1] + 0.6) < 0.05
    metrics = loop.metrics()
    assert metrics.ticks == 1200 and metrics.sends == 1200 and metrics.denies == 0


def test_目标被拒时旧目标保持不变() -> None:
    loop, _adapter, _sup, _clock = _loop()
    assert loop.set_target((0.1, 0.1)).allowed
    assert loop.set_target((9.0, 9.0)).denied
    assert loop.target == (0.1, 0.1)


def test_读状态失败进安全态并锁存急停() -> None:
    clock = ManualClock()
    adapter = FakeAdapter(clock, read_error=True)
    sup = SafetySupervisor(LIMITS, 2, clock=clock, watchdog_s=10.0)
    loop = ReflexLoop(adapter, sup, clock=clock)
    loop.step()
    assert sup.stopped and adapter.safe_calls == 1
    assert "总线掉了" in sup.stop_reason
    loop.step()
    assert adapter.safe_calls == 2  # 停在安全态是持续的，不是一次性的


def test_下发失败同样进安全态() -> None:
    clock = ManualClock()
    adapter = FakeAdapter(clock, send_error=True)
    sup = SafetySupervisor(LIMITS, 2, clock=clock, watchdog_s=10.0)
    loop = ReflexLoop(adapter, sup, clock=clock)
    loop.set_target((0.1, 0.1))
    loop.step()
    assert sup.stopped and "写超时" in sup.stop_reason and adapter.safe_calls == 1


def test_单拍超时能被记成_deadline_miss() -> None:
    clock = ManualClock()

    class SlowAdapter(FakeAdapter):
        def read_state(self) -> DeviceState:
            clock.advance(0.05)  # 200Hz 的周期是 5ms，这里一次读就花 50ms
            return super().read_state()

    slow = SlowAdapter(clock, 2)
    sup = SafetySupervisor(LIMITS, 2, clock=clock, watchdog_s=10.0)
    events: list[str] = []
    loop = ReflexLoop(
        slow, sup, hz=200.0, clock=clock, work_clock=clock,
        on_event=lambda e: events.append(e.code),
    )
    loop.step()
    assert loop.metrics().deadline_misses == 1
    assert events == ["deadline_miss"]


def test_无目标时下发_IDLE() -> None:
    loop, adapter, _sup, _clock = _loop()
    loop.step()
    assert adapter.sent and adapter.sent[0].kind is CommandKind.IDLE
    assert loop.hold().allowed and loop.target is None


def test_线程能起停且至少跑够几拍() -> None:
    clock = ManualClock()
    adapter = FakeAdapter(clock)
    sup = SafetySupervisor(LIMITS, 2, clock=clock, watchdog_s=10.0)
    loop = ReflexLoop(adapter, sup, hz=200.0, clock=clock)
    loop.start()
    loop.start()  # 幂等
    for _ in range(200):
        clock.advance(0.005)
        if len(adapter.sent) >= 3:
            break
    loop.stop()
    assert loop.metrics().ticks >= 1


def test_反射环与监督层构造期校验() -> None:
    loop, _adapter, sup, _clock = _loop()
    with pytest.raises(ValueError, match="hz"):
        ReflexLoop(_adapter, sup, hz=0)
    with pytest.raises(ValueError, match="增益"):
        ReflexLoop(_adapter, sup, kp=0)
    with pytest.raises(ValueError, match="jitter_ratio"):
        ReflexLoop(_adapter, sup, jitter_ratio=1.0)
    assert loop.metrics().ticks == 0


def test_指标在没有样本时全部为_0() -> None:
    loop, _adapter, _sup, _clock = _loop()
    metrics = loop.metrics()
    assert metrics.p50_ms == 0.0 and metrics.p99_ms == 0.0 and metrics.period_ms == 5.0
