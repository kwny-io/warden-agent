"""从对话里抽出"值得记的事实"成为候选 + 暴露成 Agent 能用的记忆工具。

  - 不用模型推断，用确定性规则从对话里挖候选事实（如"用户偏好 X"、"结论是 Y"）。
  - 挖出的候选走 MemoryService.propose（PENDING），由人工/策略 approve 后才入库。
  - 暴露四张技能卡：memory.remember（入队候选）/ memory.remember_text（从用户原话自动抽候选）
    / memory.confirm（确认生效）/ memory.recall（取用）。

【为什么 remember 不直接生效（曾经的不诚实之处）】这张卡的工具描述一直写的是
"调用后先进入待确认候选"，但实现里两个分支都直接 `approve`——绕过了候选区。
接口语义与实现相反比"没有确认环节"更坑：用户以为有问一道，实际直接写入。
现在改成真的两阶段，并且把"生效"那一步（`memory.confirm`）特意标为非 pure：
在 HTTP 产品路径下，"只是一条候选"不需要人工批准，"候选变生效"才会被策略拦下等人点确认
（见 `web/run_server.py` 的 `_POLICY_BENIGN_TOOLS`）。
"""

from __future__ import annotations

import re
from collections.abc import Callable

from warden_agent.memory.models import (
    MemoryActor,
    MemoryContent,
    MemoryKind,
    MemoryScope,
)
from warden_agent.memory.owner import current_owner
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
    owner: str = "",
) -> list[str]:
    """从用户消息里挖候选事实并入队（PENDING）。返回新候选的 uid 列表。

    `owner` **要由调用方给出真实归属**（产品路径 = 当前会话用户）：
    漏传会让候选落进"空归属"桶里，多用户部署下等于把 A 的偏好记成公共记忆，
    而 B 在确认时就能把手伸到它。
    """
    ids: list[str] = []
    for key, text in extract_facts(user_text):
        proposal = service.propose(
            scope, key, MemoryContent(kind=MemoryKind.TEXT, text=text),
            actor=MemoryActor("extractor", actor.split("-")[0]),
            owner=owner,
        )
        ids.append(proposal.item.uid)
    return ids


# ---- 暴露成 Agent 技能卡 ----
def make_memory_tools(
    service: MemoryService,
    scope: MemoryScope = MemoryScope.SESSION,
    owner: str | Callable[[], str] | None = None,
) -> list[ToolSpec]:
    """造四张记忆技能卡：remember（入队候选）+ remember_text（从原话自动抽候选）
    + confirm（确认生效）+ recall（取用）。

    `owner` 决定这张卡存/取的记忆归谁：
      - 不传（None）→ 运行期从 `memory.owner.current_owner()` 读（会话驱动时设置）——
        这是产品路径的用法：目录只建一次，归属在运行期确定；
      - 传字符串 / 取值函数 → 固定归属（SDK/测试用）。
    **归属不来自工具入参**，模型无法通过参数声称"我是别人"。
    """

    def _owner() -> str:
        if owner is None:
            return current_owner()
        return owner() if callable(owner) else owner

    @function_tool(
        "memory.remember",
        "把一条用户偏好/事实提交为记忆候选。注意：仍需要确认才生效——"
        "确认请调 memory.confirm（或在控制台的记忆面板确认）；未确认的候选在候选区里。",
        {"type": "object",
         "properties": {"key": {"type": "string"}, "text": {"type": "string"}},
         "required": ["key", "text"]},
        pure=False,
    )
    def remember(key: str, text: str) -> str:
        proposal = service.propose(
            scope, key, MemoryContent(kind=MemoryKind.TEXT, text=text),
            owner=_owner(),
        )
        uid = proposal.item.uid
        if proposal.item.conflicts_with:
            return (
                f"已进入待确认候选 [{scope.name}:{key}]：{text}（候选 {uid}）。"
                f"与已有记忆冲突，确认时会替掉旧条目——请调 memory.confirm 确认。"
            )
        return (
            f"已进入待确认候选 [{scope.name}:{key}]：{text}（候选 {uid}）。"
            f"请调 memory.confirm 确认后生效。"
        )

    @function_tool(
        "memory.confirm",
        "确认某键的待确认候选，让它真正生效（同键的候选全部生效，新的替掉旧的）。"
        "非纯操作：默认策略下这一步会等人批准。",
        {"type": "object",
         "properties": {
             "key": {"type": "string", "description": "要确认的记忆键，如 preference"},
             "replace_existing": {
                 "type": "boolean",
                 "description": "与已有记忆冲突时是否替掉旧的（默认 true）",
             }},
         "required": ["key"]},
        pure=False,
    )
    def confirm(key: str, replace_existing: bool = True) -> str:
        who = _owner()
        mine = [c for c in service.candidates(scope, owner=who) if c.item.key == key]
        if not mine:
            return f"[{scope.name}:{key}] 没有待确认的候选（用 memory.remember 先提交）"
        activated: list[str] = []
        for proposal in mine:
            item = service.approve_candidate(
                proposal.item.uid, scope=scope, owner=who,
                replace_existing=replace_existing,
            )
            if item is not None:
                activated.append(item.content.text)
        if not activated:
            return f"[{scope.name}:{key}] 的候选已不在待确认状态（可能同时被别的进程确认/拒绝了）"
        return f"已确认 [{scope.name}:{key}]：{'；'.join(activated)}"

    @function_tool(
        "memory.remember_text",
        "把一段**用户原话**交给规则抽取器，自动把里面“值得记的事实”变成待确认候选"
        "（如“我喜欢简洁回答”→“preference”候选、“结论是 X”→“fact”候选）。"
        "适用：用户已经说了一大句、你不想自己拆句子时。"
        "局限：规则式抽取（只认“喜欢/偏好/结论是/我叫…”这类句式），可能抽不准或抽不到；"
        "抽出来的仍然只是**候选**，不会生效，仍需 memory.confirm。",
        {"type": "object",
         "properties": {"text": {"type": "string", "description": "用户说过的原话"}},
         "required": ["text"]},
        pure=False,
    )
    def remember_text(text: str) -> str:
        ids = propose_from_text(service, scope, text, owner=_owner())
        if not ids:
            return (
                "这段话里没抽出可记的事实（规则式抽取只认“我喜欢/偏好…”“结论是…/记住…”这类句式）。"
                "如果是别的事实，请自己归纳后用 memory.remember 提交。"
            )
        return (
            f"已抽出 {len(ids)} 条候选并入队（仍待确认）：{', '.join(ids)}。"
            "请调 memory.confirm 确认后生效。"
        )

    @function_tool(
        "memory.recall",
        "取回当前上下文里与关键词相关的记忆。",
        {"type": "object",
         "properties": {"key": {"type": "string", "description": "要查的记忆键，如 preference"}},
         "required": ["key"]},
        pure=True,
    )
    def recall(key: str) -> str:
        text = service.recall_text(scope, key=key, owner=_owner())
        return text if text else f"没有找到 [{scope.name}:{key}] 相关记忆"

    return [remember, remember_text, confirm, recall]
