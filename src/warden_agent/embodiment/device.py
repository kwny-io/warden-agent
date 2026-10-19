"""设备适配器契约：真机与仿真**只在这一层分叉**。

一个刻意的取舍：接口里没有"设置频率"或"配置 PID"这类方法。
  频率是反射环的事、PID 是控制律的事；把它们下沉到适配器，就等于让**每个适配器**
  各自决定"多快算快"——那正是"同一个 agent 在仿真里稳、上真机就抖"的常见成因。
  适配器只负责三件事：**读状态、下发、进入安全态**。

安全态的语义（fail-closed）：`safe_state()` 必须是"断电也安全"的那个动作——
  对位置伺服是"保持在当前位并松劲"，对力矩伺服是"力矩归零"。
  接口不提供"软一点的安全态"选项：需要软着陆就该在控制律里做，不该在收尾里做。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from warden_agent.embodiment.types import Command, DeviceState


@runtime_checkable
class DeviceAdapter(Protocol):
    """最小设备契约。实现方：`sim.SimAdapter`（仿真）、`serial_adapter.SerialAdapter`（真机）。"""

    @property
    def joint_count(self) -> int:
        """关节数。命令的形状必须与它一致（多一个少一个都拒）。"""
        ...

    def now(self) -> float:
        """设备时钟（秒）。反射环的看门狗必须用它——拿主机墙钟量仿真时间轴会立刻判 stale。"""
        ...

    def connect(self) -> None:
        """建立连接。失败必须抛异常——**不许"连上了但没通"**。"""
        ...

    def read_state(self) -> DeviceState:
        """读一次状态（含单调时钟秒数）。读失败必须抛异常，不许返回上一次的缓存。"""
        ...

    def send(self, command: Command) -> None:
        """下发一条**已通过裁决**的命令。实现方不得再做策略判断（那会让拒绝理由失真）。"""
        ...

    def safe_state(self) -> None:
        """进入安全态（幂等、可重复调用、不得抛异常——它常常是在异常处理里被调的）。"""
        ...

    def close(self) -> None:
        """释放资源；同样幂等。"""
        ...
