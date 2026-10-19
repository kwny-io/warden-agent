"""具身接入层（embodiment）：把 agent 的"意图"接到"会动的东西"上。

四个模块、一条边界：
  `types`   命令/状态/限位/裁决/事件 —— 纯数据，无副作用
  `device`  `DeviceAdapter` 契约 —— **真机与仿真唯一的分叉点**
  `safety`  安全监督 —— 所有命令的唯一入口，拒绝必须带理由（fail-closed）
  `reflex`  定频反射环 —— 慢脑改目标，反射环管周期

三条取向，与仓里既有设计同源（`policy/` 的 ALLOW/ASK/DENY、`runtime/checkpoint` 的可恢复、
`web/audit.py` 的只增留痕）：
  1. **拒绝是证据**：被拒的命令带码与理由进事件流，不抛异常、不静默。
  2. **默认拒绝而不是夹取**：越界目标直接拒；要夹取必须显式选 `mode="clamp"` 且写明夹了。
  3. **停止不等于恢复**：复位只清锁存，下一次目标仍要过全部检查。

边界（诚实）：本层不做感知、不做规划、不做 sim2real 标定；它保证的是
"命令有出处、越界会被拒、状态过期会停、拍子抖动看得见"，**不保证机器人站得住**。
"""

from warden_agent.embodiment.device import DeviceAdapter
from warden_agent.embodiment.reflex import LoopMetrics, ReflexLoop
from warden_agent.embodiment.safety import SafetySupervisor
from warden_agent.embodiment.sim import SimAdapter
from warden_agent.embodiment.types import (
    Code,
    Command,
    CommandKind,
    DeviceState,
    Event,
    JointLimits,
    Verdict,
)

__all__ = [
    "Code",
    "Command",
    "CommandKind",
    "DeviceAdapter",
    "DeviceState",
    "Event",
    "JointLimits",
    "LoopMetrics",
    "ReflexLoop",
    "SafetySupervisor",
    "SimAdapter",
    "Verdict",
]
