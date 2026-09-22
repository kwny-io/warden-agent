"""认知步骤的**共享实现** —— 让两套主循环用同一份，而不是各写一遍。

背景：`AgentLoop`（demo / 评测 / 多 Agent 用）有阶段规划、意图路由、记忆按需取用、
上下文裁剪；而会话侧 `AgentSession`（HTTP / CLI，也就是**产品路径**）另有一套循环，
这四件事**一件都没有**。所以 README 里"会思考的认知循环"在产品路径上当时是不成立的。

修法不是把两套循环合并（会话侧还要状态机、审批挂起、流式，合并风险高），
而是把这四步抽到本模块，**两边都调用它** —— 一套实现，两个调用方。
这样"产品路径"和"demo 路径"在认知行为上不会再分叉。

（本模块只依赖下层工具，不 import `loop.py`，避免循环导入。）
"""

from __future__ import annotations

from typing import Any

from warden_agent.model.model import Message

# 上下文裁剪时"最近保留多少条完整消息"
_KEEP_RECENT = 6


# ---------------- 上下文管理 ----------------


def summarize(msgs: list[Message]) -> str:
    """轻量摘要：把被裁掉的早期历史里 assistant 的话提炼成一句要点。

    这是拿"被裁内容里最像结论的话"拼的简版摘要；真实系统可换成模型生成摘要。
    """
    parts = [
        m.content for m in msgs
        if m.role == "assistant" and m.content and not m.content.startswith("[调用工具")
    ][-3:]
    parts = [p for p in parts if p and p.strip()]
    if not parts:
        return "早期交互"
    body = " | ".join(p.strip() for p in parts)
    return f"先后谈及: {body}"


def manage_context(messages: list[Message], max_context_chars: int) -> list[Message]:
    """对话太长时压缩上下文：裁掉早期历史并留一句摘要，只保留最近窗口 + 所有 system。

    `max_context_chars <= 0` 表示不裁剪（原样返回）。用字符数近似 token 量。
    裁剪只动"历史对话"（assistant / user / tool 交错的老部分），system 提示始终保留；
    最近 `_KEEP_RECENT` 条完整保留，保证能正常收尾。
    """
    if max_context_chars <= 0:
        return messages
    total = sum(len(m.content or "") for m in messages)
    if total <= max_context_chars:
        return messages
    output: list[Message] = [m for m in messages if m.role == "system"]
    # 从最早的"非 system"开始留最近 _KEEP_RECENT 条作为 recent，其余算 trimmed
    history = [m for m in messages if m.role != "system"]
    trim_n = max(len(history) - _KEEP_RECENT, 0)
    trimmed = history[:trim_n]
    recent = history[trim_n:]
    if trimmed:
        output.append(Message(
            role="system",
            content="[早期对话摘要] " + summarize(trimmed),
        ))
    output.extend(recent)
    return output


# ---------------- 记忆：按需取用 ----------------


def recall_context(memory: Any, scope: Any, user_text: str,
                   owner: Any = None) -> str:
    """按当前问题检索相关记忆，返回可注入系统提示的记忆上下文（无命中返回空串）。

    【取舍】不是全量塞记忆，而是按 `user_text` 与每条记忆做**关键词重叠**判断，
    只把"和当前问题相关"的拼成一段注入；不相关的丢弃（省 token、不干扰）。

    `scope` 可以是取值函数（如 `self.memory_scope`）或作用域对象本身。
    `owner` 是**归属者**（用户 id，同样支持取值函数）：面向用户的路径必须传，
    否则会把别人的记忆召回进当前用户的提示词（跨租户投毒）。None = 不过滤（SDK/测试）。
    """
    if memory is None:
        return ""
    scope_value = scope() if callable(scope) else scope
    owner_value = owner() if callable(owner) else owner
    try:
        items = memory.recall(scope_value, owner=owner_value)
    except Exception:  # noqa: BLE001 - 记忆不可用绝不拖垮主循环
        return ""
    q_tokens = _tokens(user_text)
    if not q_tokens:
        return ""
    hits = [it for it in items if _tokens(it.content.text) & q_tokens]
    if not hits:
        return ""
    lines = "\n".join(f"- [{it.key}] {it.content.text}" for it in hits)
    return f"[你的记忆,供参考]\n{lines}"


def _tokens(text: str) -> set[str]:
    """拆"有意义的检索词"集合（与 intent / skill 触发同一套分词，不重复实现）。"""
    from warden_agent.tool.trigger import tokens as _trigger_tokens

    return _trigger_tokens(text)


# ---------------- 阶段规划 ----------------


def plan_context(planner: Any, user_text: str, stage: int = 0) -> str:
    """复杂任务才拆阶段并注入阶段目标；简单任务或无 planner 返回空串。"""
    if planner is None:
        return ""
    plan = planner.build(user_text)
    if plan is None or not getattr(plan, "is_complex", False):
        return ""
    return str(planner.context(plan, stage))


# ---------------- 意图路由（调用前的"该不该调"校验） ----------------


def intent_hint(
    intent: Any,
    tool_schema: dict[str, Any],
    call: Any,
    user_text: str,
    messages: list[Message],
) -> str | None:
    """调用前校验"这个请求真的需要调这个工具吗"。

    判为"疑似误调"时返回**要喂回模型的提醒文案**（调用方据此跳过执行），否则 None。
    这是"预防性"的；真正的拒绝仍由审批门禁负责。
    """
    if intent is None:
        return None
    history = " ".join(m.content for m in messages if m.content)
    verdict = intent.relay(call.name, tool_schema, user_text, history)
    if getattr(verdict, "action", None) == "hint":
        return str(verdict.message)
    return None
