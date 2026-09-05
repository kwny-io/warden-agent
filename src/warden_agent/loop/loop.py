"""AgentLoop：AI 干活的主循环 —— 整个项目最核心的一块。

执行循环大致是：

    1. 接收用户的一句话（请求）
    2. 把"现在要做什么"交给模型思考   -> 模型回：要么说结论，要么"我想调这个工具"
    3. 如果模型想调工具 -> 在工具箱里查这个工具 -> 执行 -> 把结果放回对话里 -> 回到第2步
    4. 如果模型说完了    -> 把最终回答拿给用户 -> 结束

用一个通俗比喻：你请了个"助理"(AgentLoop)，助理每次有事都先问"大脑"(模型)该怎么处理。
大脑说"打电话给气象局"，助理就去打(执行Tool)，把听到的天气记下来，再回来问大脑下一步。
直到大脑说"好了，这就是答案"，助理把答案交给你，收工。

为什么这样设计是好的？
- 大脑(模型)只管"思考"，动手(工具)交给助理，职责分开，互相不掺和。
- 中间每一步、每句对话都被记录下来，所以才能"存档"(持久化)和"查账"(可审计)。
- 循环内部可以嵌各种规则(比如做高危动作前要先审批)，不破坏整体结构。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from warden_agent.model.fake import FakeModel
from warden_agent.model.model import AgentChatModel, ChatRequest, Message
from warden_agent.policy.policy import Decision, PolicyEngine
from warden_agent.tool.catalog import ToolCatalog


@dataclass
class AgentReply:
    """Agent 最终回给用户的话 + 整个过程的对话记录。"""

    text: str
    messages: list[Message]  # 完整对话历史（可用于持久化/审计）


class PolicyDenied(Exception):
    """策略拒绝该次工具调用。"""


class AgentLoop:
    """用『模型 + 工具箱 + 审批策略』驱动的 Agent 主循环。

    【loop 深度①】工具调用失败自恢复：模型调工具出错时，不直接崩溃，
    而是把错误信息喂回模型，让它自己纠正（改参数 / 换工具）重试。
    用 `max_tool_retries` 控制每个工具调用最多纠正几次，防止无限打转。

    【loop 深度②】记忆接入 + 取舍：如果传入了 `memory`(MemoryService)，
    - 取用端：循环开始前按当前问题检索相关记忆(recall)，注入上下文(按需,不是全量塞)；
    - 写入端：`remember()` 用启发式判断"这条值不值得记"，值得才 propose(取舍)。
    """

    def __init__(
        self,
        model: AgentChatModel,
        catalog: ToolCatalog,
        system_prompt: str = "你是一个能使用工具的助手。",
        max_iterations: int = 10,
        policy_engine: PolicyEngine | None = None,
        max_tool_retries: int = 2,
        memory: Any = None,
        memory_scope: Any = None,
        max_context_chars: int = 0,
        planner: Any = None,
        intent: Any = None,
    ) -> None:
        self.model = model
        self.catalog = catalog
        self.system_prompt = system_prompt
        self.max_iterations = max_iterations  # 最多循环多少轮，防止死循环
        self.max_tool_retries = max_tool_retries  # 单个工具调用失败最多纠正几次
        self.max_context_chars = max_context_chars  # 上下文裁剪阈值(0=不裁剪,见 loop 深度④)
        # 没给就建一个空的安全判定器（默认全部放行）
        self.policy = policy_engine or PolicyEngine()
        self.memory = memory  # 可选：记忆中枢(见 loop 深度②)
        from warden_agent.memory import MemoryScope
        self._memory_scope = memory_scope or MemoryScope.SESSION
        # 【loop 深度③】阶段规划器（可选）。None 则不拆阶段（基础 loop）。
        self.planner = planner
        # 【loop 深度⑤】工具意图路由器（可选）。None 则不做"调用前意图校验"（只防打转）。
        self.intent = intent

    def run(self, user_text: str) -> AgentReply:
        """处理一句用户指令，返回最终回答。"""
        # 1. 组装对话：系统指令 + 用户这句话 + 可用的技能卡（+ 相关记忆上下文）
        messages: list[Message] = [Message(role="system", content=self.system_prompt)]
        # 【loop 深度②】取用端：按当前问题检索相关记忆,注入上下文(按需取用)
        memory_context = self._recall_context(user_text)
        if memory_context:
            messages.append(Message(role="system", content=memory_context))
        # 【loop 深度③】阶段规划：先判断复杂度,复杂则拆阶段,渐进注入当前阶段目标
        plan = None
        if self.planner is not None:
            plan = self.planner.build(user_text)
            if plan is not None and getattr(plan, "is_complex", False):
                messages.append(Message(role="system", content=self.planner.context(plan, 0)))
        messages.append(Message(role="user", content=user_text))
        tools = [t.to_openai_schema() for t in self.catalog.all()]

        # 追踪"同一次多工具调用里，哪个工具已经失败重试了几次"，
        # 防止模型用同样错误的参数反复打转。
        tool_retries: dict[str, int] = {}
        # 【loop 深度⑤】记录"已执行过"的工具调用签名,检测模型原地打转(同一调用反复发)
        seen_calls: list[str] = []

        # 2. 进入循环
        for _ in range(self.max_iterations):
            # 【loop 深度④】上下文管理：超长时把早期历史压缩成摘要,只保留最近窗口
            managed = self._manage_context(messages)
            response = self.model.chat(ChatRequest(messages=managed, tools=tools))

            # 情况A：模型想调用工具
            if response.tool_calls:
                for call in response.tool_calls:
                    # 记录"模型想调工具"这句（可审计）
                    assistant_note = Message(
                        role="assistant",
                        content=f"[调用工具 {call.name}]",
                        tool_call=call,
                    )
                    messages.append(assistant_note)
                    # 【门禁】执行前先过审批策略；被 DENY 直接拒绝，绝不执行
                    verdict = self.policy.evaluate(call.name, call.arguments)
                    if verdict.decision == Decision.DENY:
                        raise PolicyDenied(
                            f"策略拒绝执行 {call.name!r}: {verdict.reason}"
                        )
                    # 教学版：ASK 直接执行（真实系统会挂起等用户点批准，这里简化）

                    # 【loop 深度⑤】工具意图判断（调用前预防误调）：
                    # intent 路由器校验"这个请求真的需要调这个工具吗"。
                    # 若判定为"疑似误调",把提醒喂回模型让它确认/改选,而不是立即执行一个
                    # 可能多余的调用。这是"预防性"的;真正的拒绝仍由上面的审批门禁负责。
                    if self.intent is not None:
                        tool_schema = self._schema_of(call.name)
                        iv = self.intent.relay(call.name, tool_schema, user_text, " ".join(
                            m.content for m in messages if m.content))
                        if iv.action == "hint":
                            messages.append(Message(role="tool", content=iv.message,
                                                    tool_call=call))
                            continue

                    # 【loop 深度①】工具调用失败自恢复：
                    # 尝试执行；失败不崩溃，把错误喂回模型让它自己纠正。
                    result, error = self._safe_execute(call.name, call.arguments)
                    if error is not None:
                        count = tool_retries.get(call.id, 0) + 1
                        tool_retries[call.id] = count
                        if count <= self.max_tool_retries:
                            # 没超上限：把错误喂回模型，让它看着错误自己改
                            messages.append(Message(
                                role="tool",
                                content=f"[工具 {call.name} 执行失败，请修正后重试] {error}",
                                tool_call=call,
                            ))
                        else:
                            # 超上限：放弃这个工具，明确告知模型此路不通，换别的招
                            messages.append(Message(
                                role="tool",
                                content=(
                                    f"[工具 {call.name} 在 {self.max_tool_retries} 次尝试后"
                                    f"仍失败，放弃该工具，改用其他方式完成任务] "
                                    f"最后一次错误: {error}"
                                ),
                                tool_call=call,
                            ))
                        continue
                    # 【loop 深度⑤】意图判断：检测"原地打转"。
                    # 只有当工具**成功执行**后才把调用签名记入 seen_calls——
                    # 失败的调用不记（那样会短路深度①的重试机制，见下）。
                    # 若同一"工具名+参数"已成功执行过、却在后续又原样重发，
                    # 说明模型在打转：喂回提示让它改变策略，而不是无限重发。
                    call_sig = f"{call.name}({sorted(call.arguments.items())})"
                    if call_sig in seen_calls:
                        messages.append(Message(
                            role="tool",
                            content=(
                                f"[注意] 你已调用过 {call_sig} 且未产生新进展,"
                                f"请不要再重复同一调用——换一种做法,或直接给出最终回答。"
                            ),
                            tool_call=call,
                        ))
                        continue
                    seen_calls.append(call_sig)
                    # 成功：把工具结果放回对话，让模型"看到"结果后再决定下一步。
                    # 必须携带 call 引用使 tool_call_id 与 assistant 一致，否则真实 API 报 400。
                    messages.append(Message(role="tool", content=str(result), tool_call=call))
                continue  # 回到循环顶部，让模型基于结果再想

            # 情况B：模型直接给出了最终回答
            if response.content is not None:
                messages.append(Message(role="assistant", content=response.content))
                return AgentReply(text=response.content, messages=messages)

        # 3. 循环次数用尽还没结束 = 视为异常，防止死循环
        raise RuntimeError("AgentLoop 迭代超过上限，任务未收敛（可能模型一直在调用工具）")

    def _schema_of(self, name: str) -> dict[str, Any]:
        """取某工具说明书（供意图路由器识别触发信号）；未注册返回空 dict。"""
        try:
            spec = self.catalog.get(name)
            return {
                "description": spec.description,
                "parameters": spec.parameters_schema,
            }
        except KeyError:
            return {}

    def _safe_execute(self, name: str, arguments: dict[str, Any]) -> tuple[Any, str | None]:
        """执行工具；失败返回 (None, 错误信息)，成功返回 (结果, None)。

        把"未注册工具 / 参数不合法 / 工具内部异常"都统一转成可喂回模型的错误信息，
        而不是让异常直接击穿整个 loop——这就是"失败自恢复"能工作的前提。
        """
        try:
            return self.catalog.execute(name, arguments), None
        except Exception as e:  # noqa: BLE001 - 工具错误要全部转为可恢复信息
            return None, f"{type(e).__name__}: {e}"

    # ---- 【loop 深度④】上下文管理：裁剪 + 摘要 ----

    def _manage_context(self, messages: list[Message]) -> list[Message]:
        """对话太长时压缩上下文：裁掉早期历史并留一句摘要，只保留最近窗口 + 所有 system。

        阈值 `max_context_chars` 为 0 表示不裁剪(原样返回)。
        用字符数近似 token 量。裁剪只动"历史对话"(assistant/user/tool 交错的老部分)，
        system 提示始终保留；最近 `_KEEP_RECENT` 条完整保留，保证能正常收尾。
        """
        if self.max_context_chars <= 0:
            return messages
        total = sum(len(m.content or "") for m in messages)
        if total <= self.max_context_chars:
            return messages
        output: list[Message] = [m for m in messages if m.role == "system"]
        trimmed: list[Message] = []
        recent: list[Message] = []
        # 从最早的"非 system"开始留最近 _KEEP_RECENT 条作为 recent,其余算 trimmed
        history = [m for m in messages if m.role != "system"]
        trim_n = max(len(history) - _KEEP_RECENT, 0)
        trimmed = history[:trim_n]
        recent = history[trim_n:]
        if trimmed:
            output.append(Message(
                role="system",
                content="[早期对话摘要] " + self._summarize(trimmed),
            ))
        output.extend(recent)
        return output

    @staticmethod
    def _summarize(msgs: list[Message]) -> str:
        """轻量摘要：把裁剪掉的早期历史里 assistant 的话提炼成一句要点。

        这是拿"被裁的内容里最像结论的话"拼的简版摘要；真实系统可换成模型生成摘要。
        """
        parts = [
            m.content for m in msgs
            if m.role == "assistant" and not m.content.startswith("[调用工具")
        ][-3:]
        parts = [p for p in parts if p and p.strip()]
        if not parts:
            return "早期交互"
        body = " | ".join(p.strip() for p in parts)
        return f"先后谈及: {body}"

    # ---- 【loop 深度②】记忆：按需取用 + 取舍写入 ----

    def _recall_context(self, user_text: str) -> str:
        """按当前问题检索相关记忆，返回可注入系统提示的记忆上下文。

        【取舍】不是全量塞记忆，而是按 `user_text` 与每条记忆做**关键词重叠**判断，
        只把"和当前问题相关"的记忆拼成一段注入；不相关的丢弃(省 token、不干扰)。
        没命中就返回空串(不注入)。
        """
        if self.memory is None:
            return ""
        try:
            # 取本作用域下所有仍有效的记忆(不依赖 memory 那块粗粒度子串匹配)
            items = self.memory.recall(self.memory_scope())
        except Exception:  # noqa: BLE001 - 记忆不可用绝不拖垮主循环
            return ""
        q_tokens = _tokens(user_text)
        if not q_tokens:
            return ""
        # 只保留与当前问题有"关键词重叠"的条目
        hits = [
            it for it in items
            if _tokens(it.content.text) & q_tokens
        ]
        if not hits:
            return ""
        lines = "\n".join(f"- [{it.key}] {it.content.text}" for it in hits)
        return f"[你的记忆,供参考]\n{lines}"

    def memory_scope(self) -> Any:
        """返回记忆作用域(构造时设定,默认 SESSION)。"""
        return self._memory_scope

    def remember(self, text: str, *, key: str | None = None,
                 reason: str = "") -> bool:
        """【取舍】判断一条信息值不值得记，值得才 propose 进候选区。

        启发式(简版"什么才该记"):
          - 太短(<6 字的一般是废话/临时) → 不记
          - 太长(>500 字) → 不记(可能是一大段输出,不是可复用事实)
          - 命中"用户偏好/事实陈述"这类关键词 → 优先记
        返回值: 是否真的提出了记忆候选(True=提了,False=被取舍掉了/无记忆)。
        """
        if self.memory is None:
            return False
        stripped = text.strip()
        if len(stripped) < 6 or len(stripped) > 500:
            return False  # 太短/太长都"不值得记"
        try:
            self.memory.propose(
                self.memory_scope(),
                key or _default_memory_key(stripped),
                _text_content(stripped),
                reason=reason or "loop remember (启发式取舍)",
            )
            return True
        except Exception:  # noqa: BLE001 - 记忆写入失败不拖垮主循环
            return False


def build_default_loop(catalog: ToolCatalog) -> AgentLoop:
    """快速搭一个默认的 AgentLoop（用本地假模型，离线可跑）。"""
    return AgentLoop(model=FakeModel(), catalog=catalog)


# ---- loop 深度② 记忆辅助（模块级，避开循环导入）----

# loop 深度④：上下文裁剪时,最近保留的消息条数(保证能正常收尾)
_KEEP_RECENT = 6


def _default_memory_key(text: str) -> str:
    """给一条要记的记忆生成一个"可读键"：取前 N 个不含空白的字符。

    只用于 loop 自动记时生成默认键；调用方也可显式传 key。
    """
    cleaned = "".join(ch for ch in text if not ch.isspace())
    return cleaned[:16] or "remembered"


def _text_content(text: str) -> Any:
    """把文本包成 MemoryContent（延迟导入，避免顶层循环导入）。"""
    from warden_agent.memory import MemoryContent
    return MemoryContent(text=text)


def _tokens(text: str) -> set[str]:
    """把一段话拆成"有意义的检索词"集合，用于记忆相关性判断。

    处理两种常见形态：
      - 英文/数字：按空白/下划线拆成词(token)。
      - 中文：整段连续汉字太粗(两个句子的汉字段不会重合)，所以额外产出
        **相邻两字(bigram)**——这样"上海天气怎么样"与"用户常问上海天气"
        能共享 "上海"/"天气" 等词，实现"按需取用"的相关性筛选。
    空串/纯符号返回空集(没有可检索关键词)。
    """
    import re
    s = text.strip()
    if not s:
        return set()
    out: set[str] = set()
    # 英文/数字词
    for w in re.findall(r"[A-Za-z0-9_]+", s):
        out.add(w.lower())
    # 中文连续块 → 产出相邻两字
    for run in re.findall(r"[\u4e00-\u9fff]+", s):
        if len(run) == 1:
            out.add(run)
        else:
            out.update(run[i:i + 2] for i in range(len(run) - 1))
    return out
