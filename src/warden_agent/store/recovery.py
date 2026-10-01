"""恢复助理：让中断的 Agent 从上次存档点接着干。

⚠️ **教学版，不是恢复入口（DEPRECATED）**：这个模块保留下来只为讲清楚"恢复是怎么回事"。
它**不恢复 Run 状态机、不校验冻结的绑定/权限快照**，而是把历史**拼成一条 user 消息**
再从头 `loop.run()` —— 它一被当成真实恢复路径引用，就会静默丢掉中间状态（审批中/暂停/工具
执行到一半），而这正是恢复最难的局部。

产品代码与运维请用：
  · `runtime/recovery.py`（`RecoveryController`）：真正的恢复链（含绑定校验、权限快照）
  · `runtime/worker.py`（`RecoveryWorker`）：后台自动恢复扫描
  · 运维入口：`GET /recovery/plan`、CLI 的恢复子命令

思路（教学版）：
  1. 每次对话都存进 SQLite。
  2. 如果程序崩了/重启了，用它把上次的对话历史读回来，
     再用 AgentLoop 继续，不会从零开始。
"""
from __future__ import annotations

import warnings

from warden_agent.loop.loop import AgentLoop
from warden_agent.model.model import Message
from warden_agent.store.sqlite import SqliteStore

_DEPRECATION_MSG = (
    "store.recovery.RecoveryAssistant 是教学版恢复（会把历史拼成一条 user 消息重跑），"
    "不恢复 Run 状态机也不校验冻结绑定；产品/运维请用 runtime/recovery.py 的 "
    "RecoveryController（或走 GET /recovery/plan）。"
)


class RecoveryAssistant:
    """把一个 SqliteStore 和一个 AgentLoop 组合起来，提供"接着聊"的能力。

    **教学用**：构造时会发 `DeprecationWarning`。为什么不用"只在文档里写一句"：
    文档拦不住 `from ... import RecoveryAssistant`，而警告能在 CI（`filterwarnings=error`）
    和线上日志里当场暴露。
    """

    def __init__(self, store: SqliteStore, loop: AgentLoop) -> None:
        warnings.warn(_DEPRECATION_MSG, DeprecationWarning, stacklevel=2)
        self.store = store
        self.loop = loop

    def resume(self, run_id: str, new_user_text: str) -> str:
        """在某个 run_id 的历史基础上，追加一句用户的话，继续跑 Agent。

        如果这个 run_id 之前没有历史，就当新任务开始。

        注意：这是**教学版**做法——把历史压成文本再重跑，丢掉的消息结构（tool_call/审批
        状态）无法还原，所以只能用于看不懂恢复链路时的最小演示。
        """
        # 1. 把上次的对话历史读回来（有就续上，没有就空着）
        history = self.store.load_messages(run_id)
        # 2. 由于我们的 AgentLoop.run 每次是新建一个会话，教学版做法：
        #    把历史 + 新问题一起拼进一条"用户消息"里，模拟"接着上下文聊"。
        #    （更完整的实现会恢复整个 Run 状态机，这里是简化演示。）
        context = "\n".join(
            f"[{m.role}] {m.content}" for m in history if m.content
        )
        combined = f"{context}\n[user] {new_user_text}" if context else new_user_text
        result = self.loop.run(combined)
        self.append_run(run_id, result.messages)
        return result.text

    def append_run(self, run_id: str, messages: list[Message]) -> None:
        """把一轮跑出来的全部对话追加写进数据库（存档）。"""
        for m in messages:
            self.store.append_message(run_id, m)
