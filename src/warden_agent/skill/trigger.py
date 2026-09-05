"""技能触发判断 —— 按任务意图，选对"该用哪份操作手册"。

背景（为什么需要它）：
  技能系统（SKILL.md）用**渐进披露**——技能平时只在目录里，被激活才注入正文。
  那"谁来决定该激活哪个技能"？理想情况下模型自己判断，但工具/技能一多，模型可能：
    - 该触发"写周报"技能时，去翻了个毫不相关的"运维手册"；
    - 或者干脆不动手，把技能晾在目录里。
  出色做法是加一道**技能触发判断**：根据当前任务意图，用确定性规则"预热"出最该用的技能，
  再让模型确认触发。这既省了模型空想，也让"渐进披露"真正落到了"按需取用"。

本模块的 **SkillTriggerRouter（技能触发器）**：
  - 输入：当前任务 + 一份技能目录（SkillCatalog）。
  - 输出：按"意图匹配度"从高到低排好的技能候选列表（带命中分数和理由）。
  - 判定：用技能的关键词（name / description / 正文种子词）与任务做重叠打分，
    分数高 = 意图像、值得触发。不打模型、确定可测。
  - **工具自解释（能力层"长"进系统）**：匹配信号统一走 `tool.trigger.tokens`
    （英文词 + 中文相邻双字 + 滤泛词），与 intent 路由同源同一套中文处理；
    暴露的 `skill.trigger.pick` 工具自带 `triggers` 元数据，让意图路由知道
    "什么时候该触发技能路由"，而不是靠手配。

设计要点：
  - 非强制：路由器的输出是"建议"，真实触发仍由 Agent（或用户）确认——保持渐进披露的"按需"精神。
  - 可审计：每条建议都带"命中理由"（哪些词对上了），配合技能信任快照，能讲清楚"为什么用这份技能"。
  - 与既有能力协同：它可作为 `skill_trigger.pick` 工具暴露给 Agent，或作为 loop 里的
    预处理帮模型"预热"候选技能——都能接进能力层与 loop。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from warden_agent.skill.skill import SkillCatalog, SkillMetadata
from warden_agent.tool.trigger import tokens

if TYPE_CHECKING:
    from warden_agent.tool.catalog import ToolSpec


@dataclass
class SkillTriggerVerdict:
    """一条技能触发建议。"""

    alias: str          # 技能别名
    name: str           # 技能名（frontmatter name）
    score: int          # 意图匹配分（越高越像）
    hits: tuple[str, ...]  # 命中的关键词（可解释"为什么推荐它"）

    @property
    def reason(self) -> str:
        return f"任务提到：{'、'.join(self.hits)}"


def _skill_signal_tokens(md: SkillMetadata, body_seed: str) -> set[str]:
    """取一份技能的"匹配信号"：名字 + 描述 + 正文前若干字。

    用"种子词"而非全文，是为了让匹配更聚焦（贴合渐进披露——还没激活就不读完全部正文，
    只扫开头几十个字判断意图）。
    """
    text = f"{md.name} {md.description} {body_seed}"
    return tokens(text)


class SkillTriggerRouter:
    """按任务意图给技能目录里的技能打分排序，选出"最该触发"的候选。"""

    def __init__(self, catalog: SkillCatalog, *, top_k: int = 3) -> None:
        self.catalog = catalog
        self.top_k = top_k
        # 预计算每个技能的匹配信号（不依赖模型，构造时算好一次）
        self._signals: dict[str, set[str]] = {}
        for alias in catalog.aliases():
            binding = catalog.find(alias)
            if binding is None:
                continue
            md = binding.metadata()
            body_seed = binding.content().body[:120] if binding.content().body else ""
            self._signals[alias] = _skill_signal_tokens(md, body_seed)

    def pick(self, task: str) -> list[SkillTriggerVerdict]:
        """给定任务，返回按匹配度排序的技能触发建议（无匹配则空列表）。"""
        q = tokens(task)
        if not q:
            return []
        scored: list[tuple[int, str, tuple[str, ...]]] = []
        for alias, sig in self._signals.items():
            hits = tuple(sorted(q & sig))
            if hits:
                scored.append((len(hits), alias, hits))
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        out: list[SkillTriggerVerdict] = []
        for score, alias, hits in scored[:self.top_k]:
            binding = self.catalog.find(alias)
            name = binding.metadata().name if binding else alias
            out.append(SkillTriggerVerdict(
                alias=alias, name=name, score=score, hits=hits))
        return out

    def describe_candidates(self, task: str) -> str:
        """把最该触发的一组技能拼成一段"预热提示"，可注入上下文或作为工具返回。"""
        verdicts = self.pick(task)
        if not verdicts:
            return "当前任务与已登记技能无明显匹配，不需要触发技能。"
        lines = ["[技能触发建议]"]
        for v in verdicts:
            lines.append(f"- 技能 {v.name}（别名 {v.alias}）：{v.reason}")
        lines.append("如需使用，请激活对应技能；如不合适可不触发。")
        return "\n".join(lines)

    def tool(self) -> ToolSpec:
        """把触发器暴露成一张技能卡：模型把任务给它，它回"该触发哪个技能"的建议。

        工具名：skill.trigger.pick。自带 `triggers` 元数据——描述"什么时候该用它"，
        让 intent 路由能从工具自身识别出触发信号（能力自解释，不手配映射表）。
        """
        from warden_agent.tool.catalog import function_tool

        @function_tool(
            "skill.trigger.pick",
            "根据任务判断该触发哪个技能（SKILL.md 操作手册）。当你知道目录里有技能、"
            "但不确定该用哪个时调用它，返回技能触发建议。",
            {"type": "object",
             "properties": {"task": {"type": "string", "description": "要完成的任务描述"}},
             "required": ["task"]},
            pure=True,
            triggers=("技能", "skill", "手册", "触发", "激活"),
        )
        def pick_tool(task: str) -> str:
            return self.describe_candidates(task)

        return pick_tool


def build_skill_trigger(catalog: SkillCatalog) -> SkillTriggerRouter:
    """便捷构造。"""
    return SkillTriggerRouter(catalog)


__all__ = ["SkillTriggerRouter", "SkillTriggerVerdict", "build_skill_trigger"]

