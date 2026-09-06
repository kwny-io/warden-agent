"""启动 HTTP/SSE 服务：把 Agent 对外暴露成可调用的 API。

用法：
    cd /d/warden-agent
    py -m warden_agent.web.run_server

可选环境变量（不设则用假模型，不花真实费用）：
    DEEPSEEK_API_KEY=sk-xxx         启用真实 DeepSeek
    PORT=8000                       端口（默认 8000）
    WARDEN_API_KEY=sk-xxx            给 API 设一个密钥 → 开启认证（Bearer）
                                    未设置=本地开发开放，不鉴权
    WARDEN_AUDIT=1                   开启审计（写进 SQLite 审计表，重启不丢）
    GIT_WORKDIR=path                指定 git 仓库目录 → 注册 git.apply_patch 工具
    SKILLS_DIR=path                 启用技能系统（SKILL.md 目录）
    MCP_SERVER=cmd                  启用 MCP（需 node）

启动后：
    - 打开 http://127.0.0.1:8000/docs 可看交互式 API 文档（阶段13 起需带 Bearer key）
    - POST /chat/run-1  送一句话给 Agent
    - GET  /approvals   看等待审批的请求
    - POST /approve/run-1 / /reject/run-1  处理审批
    - GET  /health/live / /health/ready     存活/就绪探针
    - GET  /audit                           查看审计轨迹
"""

from __future__ import annotations

import os
from typing import Any

import uvicorn

from warden_agent.core.config import load_env
from warden_agent.core.logging_setup import get_logger, setup_logging
from warden_agent.model.deepseek import DeepSeekModel
from warden_agent.model.fake import FakeModel
from warden_agent.model.model import AgentChatModel
from warden_agent.policy.policy import PolicyEngine, ask_when_tool_in
from warden_agent.store.sqlite import SqliteStore
from warden_agent.tool.catalog import ToolCatalog, function_tool
from warden_agent.web.audit import SqliteAuditStore
from warden_agent.web.auth import TrustedCaller
from warden_agent.web.server import build_app

logger = get_logger("run_server")


def _build_catalog() -> ToolCatalog:
    """造一张演示用的技能卡（天气 + 高危删除）。真实场景注册你自己的工具。"""
    catalog = ToolCatalog()

    @function_tool(
        "weather.get",
        "获取某城市的天气",
        {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
        pure=True,
    )
    def get_weather(city: str) -> str:
        return f"{city}: 晴, 25 度"

    catalog.register(get_weather)

    # 演示"需要审批的高危工具"：删除文件必须人工批准
    @function_tool(
        "fs.delete",
        "删除一个文件（高危，需要审批）",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        pure=False,
    )
    def delete_file(path: str) -> str:
        return f"已删除 {path}"

    catalog.register(delete_file)
    return catalog


def _build_policy() -> PolicyEngine:
    """门禁：fs.* 高危动作需要人工批准；其余默认放行。"""
    engine = PolicyEngine()
    engine.add(ask_when_tool_in(frozenset({"fs.delete"})))
    return engine


def _api_keys_from_env() -> dict[str, TrustedCaller] | None:
    """从环境变量 WARDEN_API_KEY 读取 API Key → TrustedCaller 映射。

    未设置该变量返回 None（= 本地开发开放，不鉴权）。设了则把"谁拿着这个 key"
    解析成一个 TrustedCaller（tenant/service/principal）。
    """
    key = os.environ.get("WARDEN_API_KEY")
    if not key:
        return None
    # 一个 key 对应一个"服务调用者"身份；多 key 场景可扩展成 WARDEN_API_KEYS=csv
    return {
        key: TrustedCaller(
            tenant_id="local",
            principal_type="service",
            principal_id="api-client",
            product_id="http",
        )
    }


def main() -> None:
    load_env()  # 先读 .env（可选），密钥从环境变量取，不硬编码
    setup_logging()
    store = SqliteStore("warden-agent-local.db")  # 存档文件（已被 .gitignore 忽略）

    # 模型：有 key 用真 DeepSeek，否则用假模型（离线可跑）
    model: AgentChatModel
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if api_key:
        model = DeepSeekModel(api_key=api_key)
        logger.info("使用真实 DeepSeek 模型")
    else:
        model = FakeModel()
        logger.info("未设置 DEEPSEEK_API_KEY，使用离线假模型（设置 key 可接真实 DeepSeek）")

    # 阶段13：认证 + 审计。WARDEN_API_KEY 非空才开认证；WARDEN_AUDIT=1 开审计
    api_keys = _api_keys_from_env()
    if api_keys:
        logger.info("已开启 API 鉴权（请求需带 Authorization: Bearer <key>）")
    else:
        logger.warning("未设置 WARDEN_API_KEY，未启用鉴权（本地开发开放）")

    audit_store: Any = None
    if os.environ.get("WARDEN_AUDIT") in ("1", "true", "yes"):
        audit_store = SqliteAuditStore("warden-agent-local.db")
        logger.info("已开启审计（写入 SQLite audit_log 表）")

    app = build_app(
        model=model,
        catalog=_build_catalog(),
        policy=_build_policy(),
        store=store,
        # 默认启用记忆与 Web 搜索（离线可跑）；技能/MCP/Git 按环境变量开启
        memory=True,
        web=True,
        skills=os.environ.get("SKILLS_DIR") or None,
        mcp_server=os.environ.get("MCP_SERVER") or None,
        git_workdir=os.environ.get("GIT_WORKDIR") or None,
        api_keys=api_keys,
        audit_store=audit_store,
        model_id=("deepseek" if api_key else "fake"),
        model_api_key=api_key,
    )
    port = int(os.environ.get("PORT", "8000"))
    logger.info("可视化控制台: http://127.0.0.1:%s/  (演示网页)", port)
    logger.info("OpenAPI 文档:  http://127.0.0.1:%s/docs", port)
    logger.info("健康检查:      /health/live  /health/ready")
    uvicorn.run(app, host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
