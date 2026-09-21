"""启动 HTTP/SSE 服务：把 Agent 对外暴露成可调用的 API。

用法：
    cd /d/warden-agent
    py -m warden_agent.web.run_server

可选环境变量（不设则用假模型，不花真实费用）：
    DEEPSEEK_API_KEY=sk-xxx         启用真实 DeepSeek
    PORT=8000                       端口（默认 8000）
    WARDEN_HOST=127.0.0.1           监听地址（默认只本机；容器里要设 0.0.0.0）
    WARDEN_API_KEY=sk-xxx           **访问密钥（对外部署必须设）** → 开启 Bearer 鉴权
    WARDEN_API_KEYS=alice:k1,bob:k2 **多用户隔离**：逗号分隔 `用户id:密钥`，各自独立身份
    WARDEN_API_USER=demo-user       单 key 模式下的身份（默认 demo-user）
    WARDEN_TENANT=local             租户 id（审计与授权按它隔离）
    WARDEN_ALLOW_ANON=1             **仅本机开发**：显式声明接受"无鉴权"
    WARDEN_AUDIT=1                   开启审计（写进 SQLite 审计表，重启不丢）
    WARDEN_RATE_LIMIT=600/60         限流：每 60 秒最多 600 次（默认；设 0 关闭）
    WARDEN_STABILITY=0              关闭工具稳定性层（**默认开启**：超时 + 退避重试 + 熔断）
    GIT_WORKDIR=path                指定 git 仓库目录 → 注册 git.apply_patch 工具
    SKILLS_DIR=path                 启用技能系统（SKILL.md 目录）
    WARDEN_KNOWLEDGE=1|path         RAG 知识库：1=内置离线语料，或给一个文档目录（.md/.txt）
    WARDEN_WEB_FETCH=1              开启**真实联网抓取**（web.fetch 真发 HTTP；默认离线 mock）
    MCP_SERVER=cmd                  启用 MCP（需 node）

鉴权是 **fail-closed** 的：既没有 `WARDEN_API_KEY`、也没有显式 `WARDEN_ALLOW_ANON=1`
时**拒绝启动**；监听非本机地址（如 0.0.0.0）时若无鉴权，同样拒绝启动。
理由：`/approve` 和 `/reject` 是"人工审批"这道闸门的入口——如果接口本身无鉴权，
调用方就能批准自己的高危操作，门禁形同虚设。

启动后：
    - 打开 http://127.0.0.1:8000/docs 可看交互式 API 文档（设了 WARDEN_API_KEY 后需带 Bearer key）
    - POST /chat/run-1  送一句话给 Agent
    - GET  /approvals   看等待审批的请求
    - POST /approve/run-1 / /reject/run-1  处理审批
    - GET  /health/live / /health/ready     存活/就绪探针
    - GET  /audit                           查看审计轨迹
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import uvicorn

from warden_agent.core.config import load_env
from warden_agent.core.logging_setup import get_logger, setup_logging
from warden_agent.loop.intent import ToolIntentRouter
from warden_agent.loop.planner import ModelPlanner
from warden_agent.model.deepseek import DeepSeekModel
from warden_agent.model.fake import FakeModel
from warden_agent.model.model import AgentChatModel
from warden_agent.policy.policy import PolicyEngine, ask_when_tool_in
from warden_agent.store.sqlite import SqliteStore
from warden_agent.tool.catalog import ToolCatalog, function_tool
from warden_agent.web.audit import SqliteAuditStore
from warden_agent.web.auth import TrustedCaller
from warden_agent.web.coordination import coordination_for
from warden_agent.web.ratelimit import limiter_from_env
from warden_agent.web.search import providers_from_env
from warden_agent.web.server import build_app

logger = get_logger("run_server")

# 上下文裁剪阈值（用字符数近似 token）：约 60k 字符 —— 够长会话用，又不至于把上下文撑爆
DEFAULT_MAX_CONTEXT_CHARS = 60000


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


class AuthConfigError(RuntimeError):
    """鉴权配置缺失。fail-closed：宁可起不来，也不要无鉴权地对外服务。"""


def is_loopback_host(host: str) -> bool:
    """是否只监听本机。"""
    return host in ("127.0.0.1", "localhost", "::1")


def _caller_for(user_id: str, tenant: str) -> TrustedCaller:
    """把一个用户 id 包成调用者身份。principal_id 即会话归属用户（见 TrustedCaller.user_id）。"""
    return TrustedCaller(
        tenant_id=tenant,
        principal_type="user",
        principal_id=user_id,
        product_id="http",
    )


def resolve_auth(env: Mapping[str, str]) -> tuple[dict[str, TrustedCaller] | None, str]:
    """决定鉴权模式，返回 (api_keys, mode)。

    mode：
      "bearer"   —— 配了 `WARDEN_API_KEYS`（多用户）或 `WARDEN_API_KEY`（单用户）
      "anon-dev" —— 未配 key，但**显式**设了 `WARDEN_ALLOW_ANON=1`，声明接受无鉴权

    既没有 key、也没有显式声明 → 抛 `AuthConfigError`（拒绝启动）。
    这是刻意的 fail-closed：无鉴权时任何人都能调 `/audit`（读全部审计）、
    以及 `/approve`（批准高危操作）——审批门禁会被自己人绕过。

    **多用户（多租户）用 `WARDEN_API_KEYS`**：逗号分隔的 `用户id:密钥`，例如
    `alice:sk-aaa,bob:sk-bbb`。每个 key 绑定一个用户身份——**身份由 key 决定，
    客户端无法自称**。这是会话归属与越权拦截能成立的前提：单 key 模式下所有调用者
    都是同一个身份，谈不上隔离。
    单 key 模式（`WARDEN_API_KEY`）的身份取 `WARDEN_API_USER`（默认 `demo-user`，
    与控制台默认账号一致）。租户取 `WARDEN_TENANT`（默认 `local`）。
    """
    tenant = (env.get("WARDEN_TENANT") or "local").strip() or "local"

    raw_keys = (env.get("WARDEN_API_KEYS") or "").strip()
    if raw_keys:
        keys: dict[str, TrustedCaller] = {}
        for pair in raw_keys.split(","):
            pair = pair.strip()
            if not pair:
                continue
            user_id, sep, key = pair.partition(":")
            if not sep or not user_id.strip() or not key.strip():
                raise AuthConfigError(
                    f"WARDEN_API_KEYS 格式错误：{pair!r}。应为逗号分隔的 `用户id:密钥`，"
                    "例如 alice:sk-aaa,bob:sk-bbb"
                )
            keys[key.strip()] = _caller_for(user_id.strip(), tenant)
        if keys:
            return keys, "bearer"

    single_key = env.get("WARDEN_API_KEY")
    if single_key:
        user_id = (env.get("WARDEN_API_USER") or "demo-user").strip() or "demo-user"
        return {single_key: _caller_for(user_id, tenant)}, "bearer"

    if env.get("WARDEN_ALLOW_ANON", "").strip().lower() in ("1", "true", "yes"):
        return None, "anon-dev"

    raise AuthConfigError(
        "未设置 WARDEN_API_KEY —— 拒绝启动（fail-closed）。\n"
        "  · 对外 / 容器部署：必须设置 WARDEN_API_KEY=<强随机串>\n"
        "  · 多用户隔离：设置 WARDEN_API_KEYS=alice:key1,bob:key2\n"
        "  · 仅本机开发：显式设 WARDEN_ALLOW_ANON=1 表示接受无鉴权"
    )


def ensure_listen_is_safe(host: str, auth_mode: str) -> None:
    """校验"监听地址 × 鉴权模式"这个组合是否安全。不安全则抛 AuthConfigError。

    单独抽出来是为了可测：main() 里只负责把异常变成退出码。
    """
    if auth_mode != "bearer" and not is_loopback_host(host):
        raise AuthConfigError(
            f"WARDEN_HOST={host} 是对外监听地址，但未开启鉴权 —— 拒绝启动。"
            "请设置 WARDEN_API_KEY，或把 WARDEN_HOST 改回 127.0.0.1。"
        )


def _knowledge_from_env(env: Mapping[str, str]) -> Any:
    """RAG 知识来源开关。返回 `True`（内置离线语料）/ 目录路径 / `None`（不用）。

    `WARDEN_KNOWLEDGE=1` → 索引内置离线语料；给一个目录路径 → 索引该目录下的
    `.md` / `.txt`；不设或设 `0` → 不注册 `knowledge.search`。

    **默认不开**：RAG 要额外索引成本（真语义嵌入还要网络与费用），不该替用户悄悄决定。
    开了会在启动日志里写明用的是**哪种嵌入器**——词频嵌入与语义嵌入质量差别很大，
    不写清楚就有"过度声称语义检索"的风险。
    """
    raw = (env.get("WARDEN_KNOWLEDGE") or "").strip()
    if not raw or raw.lower() in ("0", "false", "no", "off"):
        return None
    if raw.lower() in ("1", "true", "yes", "on"):
        return True
    return raw


def _stability_from_env(env: Mapping[str, str]) -> bool:
    """工具稳定性层是否开启。**产品路径默认开启**，`WARDEN_STABILITY=0` 可关。

    为什么默认开：没有它，一个卡住的工具会拖住整个会话；有了它，"防卡死 + 抗瞬时故障 +
    熔断"这三件事统一兜在工具调用这一层，不用每个工具自己写。想"原样执行、不做任何
    重试"时（例如审计复现）显式关掉即可。
    重试是安全的：稳定性层按 `ToolSpec.pure` 判定，**非纯工具只对瞬时错误重试**，
    不会把 `fs.delete` 这类有副作用的操作重放。
    """
    off = ("0", "false", "no", "off")
    return env.get("WARDEN_STABILITY", "1").strip().lower() not in off


def _shared_state_from_env(env: Mapping[str, str]) -> bool:
    """是否把协调状态（幂等 / 事件流 / 限流计数）放进共享存储。

    `WARDEN_SHARED_STATE=1` 开启 —— **多副本部署必须开**，否则每个副本各算一份：
    同一 Idempotency-Key 打到不同副本会重复执行、SSE 事件收不到、限流总额度翻倍。
    单副本默认关（进程内实现更快、无轮询）。
    """
    on = ("1", "true", "yes", "on")
    return env.get("WARDEN_SHARED_STATE", "").strip().lower() in on


def _db_path() -> str:
    """SQLite 存档路径。

    容器里用 `WARDEN_DB_PATH` 指向可写卷 —— rootfs 设成只读（read_only: true）时，
    写 WORKDIR 会失败，必须显式给一个挂载卷里的路径。
    """
    return os.environ.get("WARDEN_DB_PATH") or "warden-agent-local.db"


def _cognition_from_env(
    env: Mapping[str, str], catalog: ToolCatalog, model: AgentChatModel,
) -> tuple[Any, Any, int]:
    """按环境变量决定认知能力开关，返回 `(planner, intent, max_context_chars)`。

    产品路径的默认值（按"是否多花钱/是否只赚不赔"来定）：

      · **意图路由**：`ToolIntentRouter` 纯离线确定性、不让模型多跑一次 → **默认开**。
        调用前校验"该不该调"，疑似误调就提示模型而不是直接执行。
      - **上下文裁剪**：防长会话把上下文撑爆，纯收益 → **默认开**（阈值可覆盖）。
      · **阶段规划**：`ModelPlanner` 会为复杂任务**多花一次模型调用** → **默认关**，
        要开就设 `WARDEN_PLANNER=1`（值得，因为复杂任务本来就贵）。
    """
    intent = ToolIntentRouter(catalog)
    on = ("1", "true", "yes", "on")
    planner = (
        ModelPlanner(model)
        if env.get("WARDEN_PLANNER", "").strip().lower() in on
        else None
    )
    raw = env.get("WARDEN_MAX_CONTEXT_CHARS", "").strip()
    limit = int(raw) if raw.isdigit() else DEFAULT_MAX_CONTEXT_CHARS
    return planner, intent, limit


def main() -> None:
    load_env()  # 先读 .env（可选），密钥从环境变量取，不硬编码
    setup_logging()
    store = SqliteStore(_db_path())  # 存档文件（已被 .gitignore 忽略）

    # 模型：有 key 用真 DeepSeek，否则用假模型（离线可跑）
    model: AgentChatModel
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if api_key:
        model = DeepSeekModel(api_key=api_key)
        logger.info("使用真实 DeepSeek 模型")
    else:
        model = FakeModel()
        logger.info("未设置 DEEPSEEK_API_KEY，使用离线假模型（设置 key 可接真实 DeepSeek）")

    # 鉴权（fail-closed）。监听地址与鉴权要一起决定：对外监听却不鉴权 = 直接拒绝启动。
    host = os.environ.get("WARDEN_HOST", "127.0.0.1")
    try:
        api_keys, auth_mode = resolve_auth(os.environ)
    except AuthConfigError as exc:
        logger.error("%s", exc)
        raise SystemExit(2) from exc

    try:
        ensure_listen_is_safe(host, auth_mode)
    except AuthConfigError as exc:
        logger.error("%s", exc)
        raise SystemExit(2) from exc

    if auth_mode == "bearer":
        logger.info("已开启 API 鉴权（请求需带 Authorization: Bearer <key>）")
    else:
        logger.warning(
            "WARDEN_ALLOW_ANON=1：未启用鉴权，且只监听本机 %s（开发用）。"
            "对外暴露端口前必须设置 WARDEN_API_KEY。", host,
        )

    audit_store: Any = None
    if os.environ.get("WARDEN_AUDIT") in ("1", "true", "yes"):
        audit_store = SqliteAuditStore(_db_path())
        logger.info("已开启审计（写入 SQLite audit_log 表）")

    catalog = _build_catalog()
    planner, intent, ctx_chars = _cognition_from_env(os.environ, catalog, model)
    # RAG：按环境变量决定要不要挂 knowledge.search，以及索引什么
    knowledge = _knowledge_from_env(os.environ)
    if knowledge is not None:
        from warden_agent.rag.knowledge import embedder_from_env

        if knowledge is not True and not os.path.isdir(str(knowledge)):
            logger.error("WARDEN_KNOWLEDGE 指向的目录不存在: %s", knowledge)
            raise SystemExit(2)
        logger.info(
            "RAG 知识库：%s（嵌入器=%s）",
            "内置离线语料" if knowledge is True else str(knowledge),
            embedder_from_env(os.environ)[1],
        )
    # 记忆落盘：USER 作用域是"跨会话的用户级记忆"，进程内实现会让它在语义上成立、
    # 实现上落空（重启即丢）。产品路径默认给一个 SQLite 记忆库。
    from warden_agent.memory import SqliteMemoryStore

    memory_repository = SqliteMemoryStore(_db_path())
    logger.info("记忆：SqliteMemoryStore（落盘 %s，重启不丢）", _db_path())

    # Web 工具：默认离线 mock；WARDEN_WEB_FETCH=1 才换成真实联网抓取
    web_providers = providers_from_env(os.environ)
    if type(web_providers[1]).__name__ == "HttpFetchProvider":
        logger.warning(
            "web.fetch 已开启**真实联网抓取**：Agent 可主动访问公网。"
            "每跳都过 WebUrlPolicy（拒内网/环回/云元数据地址），但请确认这是你要的出口策略。"
        )
    else:
        logger.info("web 工具为离线 mock（设 WARDEN_WEB_FETCH=1 可开启真实联网抓取）")
    # 多副本部署：把幂等 / 事件流 / 限流计数放进共享存储（同一个库），
    # 否则每个副本各算一份 —— 幂等失效、SSE 丢事件、限额翻倍。
    shared_state = _shared_state_from_env(os.environ)
    coordination = coordination_for(store, shared=shared_state)
    logger.info(
        "协调状态：%s",
        "共享存储（多副本）"
        if shared_state
        else "进程内（单副本；多副本请设 WARDEN_SHARED_STATE=1）",
    )
    try:
        limiter = limiter_from_env(os.environ, store=coordination[2])
    except ValueError as exc:
        logger.error("%s", exc)
        raise SystemExit(2) from exc
    if limiter is None:
        logger.warning("限流已关闭（WARDEN_RATE_LIMIT=0）——仅建议在受信任的内网/网关后使用")
    else:
        logger.info(
            "限流：每 %s 秒最多 %s 次（按调用者/来源 IP；WARDEN_RATE_LIMIT 可调）",
            limiter.config.window_seconds, limiter.config.max_requests,
        )
    logger.info(
        "认知能力：意图路由=%s｜阶段规划=%s｜上下文裁剪=%s 字符",
        "开" if intent is not None else "关",
        "开" if planner is not None else "关（设 WARDEN_PLANNER=1 可开）",
        ctx_chars or "不裁剪",
    )
    app = build_app(
        model=model,
        catalog=catalog,
        policy=_build_policy(),
        store=store,
        # 默认启用记忆与 Web 搜索（离线可跑）；技能/MCP/Git 按环境变量开启
        memory=True,
        memory_repository=memory_repository,
        knowledge=knowledge,
        web=True,
        web_providers=web_providers,
        skills=os.environ.get("SKILLS_DIR") or None,
        mcp_server=os.environ.get("MCP_SERVER") or None,
        git_workdir=os.environ.get("GIT_WORKDIR") or None,
        api_keys=api_keys,
        audit_store=audit_store,
        model_id=("deepseek" if api_key else "fake"),
        model_api_key=api_key,
        stability=_stability_from_env(os.environ),
        planner=planner,
        intent=intent,
        max_context_chars=ctx_chars,
        rate_limiter=limiter,
        shared_state=shared_state,
    )
    port = int(os.environ.get("PORT", "8000"))
    logger.info("可视化控制台: http://127.0.0.1:%s/  (演示网页)", port)
    logger.info("OpenAPI 文档:  http://127.0.0.1:%s/docs", port)
    logger.info("健康检查:      /health/live  /health/ready")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
