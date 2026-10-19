"""定频反射环：**慢脑改目标，反射环管周期**。

为什么必须把这两件事拆开（这是具身系统最常被低估的一条）：
  `loop/` 的一次工具调用是几百毫秒量级（模型 + 网络 + 沙箱），而保持站立需要 100~500Hz。
  把控制律挂在 agent 循环里，等于让"网络抖一下"变成"机器人摔一下"。
  所以：agent 只提交**目标**（一次），反射环在自己的线程里按固定周期做 PD 与看门狗；
  agent 挂了、模型超时、SSE 断了，反射环都还在，并且会自己进入安全态。

力矩饱和：PD 输出按 `max_torque` 饱和后才提交裁决。不饱和的话，kp=20 配 0.6 rad 误差
  会算出 12 N·m 去撞 5 N·m 的额定值——每一拍都被拒，环空转、关节不动，
  日志里全是 deny，看起来"安全系统在工作"，实际上一动不动。饱和要计数，因为
  "这一拍打满了"本身就是控制质量的事实。

时钟口径（写进类型而不是散在注释里）：
  `clock` 必须与适配器 `DeviceState.t` **同一时间基准**，否则看门狗会把"两个时钟的差"
  当成"状态过期"。单测里用 `SimAdapter.now` 传进来就是这个原因。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from warden_agent.embodiment.device import DeviceAdapter
from warden_agent.embodiment.safety import SafetySupervisor
from warden_agent.embodiment.types import Command, CommandKind, Event, Verdict

EventSink = Callable[[Event], None]


@dataclass(frozen=True, slots=True)
class LoopMetrics:
    """反射环的体检数据：`p99_ms` 比"平均够快"有用得多——抖动才是摔跤的原因。"""

    ticks: int
    sends: int
    denies: int
    saturations: int
    deadline_misses: int
    safe_calls: int
    period_ms: float
    p50_ms: float
    p99_ms: float
    max_ms: float


class ReflexLoop:
    """固定频率的控制环。`step()` 是纯函数式的一拍，`start()` 才开线程。"""

    def __init__(
        self,
        adapter: DeviceAdapter,
        supervisor: SafetySupervisor,
        *,
        hz: float = 200.0,
        kp: float = 12.0,
        kd: float = 0.8,
        clock: Callable[[], float] = time.monotonic,
        work_clock: Callable[[], float] = time.perf_counter,
        on_event: EventSink | None = None,
        jitter_ratio: float = 1.5,
        history: int = 2048,
    ) -> None:
        if hz <= 0:
            raise ValueError(f"hz 必须为正，得到 {hz!r}")
        if kp <= 0 or kd < 0:
            raise ValueError(f"增益非法：kp={kp!r} kd={kd!r}")
        if jitter_ratio <= 1.0:
            raise ValueError(f"jitter_ratio 必须 > 1，得到 {jitter_ratio!r}")
        self._adapter = adapter
        self._supervisor = supervisor
        self._hz = hz
        self._period = 1.0 / hz
        self._kp = kp
        self._kd = kd
        self._clock = clock
        # 量"这一拍花了多久"必须用**墙钟**，而且要和控制时钟分开：
        # 仿真后端的时间轴由步进决定，拿它量耗时恒等于周期，看着稳、其实没信息。
        # 单独暴露出来是为了可测——测试用假时钟就能稳定造出"超时拍"。
        self._work_clock = work_clock
        self._on_event = on_event
        self._jitter_ratio = jitter_ratio
        self._durations: deque[float] = deque(maxlen=history)
        self._target: tuple[float, ...] | None = None
        self._saturated_now = False
        self._ticks = 0
        self._sends = 0
        self._denies = 0
        self._saturations = 0
        self._misses = 0
        self._safe_calls = 0
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()

    # ---- 慢脑的入口：只改目标 ----
    @property
    def target(self) -> tuple[float, ...] | None:
        return self._target

    def set_target(
        self,
        target: Sequence[float],
        *,
        source: str = "agent",
        why: str = "",
        request_id: str = "",
    ) -> Verdict:
        """提交一个位置目标。被拒时**目标不变**（不会"部分生效"）。"""
        command = Command(
            kind=CommandKind.POSITIONS,
            target=tuple(float(x) for x in target),
            source=source,
            why=why,
            request_id=request_id,
        )
        verdict = self._supervisor.submit(command)
        if verdict.allowed:
            self._target = verdict.target
        return verdict

    def hold(self, *, source: str = "agent", why: str = "松劲保持") -> Verdict:
        """放弃跟踪目标（回到 IDLE）。"""
        self._target = None
        return self._supervisor.submit(Command(kind=CommandKind.IDLE, source=source, why=why))

    # ---- 一拍 ----
    def step(self) -> None:
        t0 = self._clock()
        # 单拍耗时必须用**墙钟**量：仿真后端的时间轴由步进决定，复用控制时钟量出来
        # 恒等于周期（5000µs），看着"很稳"，其实一个字的真话都没有。
        p0 = self._work_clock()
        self._ticks += 1
        try:
            state = self._adapter.read_state()
        except Exception as exc:  # noqa: BLE001 - 读失败必须降级成"停"，不是崩
            self._denies += 1
            self._supervisor.estop(f"读状态失败：{type(exc).__name__}: {exc}", by="reflex")
            self._safe()
            self._record(t0, p0)
            return
        now = self._clock()
        observed = self._supervisor.observe(state, now=now)
        if self._supervisor.stopped:
            if observed.denied:
                self._denies += 1
            self._safe()
            self._record(t0, p0)
            return
        command = self._command(state.positions, state.velocities)
        verdict = self._supervisor.submit(command, now=now, internal=True)
        if self._saturated_now:
            self._saturations += 1
            self._emit("reflex", "saturate", "PD 输出达到 max_torque，已饱和")
        if verdict.allowed:
            try:
                self._adapter.send(command)
            except Exception as exc:  # noqa: BLE001 - 下发失败同样降级成"停"
                self._denies += 1
                self._supervisor.estop(f"下发失败：{type(exc).__name__}: {exc}", by="reflex")
                self._safe()
            else:
                self._sends += 1
        else:
            self._denies += 1
        self._record(t0, p0)

    def _command(
        self, positions: Sequence[float], velocities: Sequence[float]
    ) -> Command:
        if self._target is None:
            return Command(kind=CommandKind.IDLE, source="reflex", why="无目标")
        cap = self._supervisor.limits.max_torque
        torque: list[float] = []
        saturated = False
        for i, (goal, pos) in enumerate(zip(self._target, positions, strict=False)):
            vel = velocities[i] if i < len(velocities) else 0.0
            raw = self._kp * (goal - pos) - self._kd * vel
            if abs(raw) > cap:
                # 控制器**按额定力矩饱和**，而不是把超限值直接丢给裁决：
                # 后者会让每一拍都被拒（环空转、关节不动），而且日志里全是噪音。
                # 饱和本身要计数并进事件流——"这一拍打满了"是控制质量的事实。
                saturated = True
                raw = cap if raw > 0 else -cap
            torque.append(raw)
        self._saturated_now = saturated
        return Command(
            kind=CommandKind.TORQUES,
            target=tuple(torque),
            source="reflex",
            why="PD 跟踪",
        )

    def _safe(self) -> None:
        self._safe_calls += 1
        self._adapter.safe_state()

    def _emit(self, kind: str, code: str, detail: str) -> None:
        if self._on_event is not None:
            self._on_event(
                Event(t=self._clock(), kind=kind, code=code, detail=detail, source="reflex")
            )

    def _record(self, t0: float, p0: float) -> None:
        dt = self._work_clock() - p0
        self._durations.append(dt)
        if dt > self._period * self._jitter_ratio:
            self._misses += 1
            if self._on_event is not None:
                self._on_event(
                    Event(
                        t=t0,
                        kind="reflex",
                        code="deadline_miss",
                        detail=f"单拍耗时 {dt * 1000:.2f}ms > 周期 {self._period * 1000:.2f}ms × "
                        f"{self._jitter_ratio:g}",
                        source="reflex",
                    )
                )

    # ---- 线程 ----
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._run, name="warden-reflex", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        next_at = self._clock()
        while not self._stop_evt.is_set():
            self.step()
            next_at += self._period
            delay = next_at - self._clock()
            if delay > 0:
                self._stop_evt.wait(delay)
            else:
                # 落后就重新对齐，**不补跑**——补跑会把一次卡顿放大成一次连发。
                next_at = self._clock()

    def stop(self, *, timeout: float = 2.0) -> None:
        self._stop_evt.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout)

    # ---- 指标 ----
    def metrics(self) -> LoopMetrics:
        durations = sorted(self._durations)
        if durations:
            p50 = durations[len(durations) // 2]
            p99 = durations[min(len(durations) - 1, int(len(durations) * 0.99))]
            mx = durations[-1]
        else:
            p50 = p99 = mx = 0.0
        return LoopMetrics(
            ticks=self._ticks,
            sends=self._sends,
            denies=self._denies,
            saturations=self._saturations,
            deadline_misses=self._misses,
            safe_calls=self._safe_calls,
            period_ms=self._period * 1000.0,
            p50_ms=p50 * 1000.0,
            p99_ms=p99 * 1000.0,
            max_ms=mx * 1000.0,
        )
