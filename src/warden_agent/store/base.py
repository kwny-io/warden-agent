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

    def list_runs(self, limit: int = 50, owner: str | None = None) -> list[dict[str, Any]]: ...

    def delete_run(self, run_id: str) -> None: ...

    def record_approval_decision(
        self,
        run_id: str,
        approval_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        decision: str,
    ) -> None: ...

    def list_approval_history(
        self, limit: int = 20, owner: str | None = None
    ) -> list[dict[str, Any]]: ...

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

    # ---- 检查点（跨 Run 协调恢复用；单查 + 枚举）----
    def save_checkpoint(self, checkpoint: object) -> None: ...

    def load_checkpoint(self, run_id: str) -> object | None: ...

    def list_checkpoints(self) -> list[object]: ...

    # ---- 跨副本共享状态（多副本部署用；单副本可走进程内实现，不碰这些）----
    #
    # 幂等表、事件流、限流计数原本是进程内的，多副本下各算一份 → 幂等失效 /
    # SSE 丢事件 / 限额翻倍。放到存储层后，多个副本读写同一张表即可共享。
    def get_idempotent(self, key: str) -> str | None: ...

    def save_idempotent(self, key: str, payload: str) -> None: ...

    def reserve_idempotent(self, key: str, payload: str) -> bool:
        """**仅当 key 不存在时**写入 payload（原子），返回是否占到。

        用来堵幂等的 TOCTOU：两个同 `Idempotency-Key` 的并发请求，只有占到位的那个执行。
        """
        ...

    def release_idempotent(self, key: str, payload: str) -> None:
        """删除"处理中"占位（`payload` 匹配时才删，避免误删已写好的响应快照）。"""
        ...

    def append_event(self, run_id: str, payload: str) -> int: ...

    def list_events_after(
        self, run_id: str, after_seq: int, limit: int = 200
    ) -> list[tuple[int, str]]: ...

    def hit_rate_limit(
        self, bucket_key: str, window_seconds: int, now: float
    ) -> tuple[int, float]: ...
