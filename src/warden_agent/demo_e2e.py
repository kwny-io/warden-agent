"""端到端完整演示 —— 一次跑通「L2 聪明 loop × L3 能力层」的整条链路。

这是 Phase 3 的收口演示：把之前零散的能力串成一个能讲的"完整故事"。

它做什么（按四层架构说）：
  - L2 核心循环 loop（聪明度）：
      【深度③】阶段规划 —— 复杂任务先拆阶段（planner.build_plan / plan_as_context）
      【深度⑤】工具意图判断 —— 调工具前先判"该不该调、调哪个"（ToolIntentRouter）
  - L3 能力层：
      RAG 引用        —— 知识检索带来源引用，答案可溯源（knowledge.search + SourceHit）
      多 Agent 交接   —— 主管派 Researcher→Writer，专员间用结构化交接单（make_handoff）
      技能触发        —— 按任务意图选出该用的 SKILL.md（SkillTriggerRouter）
  - L1 地基 / L4 界面：复用状态机/审批/审计 与 CLI/SSE，本演示聚焦能力串联。

两种运行方式：
  - 有 DEEPSEEK_API_KEY → 真模型端到端跑一遍（效果最完整）。
  - 无 key → 走**离线引导演示**：把每一层的新代码一步步跑给你看（确定性、可复现、绝不崩）。

用法：
    cd /d/warden-agent
    py -m warden_agent.demo_e2e
"""
from __future__ import annotations

import os
from typing import Any

from warden_agent.core.logging_setup import get_logger, setup_logging
from warden_agent.loop.intent import ToolIntentRouter
from warden_agent.loop.loop import AgentLoop
from warden_agent.loop.planner import ModelPlanner, build_plan, plan_as_context
from warden_agent.model.deepseek import DeepSeekModel
from warden_agent.model.fake import FakeModel
from warden_agent.model.model import AgentChatModel, ChatRequest, ChatResponse, ToolCall
from warden_agent.multiagent.supervisor import build_supervisor, make_handoff
from warden_agent.rag.knowledge import VectorStore, make_knowledge_tool
from warden_agent.skill import SkillCatalog, SkillPackageParser
from warden_agent.skill.trigger import SkillTriggerRouter
from warden_agent.tool.catalog import ToolCatalog, ToolSpec

logger = get_logger("demo_e2e")

_HR_SKILL = """---
name: HR一页纸
description: 把 HR 制度整理成一页纸说明
trust: trusted
---

# HR 一页纸

当需要把 HR 制度（年假/报销/考勤等）整理成简洁说明时：
1. 先从知识库检索相关制度原文；
2. 提炼关键条款（天数/金额/流程）；
3. 组织成一页纸，带来源引用。
"""


def _build_model() -> AgentChatModel:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        logger.info("使用真实 DeepSeek 模型")
        return DeepSeekModel(api_key=key)
    logger.info("未设置 DEEPSEEK_API_KEY，用离线自主闭环演示")
    return FakeModel()


class _Scripted(AgentChatModel):
    """演示用脚本化模型：按预定"剧本"逐步返回，驱动 Agent 走一条真实闭环。

    它和测试里的 ScriptedModel 同思路：不用真模型，也能让 AgentLoop 完整跑一条
    plan→act→observe 的链路。剧本里每一步返回"调哪个工具 / 给什么结论"，
    Agent 就真实地一步步推进——真正离线自主完成一个任务，而不是分节摆拍。
    """

    def __init__(self, steps: list[ChatResponse]) -> None:
        self._steps = list(steps)
        self._i = 0

    def chat(self, request: ChatRequest) -> ChatResponse:
        if self._i >= len(self._steps):
            return ChatResponse(content="（演示脚本已走完）", finish_reason="stop")
        resp = self._steps[self._i]
        self._i += 1
        return resp

    @classmethod
    def with_tool(cls, name: str, args: dict[str, object], then: str) -> _Scripted:
        """两步剧本：先调工具，再给结论。"""
        return cls([
            ChatResponse(content=None,
                         tool_calls=[ToolCall(id="c1", name=name, arguments=args)],
                         finish_reason="tool_calls"),
            ChatResponse(content=then, finish_reason="stop"),
        ])


def _tc(cid: str, name: str, args: dict[str, object]) -> ToolCall:
    return ToolCall(id=cid, name=name, arguments=args)


def _build_offline_supervisor() -> AgentLoop:
    """离线自主版主管：三个 Agent 都用脚本化模型，跑一条真实闭环。

    链路（真实发生，非摆拍）：
      主管调 research("整理年假与报销") → 调研员调 knowledge.search(带来源引用)
        → 拿到带引用的资料 → 产出结构化交接单 → 主管调 write → 写手成稿 → 主管汇总。
    """
    # 调研员：先查知识库（带来源引用），再给出带来源的结论
    researcher = AgentLoop(
        model=_Scripted.with_tool(
            "knowledge.search", {"query": "公司年假和报销制度"},
            "年假：正式员工每年15天，需提前一周申请（来源：员工手册.pdf）。"
            "报销：先填单→经理审批→财务打款，需附发票（来源：员工手册.pdf）。",
        ),
        catalog=_build_agent_catalog(),
        system_prompt="你是调研员，需要制度原文时用 knowledge.search 查并引用来源。",
    )
    # 写手：根据交接单整理成稿（一步结论）
    writer = AgentLoop(
        model=_Scripted([
            ChatResponse(content=(
                "【一页纸·年假与报销】\n"
                "- 年假：正式员工 15 天/年，提前一周申请。\n"
                "- 报销：填单 → 经理审批 → 财务打款，需附发票。\n"
                "（依据：员工手册.pdf）"
            ), finish_reason="stop"),
        ]),
        catalog=_build_agent_catalog(),
        system_prompt="你是写手，根据调研员交接的资料整理成简洁说明。",
    )
    # 主管：派 research → 派 write → 收尾
    supervisor_model = _Scripted([
        ChatResponse(content=None, tool_calls=[_tc("c1", "research",
                      {"topic": "整理公司年假与报销制度为一页纸"})], finish_reason="tool_calls"),
        ChatResponse(content=None, tool_calls=[_tc("c2", "write",
                      {"topic": "年假与报销一页纸"})], finish_reason="tool_calls"),
        ChatResponse(content="已按调研与成稿流程，交付公司年假与报销的一页纸说明。",
                     finish_reason="stop"),
    ])
    return build_supervisor(
        supervisor_model, researcher, writer,
        system_prompt=(
            "你是一个主管。接到 HR 制度整理任务后："
            "1) 用 research 派调研员查制度（它会查知识库并带来源）；"
            "2) 用 write 派写手成稿；"
            "3) 汇总成带来源的一页纸说明。"
        ),
    )



def _build_knowledge() -> tuple[VectorStore, ToolSpec]:
    """建一个带来源引用的 RAG 知识库 + 检索工具。"""
    store = VectorStore()
    store.add(
        "公司的报销流程：先填报销单，再交直属经理审批，最后由财务打款。报销需附发票。",
        source="员工手册.pdf",
    )
    store.add(
        "公司的年假制度：正式员工每年 15 天年假，需提前一周在系统申请。",
        source="员工手册.pdf",
    )
    store.add(
        "公司的考勤制度：工作日 9:00-18:00，弹性上下班，每日至少 8 小时。",
        source="考勤制度.md",
    )
    return store, make_knowledge_tool(store)


def _build_agent_catalog() -> ToolCatalog:
    """主管子 Agent 共用的工具箱：知识检索 + 天气 + 技能触发。"""
    _store, knowledge_tool = _build_knowledge()
    catalog = ToolCatalog()
    catalog.register(knowledge_tool)

    # 技能触发工具
    skill_cat = SkillCatalog()
    skill_cat.load_skill("hr-onepager", SkillPackageParser().parse(_HR_SKILL),
                         source="inline")
    catalog.register(SkillTriggerRouter(skill_cat).tool())
    return catalog


def _build_supervisor() -> AgentLoop:
    """主管 Agent：能调 Researcher / Writer 两个专员（结构化交接）+ 知识库 + 技能触发。

    深度③/⑤ 用上"模型驱动"升级：有真模型时主管会用 ModelPlanner 让模型生成阶段、
    用带 reasoner 的意图路由器让模型解释一次"疑似误调"的工具调用。
    """
    sup_model = _build_model()

    def _planner(m: AgentChatModel) -> Any:
        return ModelPlanner(m) if isinstance(m, DeepSeekModel) else None

    def _intent(m: AgentChatModel) -> ToolIntentRouter:
        router = ToolIntentRouter(
            triggers={"knowledge.search": ["年假", "报销", "制度", "考勤"]},
            domains={"web": ["天气", "新闻", "股价"]},
        )
        # 有真模型时才让模型说明理由；否则退回启发式
        if isinstance(m, DeepSeekModel):
            from warden_agent.model.model import ChatRequest, Message

            def reasoner(tool_name: str, user_text: str,
                         context_text: str, tool_schema: Any) -> bool:
                _ = (tool_name, context_text, tool_schema)  # 仅用 user_text
                req = ChatRequest(messages=[
                    Message(role="user", content=(
                        f"当前问题：{user_text}\n"
                        f"这个调用是否合理？请只回答 合理 或 不合理。")),
                ])
                try:
                    txt = (m.chat(req).content or "").strip()
                    return "合理" in txt
                except Exception:  # noqa: BLE001
                    return False
            router.reasoner = reasoner
        return router

    researcher = AgentLoop(
        model=_build_model(),
        catalog=_build_agent_catalog(),
        system_prompt=(
            "你是调研员。需要制度原文时用 knowledge.search 查，并引用来源；"
            "需要判断该用什么技能时用 skill.trigger.pick。"
        ),
        planner=_planner(_build_model()),
        intent=_intent(_build_model()),
    )
    writer = AgentLoop(
        model=_build_model(),
        catalog=_build_agent_catalog(),
        system_prompt="你是写手，根据调研员交接的资料整理成简洁说明。",
        planner=_planner(_build_model()),
        intent=_intent(_build_model()),
    )
    return build_supervisor(
        sup_model, researcher, writer,
        system_prompt=(
            "你是一个主管。接到 HR 制度整理任务后："
            "1) 用 research 派调研员查制度原文；"
            "2) 用 write 派写手成稿；"
            "3) 汇总成带来源的一页纸说明。"
        ),
    )


def _section(title: str) -> None:
    print("\n" + ("═" * 68))
    print(f"  {title}")
    print("═" * 68)


def _line(text: str) -> None:
    print(text)


# ---- 离线引导演示：逐步跑通每一层新能力（确定性、可复现）----


def _guided_demo() -> None:
    _section("L2 · loop 深度③ 阶段规划")
    task = "调研公司年假与报销制度，写成对比说明"
    plan = build_plan(task)
    _line(f"任务: {task}")
    _line(f"判定复杂: {plan.is_complex}")
    _line(f"规划简报: {plan.summary}")
    _line(plan_as_context(plan, current=0))
    _line(plan_as_context(plan, current=1))

    _section("L2 · loop 深度⑤ 工具意图判断")
    router = ToolIntentRouter(
        triggers={"knowledge.search": ["年假", "报销", "制度"]},
        domains={"web": ["天气", "新闻", "股价"]},
    )
    _line("问题「年假怎么休」→ 想调 knowledge.search")
    v1 = router.relay("knowledge.search", {"description": "查知识库"},
                      "公司年假怎么休")
    _line(f"  verdict={v1.action} ({v1.reason})")
    _line("问题「今天晚饭吃什么」→ 想调 web.search（与 web 域无关）")
    v2 = router.relay("web.search", {"description": "搜网页"},
                      "今天晚饭吃什么好")
    mode = '提示模型再确认，避免误调' if v2.action == "hint" else '命中触发词'
    _line(f"  verdict={v2.action} → {mode}")
    _line("    提示信息：" + (v2.message if v2.action == "hint" else v2.reason))

    _section("L3 · RAG 引用（可溯源）")
    store, kt = _build_knowledge()
    cat = ToolCatalog()
    cat.register(kt)
    for q in ("公司年假制度是怎样的", "报销怎么走流程"):
        out = cat.execute("knowledge.search", {"query": q})
        _line(f"Q: {q}")
        _line("  ⇣ " + str(out).replace("\n", "\n  "))

    _section("L3 · 多 Agent 结构化交接")
    note = make_handoff(
        "Researcher", "年假+报销",
        "年假：正式员工 15 天/年，提前一周申请。报销：填单→经理→财务。",
        ("来源", "员工手册.pdf"),
    )
    _line("调研员交给写手的交接单：")
    _line("  " + note.replace("\n", "\n  "))

    _section("L3 · 技能触发判断")
    skill_cat = SkillCatalog()
    skill_cat.load_skill("hr-onepager", SkillPackageParser().parse(_HR_SKILL),
                         source="inline")
    trigger = SkillTriggerRouter(skill_cat)
    _line("任务「把年假和报销整理成一页纸」→ 技能触发建议：")
    _line("  " + trigger.describe_candidates("把年假和报销制度整理成一页纸").replace("\n", "\n  "))

    _section("✅ 收尾")
    _line("离线引导演示完成：L2 深度③⑤ + L3 引用/交接/技能触发 已逐一验证。")
    _line("设置 DEEPSEEK_API_KEY 后用真模型跑，可看到这些能力被 loop 端到端串起来。")


def _autonomous_demo() -> None:
    """离线自主闭环演示：一个 Agent（主管+专员）真跑完一条任务，非分节摆拍。

    用脚本化模型驱动真实的 AgentLoop，主管真的派 research → 调研员真的查知识库
    （拿回带来源引用的资料）→ 真的产出结构化交接单 → 主管派 write → 写手成稿 →
    主管收尾。整条 plan→act→observe 链路真实发生，打印其轨迹。
    """
    _section("端到端 · 离线自主闭环（真实跑一条任务）")
    supervisor = _build_offline_supervisor()
    question = "帮我把公司年假和报销制度整理成一页纸说明"
    _line(f"问题: {question}")
    _line("\n—— 主管收到任务，开始循环 ——")
    reply = supervisor.run(question)

    _line("\n—— 过程中真实发生的轨迹 ——")
    _labels = {"system": "系统", "user": "用户", "assistant": "模型", "tool": "工具"}
    for m in reply.messages:
        role = _labels.get(m.role, m.role)
        content = m.content or ""
        line = f"[{role}] {content}"
        shown = line if len(line) <= 90 else line[:90] + "…"
        _line("  " + shown)
        if m.tool_call:
            _line(f"      └─ 调用工具 {m.tool_call.name}{m.tool_call.arguments}")

    _line("\n—— 最终回答 ——")
    _line(reply.text)
    _line("\n✅ 离线自主闭环完成：主管派单 → 调研员查库(带引用) → 交接 → 写手成稿 → 汇总。")


def _live_demo() -> None:
    _section("端到端 · 真实模型驱动")
    supervisor = _build_supervisor()
    question = "帮我把公司年假和报销制度整理成一页纸说明"
    _line(f"问题: {question}")
    reply = supervisor.run(question)
    _line("\n最终回答:")
    _line(reply.text)


def run_demo() -> None:
    setup_logging()
    _section("Warden Agent · 端到端完整演示")
    if os.environ.get("DEEPSEEK_API_KEY"):
        _live_demo()
    else:
        # 离线：先"自主闭环"真跑一条任务，再分节拆解每一层能力（加深理解）。
        _autonomous_demo()
        _guided_demo()


if __name__ == "__main__":
    run_demo()
