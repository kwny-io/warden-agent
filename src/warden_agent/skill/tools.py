"""Skill → Tool 桥：把一个技能包包装成 Agent 能调用/激活的技能卡。

  - 渐进披露：Skill 平时在目录里，只有 Agent 明确要"用某技能"时，
    才通过 skill_to_tool 把它转成一张可调用的工具，正文作为注入指令返回。
  - 这样 Agent 看到的是能力清单，真正要用时才拿到详细指令（渐进披露）。
"""

from __future__ import annotations

from warden_agent.skill.skill import (
    FrozenSkillBinding,
    SkillCatalog,
    SkillRequirementError,
    missing_requires,
)
from warden_agent.tool.catalog import ToolSpec, function_tool


def skill_to_tool(
    binding: FrozenSkillBinding,
    *,
    available: set[str] | None = None,
) -> ToolSpec:
    """把一份技能转成一张"skill.<alias>.run"的技能卡：调用 = 激活（返回注入指令）。

    入参：目标技能别名。返回：该技能的激活正文（可直接作为系统指令注入）。

    `available`：可选的"当前可用工具/依赖名集合"。给了就在激活前校验技能的
    `requires`，缺依赖则抛 `SkillRequirementError`（不满足前置条件不允许激活）。
    不可信技能（非 trusted）会在 `binding.activate()` 处被拒绝。
    """
    md = binding.metadata()

    @function_tool(
        f"skill.{md.name}.run",
        f"激活并使用技能 {md.name}：{md.description}",
        {"type": "object",
         "properties": {"goal": {"type": "string", "description": "希望用该技能完成的目标"}},
         "required": ["goal"]},
        pure=True,
    )
    def run(goal: str) -> str:
        if available is not None:
            missing = missing_requires(binding, available)
            if missing:
                raise SkillRequirementError(
                    f"技能 {md.name!r} 缺少依赖: {', '.join(missing)}"
                )
        return f"{binding.activate()}\n目标：{goal}"

    return run


def skill_activation_tool(catalog: SkillCatalog, alias: str) -> ToolSpec:
    """把目录里某技能转成一张技能卡（找不到则返回一张提示卡）。"""
    binding = catalog.find(alias)
    if binding is None:
        return function_tool(
            f"skill.{alias}.run",
            f"激活技能 {alias}",
            {"type": "object",
             "properties": {"goal": {"type": "string"}},
             "required": ["goal"]},
        )(lambda goal: f"技能 {alias} 未登记")
    return skill_to_tool(binding)
