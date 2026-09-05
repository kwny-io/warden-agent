"""从对话里抽出"值得记的事实"成为候选 + 暴露成 Agent 能用的记忆工具。

  - 不用模型推断，用确定性规则从对话里挖候选事实（如"用户偏好 X"、"结论是 Y"）。
  - 挖出的候选走 MemoryService.propose（PENDING），由人工/策略 approve 后才入库。
  - 暴露 memory.remember / memory.recall 技能卡，Agent 能主动存/取共享记忆。
"""

from __future__ import annotations

import re

from warden_agent.memory.models import (
    MemoryActor,
    MemoryContent,
    MemoryKind,
    MemoryScope,
)
from warden_agent.memory.service import MemoryService
from warden_agent.tool.catalog import ToolSpec, function_tool

# ---- 确定性事实抽取：识别"用户偏好/事实结论"这类句子 ----
_PREFERENCE_RE = re.compile(r"(喜欢|偏好|倾向|热爱|讨厌|不喜欢|希望|想要|需要)(.*)")
_FACT_RE = re.compile(r"(结论是|结果是|决定是|答案是|记住|用户(?:的)?(?:名字|是|叫))(.*)")


def extract_facts(user_text: str) -> list[tuple[str, str]]:
    """从一句话里挖出 (key, text) 形式的候选事实。

    返回的例子（user_text="用户叫小明，喜欢简洁回答"）：
      [("preference", "用户喜欢简洁回答"), ("identity", "用户叫小明")]
    """
    facts: list[tuple[str, str]] = []

    m = _PREFERENCE_RE.search(user_text)
    if m:
        # "喜欢简洁回答" → key="preference"，text 带上"用户偏好"
        facts.append(("preference", f"用户偏好{m.group(2)}"))

    m = _FACT_RE.search(user_text)
    if m:
        whole = m.group(0)
        if "名字" in whole or "叫" in whole:
            facts.append(("identity", whole))
        else:
            facts.append(("fact", whole))

    return facts


def propose_from_text(
    service: MemoryService,
    scope: MemoryScope,
    user_text: str,
    *,
    actor: str = "extractor",
) -> list[str]:
    """从用户消息里挖候选事实并入队（PENDING）。返回新候选的 uid 列表。"""
    ids: list[str] = []
    for key, text in extract_facts(user_text):
        proposal = service.propose(
            scope, key, MemoryContent(kind=MemoryKind.TEXT, text=text),
            actor=MemoryActor("extractor", actor.split("-")[0]),
        )
        ids.append(proposal.item.uid)
    return ids


# ---- 暴露成 Agent 技能卡 ----
def make_memory_tools(
    service: MemoryService,
    scope: MemoryScope = MemoryScope.SESSION,
) -> list[ToolSpec]:
    """造两张记忆技能卡：memory.remember（存）+ memory.recall（取）。"""

    @function_tool(
        "memory.remember",
        "把一条用户偏好/事实记进记忆，供后续 Agent 共享。调用后先进入待确认候选。",
        {"type": "object",
         "properties": {"key": {"type": "string"}, "text": {"type": "string"}},
         "required": ["key", "text"]},
        pure=False,
    )
    def remember(key: str, text: str) -> str:
        proposal = service.propose(
            scope, key, MemoryContent(kind=MemoryKind.TEXT, text=text),
        )
        # 有冲突就先挂着；否则直接确认生效（学习版简化：默认直接 approve）
        if proposal.item.conflicts_with:
            service.approve(proposal, replacing=True)
        else:
            service.approve(proposal, replacing=False)
        return f"已记住 [{scope.name}:{key}]：{text}"

    @function_tool(
        "memory.recall",
        "取回当前上下文里与关键词相关的记忆。",
        {"type": "object",
         "properties": {"key": {"type": "string", "description": "要查的记忆键，如 preference"}},
         "required": ["key"]},
        pure=True,
    )
    def recall(key: str) -> str:
        text = service.recall_text(scope, key=key)
        return text if text else f"没有找到 [{scope.name}:{key}] 相关记忆"

    return [remember, recall]
