"""具身工具族 —— 把「能动机器人」交到模型手里，但**每一次动都要过裁决**。

为什么要单独一层，而不是让模型直接调 adapter：

- 模型的输出是**建议**，不是命令。它几百毫秒后才回来，而控制环 5ms 一拍；
  让模型直接写力矩，等于拿一个网络往返当控制周期用。
- 被拒绝必须是**可读的结果**，而不是异常、也不是静默夹取。工具返回
  `[被拒] code=... 原因=...` 时，模型能自己改目标重试，人也能在轨迹里看到
  「它想动、系统没让」。

因此这四个工具全部只碰 `ReflexLoop` / `SafetySupervisor` 的**慢脑入口**
（`set_target` / `hold` / `estop` / `reset`）：没有任何一条路径能绕过裁决。

`pure` 标记不是装饰：`robot.state` 是 pure（只读、可放心重试），另外三个不是
（会改物理状态），策略引擎的 ASK 规则与幂等键都读这个标记。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from warden_agent.embodiment.device import DeviceAdapter
from warden_agent.embodiment.reflex import LoopMetrics, ReflexLoop
from warden_agent.embodiment.safety import SafetySupervisor
from warden_agent.embodiment.types import DeviceState, Verdict
from warden_agent.tool.catalog import FunctionToolSpec, function_tool

__all__ = ["EmbodimentStack", "build_embodiment_tools"]


@dataclass(frozen=True, slots=True)
class EmbodimentStack:
    """一套能跑的具身栈：设备 + 裁决 + 反射环。

    三者是**同一套**——设备与反射环必须共用同一个 supervisor 实例，否则
    「反射环自己发指令」这一路就绕过了裁决。关节数不一致同样在这里挡掉：
    让工具收一个 12 维目标、设备只有 8 个关节，那种错误不该等到运行期。
    """

    adapter: DeviceAdapter
    supervisor: SafetySupervisor
    loop: ReflexLoop

    def __post_init__(self) -> None:
        if self.supervisor.joint_count != self.adapter.joint_count:
            raise ValueError(
                f"关节数不一致：设备 {self.adapter.joint_count} / "
                f"裁决 {self.supervisor.joint_count}"
            )


def _state_text(state: DeviceState, metrics: LoopMetrics, supervisor: SafetySupervisor) -> str:
    """把「现在什么状态」压成一行可读的事实，而不是原始数组。"""
    positions = "[" + ", ".join(f"{p:.3f}" for p in state.positions) + "]"
    payload = {
        "t": round(state.t, 3),
        "connected": state.connected,
        "fault": state.fault,
        "positions": positions,
        "estopped": supervisor.stopped,
        "stop_reason": supervisor.stop_reason,
        "ticks": metrics.ticks,
        "sends": metrics.sends,
        "denies": metrics.denies,
        "deadline_misses": metrics.deadline_misses,
        "p99_ms": round(metrics.p99_ms, 3),
    }
    return json.dumps(payload, ensure_ascii=False)


def _verdict_text(verdict: Verdict, action: str) -> str:
    if verdict.allowed:
        detail = f"（{verdict.detail}）" if verdict.detail else ""
        return f"[放行] {action}{detail}"
    return f"[被拒] {action}：code={verdict.code.value} 原因={verdict.detail or '未说明'}"


def build_embodiment_tools(stack: EmbodimentStack) -> list[FunctionToolSpec]:
    """造出四个具身工具。返回列表交给 `ToolCatalog.register`。"""
    adapter, supervisor, loop = stack.adapter, stack.supervisor, stack.loop

    @function_tool(
        "robot.state",
        "读机器人当前状态：关节位置、是否连接/故障、是否被急停、控制环指标。"
        "只读，随时可调；做任何动作前先读一次。",
        {"type": "object", "properties": {}},
        pure=True,
        triggers=("状态", "位置", "机器人", "关节", "robot", "state"),
    )
    def robot_state() -> str:
        try:
            state = adapter.read_state()
        except RuntimeError as e:
            return f"[不可用] 读不到设备状态：{e}"
        return _state_text(state, loop.metrics(), supervisor)

    @function_tool(
        "robot.move",
        "把机器人关节送到一组位置目标（弧度或度由设备决定）。"
        "每一次都会过安全裁决：越界、跳变过大、太快、重复请求、急停中都会被**拒绝**，"
        "被拒时目标不变（不会部分生效）。返回 \"[放行]\" 或 \"[被拒] code=... 原因=...\"。",
        {
            "type": "object",
            "properties": {
                "target": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "每个关节的目标位置，长度必须等于关节数",
                },
                "why": {"type": "string", "description": "为什么动这一步（会进审计轨迹）"},
            },
            "required": ["target", "why"],
        },
        pure=False,
        triggers=("动", "移动", "关节", "站", "走", "抓", "robot", "move"),
    )
    def robot_move(target: list[float], why: str) -> str:
        verdict = loop.set_target(tuple(float(x) for x in target), source="agent", why=why)
        return _verdict_text(verdict, f"目标 {[round(float(x), 3) for x in target]}")

    @function_tool(
        "robot.stop",
        "立即急停：锁存停机，之后所有动作都被拒绝，直到显式复位。"
        "任何不确定的情况都该先停——停住是可恢复的，动错不是。",
        {
            "type": "object",
            "properties": {"reason": {"type": "string", "description": "为什么停（会进审计轨迹）"}},
            "required": ["reason"],
        },
        pure=False,
        triggers=("停", "急停", "停止", "estop", "stop"),
    )
    def robot_stop(reason: str) -> str:
        supervisor.estop(reason, by="agent")
        return f"[已急停] 原因={reason}；此后动作全部被拒，需 robot.reset 才恢复"

    @function_tool(
        "robot.reset",
        "解除急停锁存。只清「停机」这个锁，不清上次的目标与限幅状态——"
        "复位不等于回到原点，要回原位请再调 robot.move。",
        {
            "type": "object",
            "properties": {"why": {"type": "string", "description": "为什么复位"}},
            "required": ["why"],
        },
        pure=False,
        triggers=("复位", "恢复", "解除", "reset"),
    )
    def robot_reset(why: str) -> str:
        verdict = supervisor.reset(by="agent")
        return _verdict_text(verdict, f"复位（{why}）")

    return [robot_state, robot_move, robot_stop, robot_reset]
