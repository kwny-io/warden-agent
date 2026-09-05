"""完整演示入口：一键跑通 RAG + 多 Agent + 审批 的整条链路。

用法：
    cd /d/warden-agent
    py -m warden_agent.demo_full            # 离线演示（假模型，不花钱）
    # 或设置 key 用真实 DeepSeek：
    #   $env:DEEPSEEK_API_KEY = 'sk-xxx'
    #   py -m warden_agent.demo_full

它做什么：
  1. 造一个 RAG 知识库并塞入几段资料，注册 knowledge.search 工具。
  2. 造一个主管 Agent：能把 researcher / writer 两个子 Agent 当工具调用，
     同时也能查知识库。
  3. 跑一句演示问题，展示 Agent 是怎么一步步用工具回答的。

设计要点
  - 有 DEEPSEEK_API_KEY 就用真 DeepSeek（效果最好）；
    没有就退回假模型，照样能离线把流程跑通给你看（不花钱）。
"""
from __future__ import annotations

import os

from warden_agent.core.logging_setup import get_logger, setup_logging
from warden_agent.loop.loop import AgentLoop
from warden_agent.model.deepseek import DeepSeekModel
from warden_agent.model.fake import FakeModel
from warden_agent.model.model import AgentChatModel
from warden_agent.multiagent.supervisor import build_supervisor
from warden_agent.rag.knowledge import VectorStore, make_knowledge_tool
from warden_agent.tool.catalog import ToolCatalog

logger = get_logger("demo_full")


def _build_model() -> AgentChatModel:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        logger.info("使用真实 DeepSeek 模型")
        return DeepSeekModel(api_key=key)
    logger.info("未设置 DEEPSEEK_API_KEY，用离线假模型演示（效果示意）")
    return FakeModel()


def _build_knowledge_catalog() -> ToolCatalog:
    """造一个带了知识库工具 + 天气工具的目录。"""
    catalog = ToolCatalog()

    # RAG 知识库：塞几段"公司手册"式的资料
    store = VectorStore()
    store.add("公司的报销流程：先填报销单，再交直属经理审批，最后由财务打款。")
    store.add("公司的年假制度：正式员工每年 15 天年假，需提前一周申请。")
    store.add("公司健身房位于 3 楼，开放时间 8:00-22:00。")
    logger.info("知识库已载入 %d 段资料", len(store))
    catalog.register(make_knowledge_tool(store))

    return catalog


def _build_supervisor() -> AgentLoop:
    """造一个主管 Agent：能调动 researcher / writer 两个子 Agent。"""
    supervisor_model = _build_model()

    # 两个子 Agent（各自带一个知识库工具）
    researcher = AgentLoop(model=_build_model(), catalog=_build_knowledge_catalog(),
                           system_prompt="你是一个调研员。需要事实时用知识库工具查。")
    writer = AgentLoop(model=_build_model(), catalog=_build_knowledge_catalog(),
                       system_prompt="你是一个写手。根据资料写成稿。")

    return build_supervisor(supervisor_model, researcher, writer,
                            system_prompt=(
                                "你是一个主管。接到任务后："
                                "1) 需要事实时用 research 子代理调研；"
                                "2) 拿到调研结果后用 write 子代理成稿；"
                                "3) 汇总成最终回答。"
                            ))


def run_demo() -> None:
    setup_logging()
    supervisor = _build_supervisor()
    question = "我们公司年假和报销分别是什么规定？帮我写一段说明。"
    logger.info("=== 演示开始 ===")
    logger.info("用户问题：%s", question)
    try:
        reply = supervisor.run(question)
        logger.info("=== 最终回答 ===")
        print("\n" + ("─" * 60))
        print(reply.text)
        print("─" * 60 + "\n")
    except Exception as e:  # 假模型可能收敛不了，给友好提示
        logger.error("演示在假模型下未能收敛：%s", e)
        logger.info("建议设置 DEEPSEEK_API_KEY 后用真实模型重跑，效果会完整呈现。")


if __name__ == "__main__":
    run_demo()
