"""多 Agent —— 主管模式（supervisor pattern）。

我们把每一个子 Agent（如 researcher 研究员、writer 作者）包装成"主管 Agent"的一个工具。
主管负责分工：接到任务，决定先让谁干，把子 Agent 的结果汇总成最终答案。

结构：
    Supervisor（主管 Agent）
       ├── tool: research(话题)  -> 调用 Researcher Agent，返回调研结果
       └── tool: write(主题)     -> 调用 Writer Agent，返回成稿

关键点：
    - 每个子 Agent 是一个独立 AgentLoop（各自有模型 + 工具箱 + 策略）。
    - 主管也复用我们已有的 AgentLoop，把子 Agent 作为技能卡注册进它的目录。
    - 所以"多 Agent"不是新造轮子，是已有"工具管线的递归"——Agent 能把 Agent 当工具用。
    - 升级（本轮）：① **共享工作记忆**——多个子 Agent 可写读同一个 WORKSPACE 作用域的记忆；
      ② **容错降级**——某个子 Agent 失败时，主管可重试 / 换备用专员 / 降级处理，不再整体崩溃；
      ③ **并行/串行调度**——独立分派器（线程池真并行）或按依赖串行，见 dispatch.py。
"""
from __future__ import annotations

import time
from contextlib import suppress
from typing import TYPE_CHECKING

from warden_agent.loop.loop import AgentLoop
from warden_agent.memory import MemoryScope
from warden_agent.model.model import AgentChatModel
from warden_agent.policy.policy import PolicyEngine
from warden_agent.tool.catalog import ToolCatalog, ToolSpec, function_tool

if TYPE_CHECKING:
    from warden_agent.memory import MemoryService


def make_handoff(name: str, topic: str, conclusion: str,
                 *deps: tuple[str, str]) -> str:
    """构造一份**结构化交接单**：子 Agent 的产出以干净、可被下一个 Agent 消化的方式交接。

    多 Agent 协作里，"专员之间怎么传递信息"是关键。把结论甩一段自然语言，下一个 Agent
    得自己去猜；而用**结构化交接单**（字段清晰：角色/任务/结论/数据来源），下一个 Agent
    （或主管）能直接取用，不糊弄、可复核。

    参数：
      - name        交接方角色（如 "Researcher"）
      - topic       它被派去处理的任务/话题
      - conclusion  它得出的结论/产出
      - deps        零到多组 (字段名, 值) 的附加结构化数据（如 ("来源", "员工手册.pdf")）
    """
    lines = [
        f"[交接单] 角色: {name}",
        f"任务: {topic}",
        f"结论: {conclusion}",
    ]
    for field, value in deps:
        lines.append(f"{field}: {value}")
    return "\n".join(lines)


def wrap_agent_as_tool(
    agent: AgentLoop,
    name: str,
    description: str,
    arg_name: str = "topic",
    *,
    structured: bool = True,
    retries: int = 1,
    fallback: AgentLoop | None = None,
    degrade: bool = True,
) -> ToolSpec:
    """把一个子 Agent 包装成主管能调用的工具（技能卡）。

    - agent        ：子 Agent（一个独立的 AgentLoop）
    - name         ：工具名，如 "research"
    - description  ：告诉主管这个子 Agent 是干嘛的（主管靠它决定何时用）
    - arg_name     ：子 Agent 接收的输入参数名，如 "topic"
    - structured   ：True(默认)=返回**结构化交接单**（含角色/任务/结论），
                     下一个 Agent 能干净地取用，是企业级多 Agent 交接的做法；
                     False=退化为只返回一句结论文本（兼容旧行为）。
    - retries      ：子 Agent 失败时最多重试几次（默认 1）。防"一次失败就崩"。
    - fallback     ：重试仍失败时，换这个备用子 Agent 顶上（如"研究员挂了用搜索兜底"）。
    - degrade      ：重试 + 换人仍失败时，是否降级为一条"降级交接单"（默认 True），
                     这样不会把异常抛回主管层导致整体崩溃；False 则向主管层抛错。
    """
    schema = {
        "type": "object",
        "properties": {arg_name: {"type": "string"}},
        "required": [arg_name],
    }

    @function_tool(name, description, schema)
    def _call(topic: str) -> str:
        # 【容错降级】子 Agent 失败时：重试 → 换备用 → 降级，不整体崩。
        last_error: str | None = None
        for attempt in range(retries + 1):
            try:
                reply = agent.run(topic)
                return (make_handoff(name, topic, reply.text)
                        if structured else reply.text)
            except Exception as e:  # noqa: BLE001 - 子 Agent 异常统一降级处理
                last_error = f"{type(e).__name__}: {e}"
                if attempt < retries:
                    time.sleep(0.01)  # 极小退避，避免紧密重试
        # 重试用尽，尝试备用专员
        if fallback is not None:
            try:
                reply = fallback.run(topic)
                return (make_handoff(f"{name}(备用)", topic, reply.text)
                        if structured else reply.text)
            except Exception as e:  # noqa: BLE001
                last_error = f"{last_error} | 备用也失败: {type(e).__name__}: {e}"
        if not degrade:
            raise RuntimeError(f"子 Agent {name} 经重试/换人后仍失败: {last_error}")
        # 降级：给主管一条"降级交接单"，让它自行接管，而不是崩溃。
        return make_handoff(
            name, topic,
            f"[降级] 子任务执行失败（{last_error}），请主管直接处理或改派。",
        )

    return _call


def share_memory(
    service: MemoryService,
    *agents: AgentLoop,
    scope: MemoryScope = MemoryScope.WORKSPACE,
    supervisor: AgentLoop | None = None,
) -> None:
    """把一份（WORKSPACE）工作记忆注入多个 Agent，让专员之间"共享上下文"。

    - 给每个 `AgentLoop` 设置同一个 `memory` 服务 + 同一 `memory_scope`，
      这样每个专员在 `_recall_context` 里能按需检索到别的专员已写入的记忆（按需取用）。
    - 同时把 `memory.remember` / `memory.recall` 两张工具卡挂进每个专员的目录，
      让专员能显式地"记一笔 / 取一笔"共享工作记忆。
    - 可选 `supervisor`：把主管也纳入同一工作记忆（主管统筹时能读到各专员产出）。

    注意：WORKSPACE 作用域是"一次协作工作区内共享"——同一次协同任务里多个专员共用一个
    记忆桶；任务结束被视为该工作区收尾（与 RUN 类似的生命周期，但由多 Agent 共享）。
    """
    from warden_agent.memory.tools import make_memory_tools

    targets = list(agents) + ([supervisor] if supervisor is not None else [])
    for a in targets:
        a.memory = service
        a._memory_scope = scope  # 直接借用 loop 内部的作用域字段，保持共享一致
        tools = make_memory_tools(service, scope)
        for t in tools:
            with suppress(Exception):  # 已注册同名工具则跳过
                a.catalog.register(t)


def build_supervisor(
    supervisor_model: AgentChatModel,
    researcher: AgentLoop,
    writer: AgentLoop,
    system_prompt: str = "你是一个主管。接到任务后，先用 research 做调研，再用 write 成稿。",
    policy_engine: PolicyEngine | None = None,
    memory_service: MemoryService | None = None,
    memory_scope: MemoryScope = MemoryScope.WORKSPACE,
) -> AgentLoop:
    """组装一个主管 Agent：它同时能调用 researcher 和 writer 两个子 Agent。

    用法：
        researcher = AgentLoop(model=..., catalog=...)
        writer     = AgentLoop(model=..., catalog=...)
        supervisor = build_supervisor(model, researcher, writer)
        supervisor.run("帮我写一篇关于上海的一页介绍")

    传 `memory_service` 时，主管 + 两个子 Agent 会共享同一份 WORKSPACE 工作记忆：
    研究员查到的资料写进工作记忆，写手能在需要时取用——共享上下文、避免重复查。
    """
    catalog = ToolCatalog()
    catalog.register(wrap_agent_as_tool(researcher, "research",
        "调研一个话题，返回相关资料和要点", "topic"))
    catalog.register(wrap_agent_as_tool(writer, "write",
        "根据主题写一篇成稿", "topic"))

    if memory_service is not None:
        share_memory(memory_service, researcher, writer, scope=memory_scope)

    return AgentLoop(
        model=supervisor_model,
        catalog=catalog,
        system_prompt=system_prompt,
        policy_engine=policy_engine,
    )
