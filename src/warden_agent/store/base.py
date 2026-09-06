"""存储抽象（Port）：定义持久化接口，让 SQLite / PostgreSQL 可互换。

  上层（会话、Web）只依赖这个接口，不关心背后是文件型 SQLite 还是服务器型 PostgreSQL。
  想换数据库，就传入一个实现同一接口的不同 Store，其他地方一行不用改。

接口方法（三种数据）：
  - Run 状态        ：save_run / load_run
  - 对话消息        ：append_message / load_messages
  - 待审批记录      ：save_pending_approval / load_pending_approval / clear_pending_approval
"""
from __future__ import annotations

from typing import Any, Protocol

from warden_agent.core.run.status import AgentRun
from warden_agent.model.model import Message


class RunStore(Protocol):
    """任何"能存 Run 状态 + 对话 + 待审批"的存储都要实现这个接口。"""

    def save_run(self, run: AgentRun) -> None: ...

    def load_run(self, run_id: str) -> AgentRun | None: ...

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]: ...

    def delete_run(self, run_id: str) -> None: ...

    def append_message(self, run_id: str, message: Message) -> None: ...

    def load_messages(self, run_id: str) -> list[Message]: ...

    def save_pending_approval(
        self,
        run_id: str,
        approval_id: str,
        tool_name: str,
        arguments: dict[str, object],
        reason: str,
    ) -> None: ...

    def load_pending_approval(
        self, run_id: str
    ) -> tuple[str, str, dict[str, object], str] | None: ...

    def clear_pending_approval(self, run_id: str) -> None: ...
