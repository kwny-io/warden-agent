"""把 multiagent 的「主管 + 子 Agent」真正接进产品路径。

**为什么需要这一层**：`supervisor.py` / `dispatch.py` 早就实现了"主管派工 + 结构化交接 +
共享工作记忆 + 容错降级"，但此前**只有演示与评测在用**（`demo_e2e.py`、`evals/runner.py`），
HTTP / CLI 产品路径一次都没注册过——而 README 的能力表已经把它算成「已实现」。
结论要么接线、要么降级声明；这里选择接线，并把它做成**默认关、显式打开**的真开关
（`WARDEN_MULTIAGENT=1`）：子 Agent 会真的多花模型调用，不该在没人要求时自己跑起来。

**子 Agent 只拿只读工具**（`pure=True`），且排除子 Agent 卡本身，两条理由：
  1. 有副作用的工具要过审批闸门，而审批是"人对着主 Run 的对话"给的；子 Agent 是在
     一次工具调用内部跑的，没有这条对话通道——把审批类工具交给子 Agent，等于给它
     一条绕开人工确认的路。这不是"漏接线"，是"接线反而开洞"。
  2. 排除子 Agent 卡本身，避免"主管 → 子 Agent → 主管"的无限递归。
"""
from __future__ import annotations

import logging
from typing import Any

from warden_agent.loop.loop import AgentLoop
from warden_agent.memory import MemoryScope
from warden_agent.multiagent.supervisor import share_memory, wrap_agent_as_tool
from warden_agent.tool.catalog import ToolCatalog, ToolSpec

logger = logging.getLogger(__name__)

# (卡名, 给主管看的说明, 子 Agent 自己的系统提示)
DEFAULT_ROLES: tuple[tuple[str, str, str], ...] = (
    (
        "research",
        "派研究员子 Agent 去调研一个话题，返回结构化交接单（角色/任务/结论）。"
        "需要外部资料或多步查证时用它，不要自己硬猜。",
        "你是研究员。围绕被派的任务做调研，把结论写成要点，"
        "能查到来源就注明来源；查不到就明说「没查到」，不要编。",
    ),
    (
        "write",
        "派写手子 Agent 把主题写成一篇成稿，返回结构化交接单（角色/任务/结论）。",
        "你是写手。按被派的主题写一篇成稿，条理清楚、不堆废话。",
    ),
)


def readonly_tools(
    catalog: ToolCatalog, *, exclude: frozenset[str] = frozenset()
) -> ToolCatalog:
    """从主目录里挑出**只读工具**，装成一个给子 Agent 用的小目录。

    判据是 `ToolSpec.pure`（工具自己声明的"无副作用、可重试"），**不是**按名字白名单：
    这样以后新增的只读工具会自动被子 Agent 用上，新增的有副作用工具也不会悄悄漏进去。
    """
    sub = ToolCatalog()
    for spec in catalog.all():
        if spec.name in exclude or not spec.pure:
            continue
        sub.register(spec)
    return sub


def make_multiagent_tools(
    model: Any,
    catalog: ToolCatalog,
    *,
    policy_engine: Any = None,
    memory_service: Any = None,
    memory_scope: Any = MemoryScope.WORKSPACE,
    roles: tuple[tuple[str, str, str], ...] = DEFAULT_ROLES,
    retries: int = 1,
    degrade: bool = True,
) -> list[ToolSpec]:
    """造一组「子 Agent 技能卡」，注册进主管（= 当前 Agent）的目录即可。

    - `model`         ：子 Agent 用的模型。产品路径复用主模型（同一厂商/密钥/超时），
                        不另建连接池——否则"打开多 Agent"会变成"多开一份配置"。
    - `catalog`       ：主目录，用来给子 Agent 复制只读工具（见模块 docstring）。
    - `memory_service`：给了就按 `share_memory` 让专员共享同一份工作记忆
                        （研究员查到的资料，写手在需要时能取用）。
    - `retries/degrade`：透给 `wrap_agent_as_tool`——子 Agent 失败时重试，
                        仍失败则降级成一条"降级交接单"交给主管自行接管，而不是把
                        异常抛回主管层导致整个 Run 崩掉。

    返回的卡片带 `pure=False`：子 Agent 会真的发起模型调用（有成本、有外部行为），
    按本项目"未标 pure = 要审批"的口径，它应当落在审批闸门之内。
    """
    sub_catalog = readonly_tools(catalog, exclude=frozenset(name for name, _, _ in roles))
    if not sub_catalog.all():
        # 目录里一个只读工具都没有（如纯离线假模型的最小装配）：子 Agent 只能靠模型自身
        # 知识作答。不静默硬造工具，但要说清楚——排查"子 Agent 为什么什么也查不到"时，
        # 这一行日志就是答案。
        logger.info("多 Agent：主目录里没有只读工具，子 Agent 将只依靠模型自身知识作答。")

    agents: list[AgentLoop] = []
    specs: list[ToolSpec] = []
    for name, description, system_prompt in roles:
        agent = AgentLoop(
            model=model,
            catalog=sub_catalog,      # 共用同一份小目录：专员之间工具面一致，行为可复现
            system_prompt=system_prompt,
            policy_engine=policy_engine,
        )
        agents.append(agent)
        specs.append(
            wrap_agent_as_tool(
                agent, name, description, "topic", retries=retries, degrade=degrade,
            )
        )

    if memory_service is not None:
        share_memory(memory_service, *agents, scope=memory_scope)

    return specs
