"""Skill → Tool 桥：把一个技能包包装成 Agent 能调用/激活的技能卡。

  - 渐进披露：Skill 平时在目录里，只有 Agent 明确要"用某技能"时，
    才通过 skill_to_tool 把它转成一张可调用的工具，正文作为注入指令返回。
  - 这样 Agent 看到的是能力清单，真正要用时才拿到详细指令（渐进披露）。
"""

from __future__ import annotations

from collections.abc import Callable

from warden_agent.skill.skill import (
    FrozenSkillBinding,
    SkillCatalog,
    SkillRequirementError,
    missing_requires,
)
from warden_agent.tool.catalog import ToolSpec, function_tool

# 依赖名集合的两种给法：直接给一份快照，或给一个"取当前集合"的可调用。
# 为什么需要后者：技能的 `requires` 指的是"**运行时**能不能用到这些工具"，
# 而工具是在装配过程中陆续注册进目录的（记忆 → 技能 → Web → MCP → Git …）。
# 如果装配技能卡的那一刻就采集一份快照，后面才注册的工具就会被误判为缺失。
AvailableNames = Callable[[], set[str]] | set[str] | None


def skill_to_tool(
    binding: FrozenSkillBinding,
    *,
    available: AvailableNames = None,
) -> ToolSpec:
    """把一份技能转成一张"skill.<alias>.run"的技能卡：调用 = 激活（返回注入指令）。

    入参：目标技能别名。返回：该技能的激活正文（可直接作为系统指令注入）。

    `available`：可选的"当前可用工具/依赖名集合"（或取该集合的可调用）。给了就在
    **激活时**校验技能的 `requires`，缺依赖则抛 `SkillRequirementError`
    （不满足前置条件不允许激活）——"声明了前置依赖却从未有人校验"等于没声明。
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
        names = available() if callable(available) else available
        if names is not None:
            missing = missing_requires(binding, names)
            if missing:
                raise SkillRequirementError(
                    f"技能 {md.name!r} 缺少依赖: {', '.join(missing)}"
                )
        return f"{binding.activate()}\n目标：{goal}"

    return run


def skill_activation_tool(
    catalog: SkillCatalog,
    alias: str,
    *,
    available: AvailableNames = None,
) -> ToolSpec:
    """把目录里某技能转成一张技能卡（找不到则返回一张提示卡）。

    `available` 会透传给 `skill_to_tool`；装配路径（`agent.build_agent`）传的是
    "取当前已注册工具名"的可调用，所以 `requires` 门禁**真的会拦人**。
    """
    binding = catalog.find(alias)
    if binding is None:
        return function_tool(
            f"skill.{alias}.run",
            f"激活技能 {alias}",
            {"type": "object",
             "properties": {"goal": {"type": "string"}},
             "required": ["goal"]},
        )(lambda goal: f"技能 {alias} 未登记")
    return skill_to_tool(binding, available=available)
