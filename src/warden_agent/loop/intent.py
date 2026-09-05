"""工具意图判断 —— loop 深度⑤：调用工具前，先判断"要不要调、该调哪个"。

背景（为什么需要它）：
  工具一多，模型可能乱调、或该调不调。基础 loop 对"模型想调工具"几乎是照单全收
  （除了审批门禁 + 失败自恢复）。出色的 loop 会在**调用前再确认一步**：
    - 这个请求真的需要调工具吗？还是直接回答就行？
    - 十几个工具里，哪一个才是当前最合适的？

本模块落地的 **ToolIntentRouter（意图路由器）**：
  它在模型抛出某个工具调用后、真正执行前，用**确定性启发式**做一道"意图校验"：
    1. **该不该调**：如果当前问题/上下文里根本没有提到这个工具相关的关键词，
       也没有触发它该被使用的信号，说明模型可能"误调用"了——给模型一个善意的提醒，
       让它确认（而不是直接执行一个可能多余的调用，或粗暴拒绝）。
    2. **该不该换**：对"领域分组的工具"。比如 Model 说想调 `weather.get`，但用户问的是
       "公司年假"，路由器会发现 `weather` 域和当前问题不搭，提示模型是否该用别的工具。

设计要点：
  - **确定性、离线可测**：不靠模型自己"再想一步"（那会多一次模型调用、也更不可控），
    而是用轻量关键词/领域信号做校验，保证测试稳定。
  - **非阻断**：路由器的"提醒"是塞回给模型的一条 system/tool 消息，让模型自己决定
    要不要改；它不硬性拒绝（真正的拒绝由审批门禁 DENT 负责）。
    这保持了"可恢复、可协商"的 Agent 风格。
  - **与 loop 深度①/⑤ 已有机制互补**：
      - 深度① 在"执行失败"后救场；本模块在"执行前"预防误调。
      - loop 里已有的 seen_calls 在"成功后再调用"时防打转；本模块在"第一次调用前"防乱调。
  - **工具自解释（能力层"长"进系统）**：触发词不再靠手配一张 `{工具名: [词]}` 映射表，
    而是由 ToolSpec 自带 `triggers` 元数据 + 从 `description` 自动提取（见 tool.trigger）。
    工具"什么时候该被用"和工具本身长在一起；没有可识别触发词的工具默认放行（不误伤）。
  - 若给了 `reasoner`（让模型说明理由），深度⑤还能在"无触发信号"时**让模型自己说明理由**，
    而不是只靠启发式硬判；不给 reasoner 就用原有启发式提示（离线可测、默认行为不变）。

用法（挂进 AgentLoop）：
    router = ToolIntentRouter(catalog)          # 从工具箱自动长出触发词
    router = ToolIntentRouter(catalog, reasoner=model_reasoner)  # 加"模型说明理由"
    verdict = router.relay(tool_name, tool_schema, user_text, context_text)
    - verdict.action == "proceed"   → 正常执行
    - verdict.action == "hint"      → 不执行，把 verdict.message 喂回模型让它确认/改选
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from warden_agent.tool.trigger import extract_triggers


@dataclass
class IntentVerdict:
    """意图判断的结论。"""

    action: str   # "proceed" 正常执行 / "hint" 给模型一条提醒,先不执行
    message: str = ""  # hint 时要喂回给模型的提醒文案
    reason: str = ""   # 判断依据（审计/调试用）


class ToolIntentRouter:
    """基于"工具自解释触发词" + 领域信号的工具意图校验器。

    - `catalog`: 可选，一个 ToolCatalog。给了它，触发词自动从每个工具的
      `triggers` 元数据 + `description` 提取（能力"长"进系统，不手配）。
    - `triggers` / `domains`: 仍是手动映射（兼容旧用法），但**优先自动提取**；
      显式手配的可叠加在自动之上（手标是"可信第一手"，自动是"兜底"）。
    - `enable_probe`: 是否启用"误调用提醒"（默认开启）。
    - `reasoner`: 可选，一个"让模型替工具调用说一句理由"的调用函数。
      签名为 `reasoner(tool_name, user_text, context_text) -> bool | str`：
        返回 True / 非空理由字符串 → 判定"该调用其实合理"，放行；
        返回 False / 空 → 维持"疑似误调"，提醒。
      给了它，深度⑤就能在"无触发信号"时**让模型自己说明理由**，而不是只靠启发式硬判；
      不给它，就用原有启发式提示（离线可测、默认行为不变）。
    """

    def __init__(
        self,
        catalog: Any = None,
        triggers: dict[str, list[str]] | None = None,
        domains: dict[str, list[str]] | None = None,
        enable_probe: bool = True,
        reasoner: Any = None,
    ) -> None:
        self.catalog = catalog
        # 显式手配的映射（兼容旧用法），"可信第一手"，可叠加在自动提取之上
        self.triggers = _normalize(triggers or {})
        self.domains = _normalize(domains or {})
        self.enable_probe = enable_probe
        self.reasoner = reasoner
        # 从工具箱"长出"的自动触发词缓存：{工具名 -> [触发词]}
        self._auto_triggers: dict[str, list[str]] = {}

    def _auto_trigger_words(self, tool_name: str, tool_schema: dict[str, Any]) -> list[str]:
        """从 ToolSpec 的 triggers 元数据 + description 自动提取触发词（缓存一次）。

        优先查 catalog 里的真实 ToolSpec（有手工 triggers 就取第一手）；
        没有 catalog 或查不到时，退回从传入的 tool_schema.description 提取兜底。
        """
        if tool_name in self._auto_triggers:
            return self._auto_triggers[tool_name]
        extra: tuple[str, ...] = ()
        desc = (tool_schema.get("description") or "")
        if self.catalog is not None:
            try:
                spec = self.catalog.get(tool_name)
                if spec is not None:
                    extra = tuple(getattr(spec, "triggers", ()) or ())
                    desc = spec.description or desc
            except KeyError:
                pass
        words = extract_triggers(desc, extra=extra)
        self._auto_triggers[tool_name] = words
        return words

    def _domain_of(self, tool_name: str) -> str:
        return tool_name.split(".")[0]

    def relay(self, tool_name: str, tool_schema: dict[str, Any],
              user_text: str, context_text: str = "") -> IntentVerdict:
        """对"模型想调 tool_name"做一次意图校验，返回放行或提醒。

        - tool_schema  工具说明书（取 description 作为识别依据之一）
        - user_text    用户本轮问题
        - context_text 当前上下文（对话要点），用于更宽的意图判断
        """
        if not self.enable_probe:
            return IntentVerdict(action="proceed")
        haystack = f"{user_text} {context_text}".lower()

        # 触发词来源（优先级）：
        #   1) 显式手配的工具触发词（第一手）
        #   2) 从 ToolSpec 自动提取的触发词（能力自解释，长在描述里）
        #   3) 领域触发词
        tool_triggers = self.triggers.get(tool_name, [])
        auto_triggers = self._auto_trigger_words(tool_name, tool_schema)
        merged_triggers = tool_triggers + [t for t in auto_triggers if t not in tool_triggers]

        if merged_triggers and any(t in haystack for t in merged_triggers):
            source = "手配触发词" if tool_triggers else "工具自解释触发词"
            return IntentVerdict(action="proceed",
                                 reason=f"命中{source}: {tool_name}")

        # 领域触发词：工具名属于某个域,且该域的关键词在问题里
        domain = self._domain_of(tool_name)
        domain_words = self.domains.get(domain, [])
        if domain_words and any(d in haystack for d in domain_words):
            return IntentVerdict(action="proceed",
                                 reason=f"命中领域 {domain} 的触发词")

        # 兜底：工具自身描述里提到的词出现在问题里（比如知识库工具描述提到"知识""资料"）
        desc = (tool_schema.get("description") or "").lower()
        desc_tokens = [w for w in _words(desc) if len(w) > 1]
        if desc_tokens and any(w in haystack for w in desc_tokens[:8]):
            return IntentVerdict(action="proceed",
                                 reason=f"工具描述命中: {tool_name}")

        # 没有任何触发信号(手配/自动/领域都为空) => 无法判断意图就不该拦（避免误伤）。
        if not merged_triggers and not domain_words:
            return IntentVerdict(action="proceed",
                                 reason=f"工具 {tool_name} 未识别到触发信号,默认放行")

        # 配了信号但都没命中 => 疑似误调。
        # 若给了 reasoner，先让"模型自己说明理由"：模型说合理就放行（深度⑤升级），
        # 否则维持提醒；没给 reasoner 就退回启发式提醒（默认行为不变）。
        if self.reasoner is not None:
            try:
                verdict_from_model = self.reasoner(
                    tool_name, user_text, context_text, tool_schema)
            except Exception:  # noqa: BLE001 - 理由模型挂了不拖垮主循环
                verdict_from_model = False
            if verdict_from_model:
                reason_str = (str(verdict_from_model)
                              if verdict_from_model is not True else "模型判定该调用合理")
                return IntentVerdict(action="proceed",
                                     reason=f"模型说明理由后放行: {reason_str}")
            return IntentVerdict(
                action="hint",
                message=(
                    f"[意图提示] 你想调用工具 {tool_name}，触发信号不足；且模型说明后"
                    f"仍认为不确定。请确认：这个问题真的需要 {tool_name} 吗？"
                    f"如果需要，请说明理由；如果不需要，请直接回答或换更合适的工具。"
                ),
                reason=f"工具 {tool_name} 在上下文中找不到足够触发信号（模型复核后仍不确定）",
            )

        return IntentVerdict(
            action="hint",
            message=(
                f"[意图提示] 你想调用工具 {tool_name}，但当前问题/上下文里似乎没有"
                f"足够的信号支撑这个调用。请确认：这个问题真的需要 {tool_name} 吗？"
                f"如果需要，请说明理由；如果不需要，请直接回答或换更合适的工具。"
            ),
            reason=f"工具 {tool_name} 在上下文中找不到足够触发信号",
        )

    def describe(self, tool_names: list[str]) -> str:
        """给一组工具生成"何时该用"的说明，注入系统提示帮模型选工具（可选增强）。"""
        if not self.enable_probe:
            return ""
        lines = []
        for name in tool_names:
            triggers = self.triggers.get(name)
            domain = self._domain_of(name)
            dom_words = self.domains.get(domain)
            if triggers:
                lines.append(f"- {name}: 当问题提到 {('、'.join(triggers))} 时适用")
            elif dom_words:
                lines.append(f"- {name}: 当问题提到 {('、'.join(dom_words))} 时适用")
        return "\n".join(lines)


def _normalize(mapping: dict[str, list[str]]) -> dict[str, list[str]]:
    return {k: [w.lower() for w in v] for k, v in mapping.items()}


def _words(text: str) -> list[str]:
    import re

    return re.findall(r"[a-z0-9_]+", text)


__all__ = ["ToolIntentRouter", "IntentVerdict"]
