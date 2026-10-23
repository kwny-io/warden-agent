"""具身工具族的反向对照：**拒绝**必须是可读结果，不是异常、不是静默夹取。"""

from __future__ import annotations

import json

import pytest

from warden_agent.embodiment import (
    JointLimits,
    ReflexLoop,
    SafetySupervisor,
    SimAdapter,
)
from warden_agent.embodiment.tools import EmbodimentStack, build_embodiment_tools


def _call(tools: dict[str, object], name: str, args: dict[str, object]) -> str:
    spec = tools[name]
    fn = spec.function  # type: ignore[attr-defined]
    assert fn is not None
    return str(fn(**args))


def _stack(
    joint_count: int = 4, *, mode: str = "deny", max_step: float = 0.5
) -> tuple[EmbodimentStack, dict[str, object]]:
    adapter = SimAdapter(joint_count=joint_count)
    adapter.connect()
    limits = JointLimits(position=(-1.0, 1.0), max_speed=50.0, max_step=max_step)
    supervisor = SafetySupervisor(limits, joint_count, max_rate_hz=1000.0, mode=mode)
    loop = ReflexLoop(adapter, supervisor, hz=200.0, clock=adapter.now)
    stack = EmbodimentStack(adapter=adapter, supervisor=supervisor, loop=loop)
    tools = {spec.name: spec for spec in build_embodiment_tools(stack)}
    return stack, tools


def test_四张卡与_pure_标记一致() -> None:
    _stack_obj, tools = _stack()
    assert list(tools) == ["robot.state", "robot.move", "robot.stop", "robot.reset"]
    # 只有只读的那张是 pure：另外三张会改物理状态，策略引擎的 ASK 规则读这个标记
    assert tools["robot.state"].pure is True
    assert tools["robot.move"].pure is False
    assert tools["robot.stop"].pure is False
    assert tools["robot.reset"].pure is False


def test_状态是结构化事实而不是原始数组() -> None:
    stack, tools = _stack()
    text = _call(tools, "robot.state", {})
    payload = json.loads(text)
    assert payload["connected"] is True
    assert payload["estopped"] is False
    assert payload["positions"].count(",") == 3
    assert payload["ticks"] == 0


def test_设备没连上时状态返回不可用而不是抛异常() -> None:
    adapter = SimAdapter(joint_count=4)          # 故意不 connect
    supervisor = SafetySupervisor(JointLimits(position=(-1.0, 1.0)), 4, max_rate_hz=1000.0)
    loop = ReflexLoop(adapter, supervisor)
    tools = {s.name: s for s in build_embodiment_tools(EmbodimentStack(adapter, supervisor, loop))}
    assert _call(tools, "robot.state", {}).startswith("[不可用]")


def test_越界目标被拒而且目标不变() -> None:
    stack, tools = _stack()
    text = _call(tools, "robot.move", {"target": [2.0, 0.0, 0.0, 0.0], "why": "故意越界"})
    assert text.startswith("[被拒]")
    assert "LIMIT" in text.upper()
    assert stack.loop.target is None          # 被拒不是"部分生效"


def test_放行后目标真的生效() -> None:
    stack, tools = _stack()
    text = _call(tools, "robot.move", {"target": [0.3, 0.0, 0.0, 0.0], "why": "抬一个关节"})
    assert text.startswith("[放行]")
    assert stack.loop.target == pytest.approx((0.3, 0.0, 0.0, 0.0))


def test_跳变超限在_clamp_模式下被夹取且说明原因() -> None:
    stack, tools = _stack(mode="clamp", max_step=0.1)
    stack.loop.step()                      # 先看一眼真实位姿：跳变要有参照
    text = _call(tools, "robot.move", {"target": [0.9, 0.0, 0.0, 0.0], "why": "一步跨太远"})
    assert text.startswith("[放行]")
    assert "限幅" in text or "夹" in text
    assert stack.loop.target is not None
    assert stack.loop.target[0] == pytest.approx(0.1, abs=1e-6)


def test_急停后一切动作被拒_复位后恢复() -> None:
    stack, tools = _stack()
    assert _call(tools, "robot.stop", {"reason": "看到人靠太近"}).startswith("[已急停]")
    assert stack.supervisor.stopped is True
    denied = _call(tools, "robot.move", {"target": [0.2, 0.0, 0.0, 0.0], "why": "试试还能不能动"})
    assert denied.startswith("[被拒]")
    assert "ESTOP" in denied.upper()
    assert stack.loop.target is None
    assert _call(tools, "robot.reset", {"why": "人走开了"}).startswith("[放行]")
    assert stack.supervisor.stopped is False


def test_没有参照位姿时不做跳变判断也不假装检查过() -> None:
    """既没 observe 过、也没下发过目标——那时我们对"现在在哪儿"一无所知。

    这条钉的是**行为边界**：不确定就不判断（放行），而不是拿 0 当参照假装检查。
    一旦 observe 过一次，同一条命令就必须被拦。
    """
    stack, tools = _stack(mode="deny", max_step=0.1)
    first = _call(tools, "robot.move", {"target": [0.9, 0.0, 0.0, 0.0], "why": "没有参照"})
    assert first.startswith("[放行]")
    stack.loop.step()                      # 从这一拍起有了参照位姿
    second = _call(tools, "robot.move", {"target": [0.0, 0.0, 0.0, 0.0], "why": "反向跨太远"})
    assert second.startswith("[被拒]")
    assert "STEP" in second.upper()
    assert stack.loop.target == pytest.approx((0.9, 0.0, 0.0, 0.0))   # 被拒后目标仍是上一个


def test_关节数不一致拒绝装配() -> None:
    adapter = SimAdapter(joint_count=4)
    supervisor = SafetySupervisor(JointLimits(position=(-1.0, 1.0)), 5)
    loop = ReflexLoop(adapter, supervisor)
    with pytest.raises(ValueError, match="关节数不一致"):
        EmbodimentStack(adapter, supervisor, loop)
