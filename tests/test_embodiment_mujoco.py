"""真物理适配器的反向对照：单位、重力、松劲、未连接。

这些用例需要 `mujoco`（可选依赖）；没装就 skip，而不是让整套测试变红——
但**一旦装了**，它们必须能证明"这条链路真的被重力影响过"。
"""

from __future__ import annotations

import pytest

mujoco = pytest.importorskip("mujoco", reason="mujoco 是可选依赖：没装就跳过物理用例")

from warden_agent.embodiment import (  # noqa: E402 - importorskip 之后再导入
    JointLimits,
    ReflexLoop,
    SafetySupervisor,
)
from warden_agent.embodiment.mujoco_sim import MujocoAdapter  # noqa: E402


def _loop(joint_count: int = 3) -> tuple[MujocoAdapter, SafetySupervisor, ReflexLoop]:
    adapter = MujocoAdapter(joint_count=joint_count)
    adapter.connect()
    supervisor = SafetySupervisor(
        JointLimits(position=(-1.4, 1.4), max_speed=50.0, max_step=0.5),
        joint_count,
        max_rate_hz=1000.0,
    )
    return adapter, supervisor, ReflexLoop(adapter, supervisor, hz=200.0)


def test_限位是弧度不是度() -> None:
    """回归：MuJoCo 默认按**度**解释 range，`-1.4 1.4` 会被读成 ±1.4°（±0.0244 rad）。

    这条不写就等于没发现——限位窄到 0.024 rad 时，任何动作都会被判越界，
    而错误信息会说是"目标越界"，看不出根因在单位。
    """
    adapter = MujocoAdapter(joint_count=3)
    lo, hi = adapter.limits.position
    assert lo == pytest.approx(-1.4, abs=1e-6)
    assert hi == pytest.approx(1.4, abs=1e-6)


def test_未连接时读状态抛异常而不是给缓存() -> None:
    adapter = MujocoAdapter(joint_count=3)
    with pytest.raises(RuntimeError, match="未连接"):
        adapter.read_state()


def test_PD_能把关节顶到目标_重力真实存在() -> None:
    adapter, _supervisor, loop = _loop()
    assert loop.set_target((0.6, 0.0, 0.0), why="顶到 0.6 rad").allowed
    for _ in range(600):                      # 600 × 5ms = 3s
        loop.step()
    assert adapter.positions[0] == pytest.approx(0.6, abs=0.12)


def test_安全态是松劲不是悬停_重力会把它压回去() -> None:
    adapter, _supervisor, loop = _loop()
    loop.set_target((0.6, 0.0, 0.0), why="先顶住")
    for _ in range(400):
        loop.step()
    held = adapter.positions[0]
    adapter.safe_state()
    adapter.advance(1.0)                      # 一秒内没有力矩，只有重力
    assert adapter.positions[0] < held - 0.05


def test_越界目标在真物理下同样被拒() -> None:
    _adapter, _supervisor, loop = _loop()
    verdict = loop.set_target((3.0, 0.0, 0.0), why="故意越界")
    assert verdict.allowed is False
    assert verdict.code.value == "limit"
