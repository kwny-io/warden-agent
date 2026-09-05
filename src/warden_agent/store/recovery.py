"""恢复助理：让中断的 Agent 从上次存档点接着干。

思路是：
  1. 每次对话都存进 SQLite。
  2. 如果程序崩了/重启了，用它把上次的对话历史读回来，
     再用 AgentLoop 继续，不会从零开始。

更高阶的实现还会恢复"暂停/审批中"这种中间状态、校验冻结的绑定等，
这里我们只演示最核心的"把对话续上"这一步，够你理解恢复是怎么回事。
"""
from __future__ import annotations

from warden_agent.loop.loop import AgentLoop
from warden_agent.model.model import Message
from warden_agent.store.sqlite import SqliteStore


class RecoveryAssistant:
    """把一个 SqliteStore 和一个 AgentLoop 组合起来，提供"接着聊"的能力。"""

    def __init__(self, store: SqliteStore, loop: AgentLoop) -> None:
        self.store = store
        self.loop = loop

    def resume(self, run_id: str, new_user_text: str) -> str:
        """在某个 run_id 的历史基础上，追加一句用户的话，继续跑 Agent。

        如果这个 run_id 之前没有历史，就当新任务开始。
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
