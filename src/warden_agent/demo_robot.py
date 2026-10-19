"""具身演示：让 Agent 真的去动一台机器人，并且**看着它被拦住一次**。

跑法（不需要任何 API key、不需要联网）：

    python -m warden_agent.demo_robot            # 有 mujoco 就用真物理，没有就用内置仿真
    python -m warden_agent.demo_robot --sim      # 强制用手写仿真（零依赖）

它演示的不是"模型很聪明"，而是这个仓库真正的主张：**模型的输出是建议，不是命令**。
同一次对话里会出现四种结果，每一种都可判、可审计：

    放行（正常目标）   → [放行] 目标 [0.3, 0.0, 0.0]
    拒绝（越界）       → [被拒] code=limit 原因=关节 0 目标 9.9000 超出 [-1.4, 1.4]
    拒绝（急停中）     → [被拒] code=estop 原因=...
    恢复（复位后才放行）

模型用的是**脚本模型**（`ScriptedModel`）：回复写死、逐条重放。这不是"降级后的真模型"，
所以每条回复都带 `[脚本模型]` 前缀——避免把演示输出当成推理能力。
"""

from __future__ import annotations

import sys
import time
from typing import Any

from warden_agent.agent import build_agent
from warden_agent.embodiment import (
    Code,
    EmbodimentStack,
    JointLimits,
    ReflexLoop,
    SafetySupervisor,
)
from warden_agent.embodiment.sim import SimAdapter
from warden_agent.model.model import ChatResponse, ToolCall

SCRIPT_PREFIX = "[脚本模型]"


class ScriptedModel:
    """按脚本逐条回放回复的模型。**不是**推理，所以每条都带前缀。"""

    def __init__(self, steps: list[ChatResponse]) -> None:
        self._steps = list(steps)
        self.calls = 0

    def chat(self, request: Any) -> ChatResponse:
        self.calls += 1
        if not self._steps:
            return ChatResponse(content=f"{SCRIPT_PREFIX} 脚本已经跑完。", finish_reason="stop")
        return self._steps.pop(0)


def _tool(call_id: str, name: str, args: dict[str, Any]) -> ChatResponse:
    return ChatResponse(
        content=None,
        tool_calls=[ToolCall(id=call_id, name=name, arguments=args)],
        finish_reason="tool_calls",
    )


def _script() -> list[ChatResponse]:
    """七步脚本：读状态 → 正常动 → 越界 → 急停 → 急停中再动 → 复位 → 收尾。"""
    return [
        _tool("s1", "robot.state", {}),
        _tool("s2", "robot.move", {"target": [0.3, 0.0, 0.0], "why": "抬到 0.3 rad"}),
        _tool("s3", "robot.move", {"target": [9.9, 0.0, 0.0], "why": "故意试越界"}),
        _tool("s4", "robot.stop", {"reason": "有人靠得太近"}),
        _tool("s5", "robot.move", {"target": [0.1, 0.0, 0.0], "why": "试试急停中还能不能动"}),
        _tool("s6", "robot.reset", {"why": "人走开了"}),
        ChatResponse(
            content=f"{SCRIPT_PREFIX} 汇报：正常目标放行、越界与急停中的动作被拒、复位后恢复。",
            finish_reason="stop",
        ),
    ]


def _make_adapter(force_sim: bool) -> tuple[Any, str, Any]:
    """返回 (设备, 后端描述, 反射环该用的时钟)。

    时钟必须跟着后端走：mujoco 是实时步进的，用 monotonic；SimAdapter 的时间轴
    是**仿真时间**（从 0 起），配 monotonic 会让看门狗立刻判 stale 并锁存急停，
    后面该发生的越界拒绝就再也不会发生了（这个坑在测试里先踩到过一次）。
    """
    if not force_sim:
        try:
            from warden_agent.embodiment.mujoco_sim import MujocoAdapter

            adapter: Any = MujocoAdapter(joint_count=3)
            adapter.connect()
            return adapter, "mujoco（真物理：重力/接触都在）", time.monotonic
        except Exception as exc:  # noqa: BLE001 - 演示必须能降级，不能因为没装依赖就跑不起来
            print(f"  mujoco 不可用（{type(exc).__name__}: {exc}），降级到内置仿真")
    sim: Any = SimAdapter(joint_count=3, limits=JointLimits(position=(-1.4, 1.4)))
    sim.connect()
    return sim, "sim（手写二阶系统：无重力/摩擦/延迟）", sim.now


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    force_sim = "--sim" in args
    events: list[str] = []

    print("=" * 78)
    print("第 1 步：装配（设备 → 安全裁决 → 200Hz 反射环 → 工具卡）")
    print("=" * 78)
    adapter, backend, clock = _make_adapter(force_sim)
    supervisor = SafetySupervisor(
        JointLimits(position=(-1.4, 1.4), max_speed=50.0, max_step=0.5),
        adapter.joint_count,
        max_rate_hz=1000.0,
        mode="deny",
        on_event=lambda e: events.append(f"{e.kind}/{e.code}"),
    )
    loop = ReflexLoop(adapter, supervisor, hz=200.0, clock=clock,
                      on_event=lambda e: events.append(f"{e.kind}/{e.code}"))
    stack = EmbodimentStack(adapter=adapter, supervisor=supervisor, loop=loop)
    print(f"  后端：{backend}")
    print(f"  关节数：{adapter.joint_count}   限位：{supervisor.joint_count} 维，"
          f"deny 模式（越界不夹取）")

    print()
    print("=" * 78)
    print("第 2 步：反射环真跑 1 秒（目标 0.3 rad，看它是不是真的动起来）")
    print("=" * 78)
    loop.set_target((0.3, 0.0, 0.0), why="演示：先让环自己把关节顶上去")
    loop.start()
    time.sleep(1.0)
    loop.stop()
    metrics = loop.metrics()
    print(f"  位置：{[round(p, 3) for p in adapter.positions]}")
    print(f"  拍数 {metrics.ticks} / 下发 {metrics.sends} / 拒绝 {metrics.denies} / "
          f"超时拍 {metrics.deadline_misses} / 单拍 p99 {metrics.p99_ms * 1000:.0f}µs")
    print("  注意：只有关节 0 有目标；其余关节在重力下自然下垂——这正是真物理与"
          "手写仿真的区别，也是为什么不能拿漂亮的位置曲线当能力证明。")

    print()
    print("=" * 78)
    print("第 3 步：让 Agent 用工具去动它（脚本模型，不是真推理）")
    print("=" * 78)
    model = ScriptedModel(_script())
    agent = build_agent(
        provider=model,
        policy_engine=None,
        embodiment=stack,
        system_prompt="你是一个能动机器人的助手。每次动作都要说明 why。",
        max_iterations=12,
    )
    reply = agent.chat("把关节抬到 0.3，然后演示一次越界与一次急停。")
    print(f"  模型调用 {model.calls} 次；最终回答：{reply}")

    print()
    print("=" * 78)
    print("第 4 步：证据（事件流 + 裁决码）")
    print("=" * 78)
    denials = [e for e in events if e.split("/")[-1] in {c.value for c in Code if c is not Code.OK}]
    print(f"  事件 {len(events)} 条，其中被拒 {len(denials)} 条：")
    for name in denials:
        print(f"    - {name}")
    print(f"  急停状态：{supervisor.stopped}（理由：{supervisor.stop_reason or '无'}）")
    print()
    print("结论：越界与急停中的动作都没有到达设备——拒绝发生在下发之前，")
    print("      并且留在了事件流里。模型可以提出任何目标，能落地的只有过了裁决的那些。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
