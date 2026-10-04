"""HTTP / SSE 服务：把 Agent 会话对外暴露成能调用的 API。

不是让用户写 Python 代码调用 Agent，而是把 Agent 变成一个"服务"，任何人(或别的程序)
通过 HTTP 就能对话、查看审批、批准/拒绝。

本服务用 FastAPI + uvicorn 实现。提供的接口：
    POST   /chat/{run_id}      送一句话给 Agent，返回最终回答，或"需要审批"
    GET    /status/{run_id}    查会话当前状态
    GET    /approvals          列出所有等待审批的请求（审批队列）
    POST   /approve/{run_id}   批准某会话卡住的工具
    POST   /reject/{run_id}    拒绝某会话卡住的工具
    GET    /events/{run_id}    SSE 事件流（监听该会话的状态变化）

阶段13 新增（产品级 HTTP 服务）：
    GET    /health/live        存活探针（进程活着即 200）
    GET    /health/ready       就绪探针（会 ping 底层存储，断了返 503）
    GET    /audit              查看审计轨迹（谁在何时对哪个会话做了什么）
    认证  ：api_keys 非空时开启，所有业务接口都要 Authorization: Bearer <key>
    审计  ：每条请求记 correlation_id + 调用者 + 操作 + 结果状态
    错误  ：认证/授权失败以 problem+json 返回（RFC 7807 problem+json）

设计要点：
  - 一个 run_id 对应一个长期、可恢复的会话(AgentSession)。
  - 会话状态由 SessionRegistry 管理在内存里，并落到 SQLite；重启服务可恢复。
  - ASK 审批真正"挂起"：模型想调被审批的工具时，API 返回 needs_approval，
    调用方拿到 approval 后调 approve/reject，会话才继续。

**路由访问级别是谁说了算**：唯一权威在 `tests/test_route_surface.py` 的 `ROUTES` 表
  （公开 / 认证 / 管理员三档 + 藏在请求体分支里的管理员限制）。新增任何路由都要在
  那里登记一行，否则 CI 会红——目的是让"新加的路由没守住"被 **CI** 发现，
  而不是被下一位审计者发现（`/metrics` 就给只读角色开过一个口子）。
"""

from __future__ import annotations

import contextlib
import logging
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from warden_agent.core import tracing
from warden_agent.core.metrics import metrics
from warden_agent.credential.broker import CredentialBroker, SecretRedactor, default_broker
from warden_agent.credential.vault import DEPLOYMENT_SCOPE, as_vault
from warden_agent.execution.sandbox import detect_isolation_tier, sandbox_isolation_policy
from warden_agent.model.fake import FakeModel
from warden_agent.model.model import AgentChatModel
from warden_agent.policy.policy import PolicyEngine
from warden_agent.runtime.locking import (
    InProcessRunLock,
    RunLease,
    RunLock,
    new_owner_id,
)
from warden_agent.store.sqlite import SqliteStore
from warden_agent.tool.catalog import ToolCatalog
from warden_agent.tool.stability import build_stability_executor
from warden_agent.web import security_headers
from warden_agent.web.audit import AuditLogger, AuditStore
from warden_agent.web.auth import (
    ApiKeyAuthenticator,
    HttpAuthenticationError,
    HttpAuthorizationError,
    RunOperationAuthorizer,
    TrustedCaller,
    combine_authorizers,
    operation_for,
    owner_authorizer,
    permission_authorizer,
)
from warden_agent.web.coordination import (
    EventBus,
    IdempotencyStore,
    coordination_for,
)

# 拆分后这些模块级辅助住在 http_kit / schemas / session_registry 里。
# 名字仍从本模块**原样可导入**（`from warden_agent.web.server import X` 不受影响）：
# 用 `X as X` 的显式再导出形式，避免被当成未使用的 import。
from warden_agent.web.http_kit import (
    ENV_APPROVAL_ALLOW_SELF as ENV_APPROVAL_ALLOW_SELF,
)
from warden_agent.web.http_kit import (
    SseConcurrencyGate as SseConcurrencyGate,
)
from warden_agent.web.http_kit import (
    _approval_actor as _approval_actor,
)
from warden_agent.web.http_kit import (
    _checkpoint_store_for,
    _drain_and_rebuild,
    _extract_run_id,
    _idem_response_from_store,
    _is_approval_decision_path,
    _is_public,
    _model_key_name,
    _problem,
    _problem_for_status,
)
from warden_agent.web.http_kit import (
    _credential_scope as _credential_scope,
)
from warden_agent.web.http_kit import (
    _has_model_key as _has_model_key,
)
from warden_agent.web.http_kit import (
    _memory_scope_or_400 as _memory_scope_or_400,
)
from warden_agent.web.http_kit import (
    _memory_service_or_404 as _memory_service_or_404,
)
from warden_agent.web.http_kit import (
    _model_kind_headers as _model_kind_headers,
)
from warden_agent.web.http_kit import (
    _plan_to_dict as _plan_to_dict,
)
from warden_agent.web.http_kit import (
    _registered_model_key as _registered_model_key,
)
from warden_agent.web.http_kit import (
    _serialize_messages as _serialize_messages,
)
from warden_agent.web.http_kit import (
    _split_version as _split_version,
)
from warden_agent.web.outbound import OutboundLimiter
from warden_agent.web.ratelimit import RateLimiter, client_key
from warden_agent.web.routes import approvals as _routes_approvals
from warden_agent.web.routes import chat as _routes_chat
from warden_agent.web.routes import memory as _routes_memory
from warden_agent.web.routes import models as _routes_models
from warden_agent.web.routes import ops as _routes_ops
from warden_agent.web.routes import runs as _routes_runs
from warden_agent.web.routes import spa as _routes_spa
from warden_agent.web.routes.spa import _web_dist_dir
from warden_agent.web.schemas import (
    ApprovalDecisionIn as ApprovalDecisionIn,
)
from warden_agent.web.schemas import (
    ChatRequestIn as ChatRequestIn,
)
from warden_agent.web.schemas import (
    ChatResponseOut as ChatResponseOut,
)
from warden_agent.web.schemas import (
    ModelSelectIn as ModelSelectIn,
)
from warden_agent.web.schemas import (
    UserCreateIn as UserCreateIn,
)
from warden_agent.web.session_registry import (
    DEFAULT_SESSION_CACHE_MAX as DEFAULT_SESSION_CACHE_MAX,
)
from warden_agent.web.session_registry import SessionRegistry as SessionRegistry

logger = logging.getLogger(__name__)

# ---- HTTP contract：统一 API 版本（所有响应都带这个头，客户端可据此协商）----
API_VERSION = "1.0"
# 仍然支持协商的**主版本**（"1" 表示 1.x 都接受）。
# 政策（见 docs/api-versioning.md）：主版本变更 = 破坏性变更；次版本只增不改。
# 客户端可以带 `X-Warden-Api-Version` 声明它按哪个版本写的；主版本不被支持时**明确报 400**，
# 而不是"装作没事"继续处理（那会让调用方拿着旧假设去读一个语义已经变了的响应）。
API_SUPPORTED_MAJORS: tuple[str, ...] = ("1",)

# ---- 入站请求保护默认值（可在 build_app 覆盖；0 = 关闭）----
# 请求体上限：默认 1 MiB。防的是超大 body 把内存/JSON 解析打爆（与限流互补：限流管次数）。
DEFAULT_MAX_REQUEST_BYTES = 1024 * 1024

# 可以带请求体的方法（HTTP 规范允许 DELETE 带 body）。请求体闸门按这个集合判，
# 而不是只认 POST/PUT/PATCH：不然一个 DELETE 就能把闸门绕过去。
_BODY_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# SSE 长连接并发上限：默认 100。长连接一直占线程/连接，不限并发时少量客户端就能耗尽连接池。
DEFAULT_SSE_MAX_CONCURRENCY = 100
# SSE 心跳间隔（秒）：空闲这么久就发一条注释行。工具执行/长模型调用期间可能几十秒
# 一个字节都不吐，中间层（nginx/CDN/浏览器）会把"零字节的长连接"当成死连接掐掉。
# 这个常量是**默认值**：真实取值由 `WARDEN_SSE_HEARTBEAT_SECONDS` 决定，在
# `run_server.main()` 里读并显式传给 `build_app`（不读就只是一句空承诺——
# README/docs 早就把它写成可调旋钮了）。0 = 关心跳（见 `routes/chat.py` 的 `_heartbeat_window`）。
# 测试里会 monkeypatch 成极小值，用来验证心跳确实发得出来（不给 `build_app` 传参时
# 走的就是这个全局）。
SSE_HEARTBEAT_SECONDS = 10.0




def build_app(
    model: AgentChatModel,
    catalog: ToolCatalog,
    policy: PolicyEngine,
    store: SqliteStore,
    system_prompt: str = "你是一个能使用工具的助手。",
    memory: bool = False,
    memory_repository: Any = None,
    knowledge: Any = None,
    web_providers: Any = None,
    skills: dict[str, str] | str | None = None,
    web: bool = False,
    mcp_server: str | None = None,
    git_workdir: str | None = None,
    *,
    api_keys: Mapping[str, TrustedCaller] | None = None,
    audit_store: AuditStore | None = None,
    model_id: str = "custom",
    model_api_key: str | None = None,
    stability: Any = None,
    planner: Any = None,
    intent: Any = None,
    max_context_chars: int = 0,
    credential_broker: CredentialBroker | None = None,
    rate_limiter: RateLimiter | None = None,
    shared_state: bool = False,
    idempotency_store: IdempotencyStore | None = None,
    event_bus: EventBus | None = None,
    event_keep: int | None = None,
    outbound_limiter: OutboundLimiter | None = None,
    run_lock: RunLock | None = None,
    maintenance: Any = None,
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
    sse_max_concurrency: int = DEFAULT_SSE_MAX_CONCURRENCY,
    # None = 每次调用时读本模块的全局 `SSE_HEARTBEAT_SECONDS`（测试 monkeypatch 的就是它）；
    # run_server 把环境变量的值显式传进来。两种路径都保留，因为二者的用途不同：
    # 前者让用例能在不起真进程的前提下把间隔压到毫秒级，后者才是部署方的旋钮。
    sse_heartbeat_seconds: float | None = None,
    session_cache_max: int = DEFAULT_SESSION_CACHE_MAX,
    session_ttl_s: float = 0.0,
    multiagent: bool = False,
) -> FastAPI:
    """构建 FastAPI 应用。工厂方式便于测试注入假实现。

    memory/skills/web/mcp_server/knowledge：让 Agent 在 HTTP 服务里也能用这些能力
    （复用 agent.augment_catalog，把对应工具注册进目录）。
      memory_repository：记忆库实现；不传=进程内（重启即丢），传 SqliteMemoryStore 则落盘。
      knowledge        ：RAG 知识来源：VectorStore / True（内置语料）/ 目录路径。

    阶段13 新增（产品级 HTTP 服务）：
      api_keys    ：{API Key: TrustedCaller}。非空则开启认证，业务接口都要带
                    `Authorization: Bearer <key>`；None=本地开发开放（不鉴权）。
      audit_store ：审计后端（AuditStore 实现）。给出则开启审计，每请求记一条；
                    None=不落审计。
      model_id / model_api_key：启动时的模型标识与密钥（/models 切换接口的初始状态）。
    """
    from warden_agent.agent import augment_catalog

    extra = augment_catalog(
        catalog,
        memory=memory,
        memory_repository=memory_repository,
        knowledge=knowledge,
        web_providers=web_providers,
        skills=skills,
        web=web,
        mcp_server=mcp_server,
        git_workdir=git_workdir,
        outbound_limiter=outbound_limiter,
        # 多 Agent：子 Agent 复用**同一个** HTTP 服务模型实例（同一密钥/超时/用量统计口径），
        # 不另建一份模型连接。默认关，由 run_server 按 WARDEN_MULTIAGENT 打开。
        multiagent=multiagent,
        multiagent_model=model if multiagent else None,
        policy_engine=policy,
    )
    registry = SessionRegistry(
        model, catalog, policy, store, system_prompt, extra,
        default_model_id=model_id,
        stability=build_stability_executor(stability),
        planner=planner,
        intent=intent,
        max_context_chars=max_context_chars,
        # 存档点落库：把 SqliteStore 的 checkpoint 方法适配成 CheckpointStore 接口，
        # 会话侧才能真正"记下跑到第几轮、正在哪一步"（见 _owner_of / recovery 端点）。
        checkpoint_store=_checkpoint_store_for(store),
        max_sessions=session_cache_max,
        session_ttl_s=session_ttl_s,
    )

    # 下面三个值在**构建时**算一次：隔离档探测要 fork 一个 `unshare -rn true`
    # （Linux 上），不该让每次 /capabilities 都去探一遍。
    # `demo_instance`：这个进程启动时用的是不是假模型（FakeModel 的回复是固定脚本，
    # 看起来却像真能力）；`model_kind_now`：**当前**默认模型是不是它——
    # /models/select 允许把默认模型切成「离线假模型（免费）」，切完之后这个实例就变成
    # 演示实例了，运维必须能一眼看到，而不是靠翻日志。
    _demo_instance = isinstance(model, FakeModel)
    try:
        _declared_isolation = sandbox_isolation_policy()[0]
    except ValueError:
        # 取值写错不该让 /capabilities 直接 500（启动路径会 fail-fast 报错）
        _declared_isolation = "invalid"
    _detected_isolation = detect_isolation_tier().describe()

    def _close_quietly(resource: Any, what: str) -> None:
        """尽力关闭一个资源；失败只记日志——**停机路径里绝不能再抛异常**。

        为什么：关停时如果第一个 close 抛了，后面的资源就永远不会被关（连接泄漏、
        下次启动可能因文件锁起不来）。所以逐个 try，坏一个不影响其余。
        """
        close = getattr(resource, "close", None)
        if not callable(close):
            return
        try:
            close()
            logger.info("停机：已关闭 %s", what)
        except Exception:  # noqa: BLE001 - 停机清理失败不该影响退出
            logger.warning("停机：关闭 %s 失败（忽略，继续关其它的）", what, exc_info=True)

    @contextlib.asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """应用生命周期：启动只记一行，**停机负责把资源关掉**。

        为什么需要它（此前没有）：进程收到 SIGTERM 时，uvicorn 会等请求排空再退出，
        但我们持有的连接（存储、事件总线的 LISTEN 连接、记忆库）**没有任何地方关闭**——
        依赖进程退出时的资源回收。单副本影响有限，但重启/滚动升级时更容易暴露问题
        （PG 侧留下僵尸连接、SQLite 的网络盘/文件锁场景更明显）。

        顺序：先关"雨伞"再关"地基"——事件总线（可能持有独立连接）→ 记忆库 → 主存储。
        """
        logger.info("启动完成：v%s（停机时会关闭存储/事件总线/记忆库）", API_VERSION)
        try:
            yield
        finally:
            # 注：`bus` / `store` / `memory_repository` 都是在 build_app 里后于本闭包定义的，
            # 但这里在**停机时**才取它们的值（闭包按调用时解析），所以顺序没问题。
            _close_quietly(bus, "事件总线")
            _close_quietly(memory_repository, "记忆库")
            _close_quietly(store, "主存储")
            # 后台维护清扫线程：停机时先让它退出（它自己带 stop(timeout)）
            if maintenance is not None:
                with contextlib.suppress(Exception):
                    maintenance.stop()
            # OTLP 导出器：停机前尽量把队列里的 span 冲刷掉（未开启时是空操作）
            from warden_agent.core import otel

            with contextlib.suppress(Exception):
                otel.shutdown()
            logger.info("停机完成")

    app = FastAPI(
        title="Warden Agent Python", version=API_VERSION, lifespan=_lifespan,
    )
    # 把会话注册表挂到 app 上：便于运维/测试观察缓存占用（有界 LRU 的容量与当前条数）。
    app.state.session_registry = registry

    # ---- 统一错误响应：所有错误都变成 problem+json（不再混 FastAPI 默认的 {"detail": ...}）----
    # 同时拦住端点里的 `raise HTTPException(...)` 与 Starlette 自带的 404/405，
    # 以及请求体校验失败（422）。HTTP 状态码保持不变，只统一**响应体形状**。
    @app.exception_handler(StarletteHTTPException)
    async def _http_error_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        cid = getattr(request.state, "correlation_id", "")
        return _problem_for_status(
            exc.status_code, str(exc.detail), cid, dict(exc.headers or {})
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        cid = getattr(request.state, "correlation_id", "")
        return _problem_for_status(422, "请求体校验失败", cid)

    # ---- Run 级锁：同一会话同一时刻只允许一方驱动 ----
    # 多副本下两个副本同时处理同一个 run 会"后写覆盖前写"（历史分叉或丢失，且不报错）。
    # 抢不到就回 423，让客户端稍后重试，而不是两边一起写。
    # 默认进程内锁（单副本行为不变）；多副本由 run_server 传 SqlRunLock。
    _run_lock: RunLock = run_lock if run_lock is not None else InProcessRunLock()

    def _acquire_run(run_id: str) -> RunLease:
        """为**本次请求**取 Run 租约；抢不到抛 423。返回的 lease 由调用方 `stop()`。

        为什么返回 `RunLease` 而不是 owner 字符串：租约带**后台心跳续租**，所以"这次请求
        跑得比锁 TTL 还久"（模型+工具很慢、或 SSE 流很长）不会让租约中途过期被接管。
        流式路径必须手动 `start()` / `stop()`——锁在端点里取，但要到 SSE 流结束才释放。

        这里用**每请求一个 owner**（而不是"每副本一个"）是刻意的：同一进程内两个并发请求
        驱动同一个 run，同样属于并发写，也必须被挡住——所以不能复用同一个 owner
        （同 owner 重复取锁是允许的，那是给重入用的）。
        """
        lease = RunLease(_run_lock, run_id, new_owner_id())
        if not lease.start():
            holder = _run_lock.owner_of(run_id) or "另一个副本"
            raise HTTPException(
                status_code=423,
                detail=(
                    f"会话 {run_id} 正在被 {holder} 处理中，请稍后重试"
                    "（同一会话同一时刻只允许一方驱动，避免并发写互相覆盖）"
                ),
            )
        return lease

    # ---- SSE 长连接并发闸门 ----
    # 长连接（/chat/stream）会一直占着连接与工作线程，不限并发时少量客户端就能把服务
    # 拖到连健康探针都探不到。到上限**立刻 503**（不排队，排队只会让一堆请求一起超时），
    # 名额在该请求的 SSE 流结束时释放（含客户端断开触发的 GeneratorExit）。
    sse_gate = SseConcurrencyGate(sse_max_concurrency)
    app.state.sse_gate = sse_gate  # 挂到 app 上，便于运维/测试观察当前占用量

    # ---- 模型切换：傻瓜式接入的模型目录，/models 查询、/models/select 切换/导入 ----
    from warden_agent.model import deepseek as _ds

    model_catalog: dict[str, dict[str, Any]] = {
        "fake": {
            "name": "离线假模型（免费）",
            "needs_key": False,
            "make": lambda key=None: FakeModel(),
        },
        "deepseek": {
            "name": "DeepSeek",
            "needs_key": True,
            "make": lambda key: _ds.DeepSeekModel(api_key=key),
        },
        "openai": {
            "name": "OpenAI",
            "needs_key": True,
            "make": lambda key: _ds.OpenAIModel(api_key=key),
        },
        "zhipu": {
            "name": "智谱 GLM",
            "needs_key": True,
            "make": lambda key: _ds.ZhipuModel(api_key=key),
        },
        "bailian": {
            "name": "阿里百炼",
            "needs_key": True,
            "make": lambda key: _ds.BailianModel(api_key=key),
        },
    }
    # 已导入的模型 API Key 由凭证 broker 保管：**加密落库 + 短租约**，不再明文躺在
    # 一个 dict 里，也不再随进程退出而丢失（store 支持落库时自动用 store 当保管库；
    # 内存版存储则退回进程内）。redactor 用于把密钥从日志中抹掉（见网关异常分支）。
    broker = credential_broker or default_broker(vault=as_vault(store))
    redactor = SecretRedactor()
    if model_api_key and model_id in model_catalog:
        # 启动配置的 key 记为**部署级**：全体调用者共用（它由部署者提供，不属于某个用户）。
        broker.register(
            _model_key_name(model_id), {"api_key": model_api_key}, DEPLOYMENT_SCOPE
        )
        redactor.add(model_api_key)
    current_model_id = model_id
    # 把脱敏器挂到 app 上，供其它处理器/扩展复用（密钥不进日志）
    app.state.secret_redactor = redactor

    def _identity(request: Request, query_user_id: str | None) -> str:
        """解析"这次请求属于哪个用户"。

        认证模式：以**凭证派生的身份**为准（`caller.user_id`），忽略查询参数——
        身份由服务端从 key 推出，客户端说了不算。这正是多租户与"按字段过滤"的分界。
        匿名开发模式：沿用查询参数（前端靠它切换演示账号），缺省 `demo-user`。
        """
        caller: TrustedCaller | None = getattr(request.state, "caller", None)
        if caller is not None:
            return caller.user_id
        return query_user_id or "demo-user"

    _routes_models.register(
        app,
        _identity=_identity,
        broker=broker,
        current_model_id=current_model_id,
        model_catalog=model_catalog,
        registry=registry,
        redactor=redactor,
    )

    # ---- T8 可观测性：指标定义（全局注册表，Prometheus 文本输出）----
    m = metrics()
    m_http = m.counter("warden_http_requests_total", "HTTP 请求总数", ["method", "path"])
    m_http_errors = m.counter("warden_http_errors_total", "HTTP 5xx 错误数", ["method", "path"])
    # 桶上界必须盖住告警/面板要看的最大延迟：分位数落在 +Inf 桶时，histogram_quantile
    # 返回的是**最高有限桶的上界**（不是 +Inf）。旧桶集到 1.0 为止，于是
    # `histogram_quantile(...) > 2` 不论服务多慢都恒为假（第五十九批：promtool 单元测试实测
    # 10% 请求超过 1s 时 p95 仍返回 1.0）。加 2.5/5.0/10.0 之后 >2s 才是真能测到的。
    m_http_latency = m.histogram(
        "warden_http_request_duration_seconds",
        "HTTP 请求耗时(秒)",
        [0.01, 0.05, 0.1, 0.5, 1.0, 2.5, 5.0, 10.0],
    )
    m_approvals = m.counter("warden_approvals_total", "审批决策数", ["action"])
    m_rate_limited = m.counter("warden_rate_limited_total", "被限流拒绝的请求数", ["path"])
    # 运维告警用的 gauge：等待人工处理超过阈值的 Run 数。抓取时现算（见 _refresh_stuck_gauge），
    # 不与写入路径耦合——这样重启/多副本都不会让这个数漂移。
    m_stuck = m.gauge("warden_stuck_runs", "等待人工处理超过阈值的 Run 数", ["older_than"])
    # 审计链未配密钥（退化为不带密钥哈希链）：暴露成 gauge，并在 /health/ready 上降级。
    # 没配密钥不是错误（默认仍非致命），但必须**可观测**——否则会“以为在防篡改、其实没有”。
    audit_unkeyed = audit_store is not None and getattr(audit_store, "keyed", True) is False
    m_audit_unkeyed = m.gauge(
        "warden_audit_unkeyed", "审计链未配置密钥（1=退化为不带密钥哈希链）"
    )
    m_audit_unkeyed.set(1 if audit_unkeyed else 0)

    # ---- 阶段13：认证 + 审计中间件 ----
    authenticator = ApiKeyAuthenticator(api_keys) if api_keys else None

    def _owner_of(run_id: str) -> str | None:
        """读某个 Run 的归属用户（不存在或尚无归属时返回 None）。"""
        run = store.load_run(run_id)
        if run is not None and run.user_id:
            return run.user_id
        # 尚未落库归属时，回落到“本进程首个创建者”——否则客户端可先看一个无归属
        # 的 run_id、再让另一 principal 通过 /chat 把它“接管”成自己的（抢注）。
        return registry.creator_of(run_id)

    # 认证开启 → 启用授权：先过**角色权限**（能不能做这类动作），
    # 再过**按归属**（能不能碰这个 Run）。
    # 未认证（anon-dev）→ 保持旧的开放行为，本地开发不该被租户边界挡死。
    #
    # `_approval_only_authorizer` 是**唯一**一条"管理员可跨归属"的例外，
    # 见 `_is_approval_decision_path` 的注释：职责分离要求审批人不是发起人，
    # 而归属授权会把"别人名下的 Run"一律拦下——两条规则叠加的默认结果是"谁也批不了"。
    permission_only = permission_authorizer()
    owner_gate = RunOperationAuthorizer(
        combine_authorizers(permission_only, owner_authorizer(_owner_of))
    )
    authorizer = owner_gate if authenticator is not None else RunOperationAuthorizer()


    def _owner_scope(request: Request) -> str | None:
        """读路径的**归属过滤口径**：普通用户 = 自己；**管理员 = None（全局视图）**。

        只用于"运维要看全局"的三处（审计 / 挂起告警 / 恢复计划）。
        ⚠️ **不用于 `/memory/{scope}`**：那是用户内容，管理员也不该随便读别人的记忆——
        运维需要的是"系统状态"的全局视图，不是"用户数据"的全局视图。
        """
        caller: TrustedCaller | None = getattr(request.state, "caller", None)
        if caller is None:
            return None          # 匿名开发模式：保持历史行为（不过滤）
        return None if caller.is_admin else caller.user_id

    audit = AuditLogger(audit_store) if audit_store is not None else None
    # 协调状态：幂等表 / 事件总线。
    #   shared_state=False（默认）→ 进程内实现，单副本行为与历史一致；
    #   shared_state=True          → 存储实现，多副本读写同一张表，幂等与事件流才跨副本成立。
    #   event_keep：进程内总线每个 run 保留的最近事件数（`WARDEN_EVENT_KEEP`）；
    #   由 run_server 透传，避免这里用默认值"另起一套"——否则配置了也不生效。
    _coordination = coordination_for(store, shared=shared_state, event_keep=event_keep)
    idem_store: IdempotencyStore = idempotency_store or _coordination[0]
    bus: EventBus = event_bus or _coordination[1]
    # 中间件闭包里带"当前是否开启"标志，`_is_public`/`_extract_run_id` 复用在端点里
    audit_enabled = audit is not None

    @app.middleware("http")
    async def _gateway(request: Request, call_next: Any) -> Any:
        """统一入口：分配 correlation_id → 认证 → 授权 → 执行业务 → 落审计 + 记指标。"""
        correlation_id = request.headers.get("X-Correlation-Id") or uuid.uuid4().hex
        # 把 correlation_id 挂到 request 上：异常处理器（统一 problem+json）能取到它
        request.state.correlation_id = correlation_id
        method = request.method
        path = request.url.path
        run_id = _extract_run_id(path)
        operation = operation_for(method, path)
        caller: TrustedCaller | None = None
        status_code = 200
        _start = time.monotonic()  # T8：请求开始计时
        # 幂等占位标记必须**在 try 之前**初始化：下面的兜底 except 要用它来决定是否释放占位，
        # 而认证/授权阶段抛异常时 try 内那次赋值还没执行到——不初始化的话兜底块自己会
        # UnboundLocalError，把「干净的 500 problem+json」变成未捕获异常（错误路径最脆）。
        idem_reserved = False
        # 链路追踪：从入站 traceparent 接着上游的链（没有就起新链）。这里手动进入
        # 上下文（而非 with），是为了不把后面整段网关逻辑重新缩进一层；退出放在 finally。
        # 进入后 contextvar 里就有当前 trace，出站调用会用同一 trace_id 透传给下游。
        trace_cm = tracing.server_span(
            "http.request",
            request.headers.get("traceparent"),
            attributes={"method": method, "path": path},
        )
        trace_ctx = trace_cm.__enter__()
        try:
            # 请求体大小上限：超限返 413。
            # 判不判的依据是“这个请求可能带 body”，而不是“它是 POST/PUT/PATCH”：
            # DELETE 按 HTTP 规范也可以带 body，旧口径把它放了过去——一个 DELETE 就能把
            # 任意大的 body 灌进来（同一个闸门，同一份内存）。现在改为：
            #   · 声明了 Content-Length（任何方法）→ 按声明值判；
            #   · 没声明但方法本身可以带 body（POST/PUT/PATCH/DELETE）→ 边读边计数。
            # 先看 Content-Length 是廉价路径；无声明时必须真读并计数，否则不声称长度的
            # 客户端能绕过闸门。读完（未超限）后把已读字节挂回 `request._body`：
            # Starlette 的 BaseHTTPMiddleware 会把它透给下游，下游照常能读到 body
            # （口径与 request.body() 一致，但内存被上限封顶）。
            declared = request.headers.get("content-length")
            declared_bytes = int(declared) if declared and declared.isdigit() else None
            if max_request_bytes > 0 and (
                declared_bytes is not None or method in _BODY_METHODS
            ):
                if declared_bytes is not None and declared_bytes > max_request_bytes:
                    status_code = 413
                    return _problem(
                        413, "PAYLOAD_TOO_LARGE",
                        f"请求体超过上限 {max_request_bytes} 字节",
                        correlation_id,
                    )
                if declared_bytes is None:
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in request.stream():
                        total += len(chunk)
                        if total > max_request_bytes:
                            status_code = 413
                            return _problem(
                                413, "PAYLOAD_TOO_LARGE",
                                f"请求体超过上限 {max_request_bytes} 字节",
                                correlation_id,
                            )
                        chunks.append(chunk)
                    request._body = b"".join(chunks)  # 透传给下游（见上）
            # API 版本协商：客户端可用 `X-Warden-Api-Version` 声明它按哪个版本写的。
            # 主版本不被支持 → **明确 400**（而不是装作没事继续处理：调用方拿着旧假设去读
            # 语义已经变了的响应，比直接报错危险得多）。只认主版本，次版本差异不拒绝。
            requested_version = request.headers.get("X-Warden-Api-Version")
            if requested_version:
                parsed = _split_version(requested_version)
                if parsed is None or parsed[0] not in API_SUPPORTED_MAJORS:
                    status_code = 400
                    return _problem(
                        400, "UNSUPPORTED_API_VERSION",
                        f"不支持的 API 版本 {requested_version!r}；"
                        f"当前版本 {API_VERSION}，支持的主版本 {list(API_SUPPORTED_MAJORS)}",
                        correlation_id,
                    )
            if authenticator is not None and not _is_public(path):
                try:
                    caller = authenticator.authenticate(request)
                except HttpAuthenticationError as e:
                    # 提前返回也要把状态码写回，否则 finally 会按 200 记指标/审计
                    status_code = 401
                    return _problem(401, "AUTHENTICATION_REQUIRED", str(e), correlation_id)
                if caller is not None:
                    # 把已认证身份挂到 request 上，端点据此解析归属（见 _identity）
                    request.state.caller = caller
                    # 防 run_id 抢注：首个“看到”一个无归属 Run 的 principal 即它的创建者，
                    # 后续别的 principal 再碰同一 run_id 会被 owner_authorizer（经 _owner_of）拒绝。
                    if run_id is not None:
                        registry.note_creator(run_id, caller.user_id)
                    try:
                        if caller.is_admin and _is_approval_decision_path(path):
                            # 审批人（admin）跨归属批别人的 Run：这是与"职责分离"配套的
                            # **窄权限**——只放开归属这一道，角色权限仍然照跑
                            # （viewer 即便被写进 admin 名单也不会因此拿到 COMMAND）。
                            # run_id 传 None = 不做归属校验。
                            # 注意：`permission_authorizer()` 返回的是**裸回调**
                            # （AuthorizeFn = Callable[[caller, operation, run_id], None]），
                            # 不是 RunOperationAuthorizer 那种带 .authorize() 的对象。
                            # 曾经写成 `permission_only.authorize(...)` → 必抛
                            # AttributeError，于是「admin 跨归属审批」这条路径从来没真正跑通过。
                            permission_only(caller, operation, None)
                        else:
                            authorizer.authorize(caller, operation, run_id)
                    except HttpAuthorizationError as e:
                        status_code = 403
                        return _problem(403, "AUTHORIZATION_DENIED", str(e), correlation_id)
            # 限流：健康探针等公开路径豁免（负载均衡探活不能被限流挡住），
            # 其余按调用者身份（匿名时按来源 IP）计数。
            if rate_limiter is not None and not _is_public(path):
                rl_key = client_key(
                    caller.user_id if caller is not None else None,
                    request.client.host if request.client else None,
                )
                allowed, retry_after = rate_limiter.check(rl_key)
                if not allowed:
                    m_rate_limited.inc(labels=(path,))
                    status_code = 429
                    return _problem(
                        429,
                        "RATE_LIMITED",
                        f"请求过于频繁，请 {retry_after} 秒后重试",
                        correlation_id,
                        extra_headers={"Retry-After": str(retry_after)},
                    )
            # 幂等：带 Idempotency-Key 的 POST，同 key 重复请求返回同一结果。
            # 流式端点(SSE)不参与幂等缓存（消费流会破坏它）。
            #
            # 键**按调用者作用域**：幂等表是全局的，若直接拿客户端给的 header 当键，
            # 任一认证用户用"别人用过的 key"重放，就会**读到别人的缓存响应体**
            # （/chat 的响应含模型输出与会话状态），甚至抢占 key 让别人的请求被 409。
            raw_idem = request.headers.get("Idempotency-Key")
            caller_scope = (
                caller.user_id if caller is not None
                else (request.client.host if request.client else "anonymous")
            )
            idem_key = f"{caller_scope}:{raw_idem}" if raw_idem else None
            is_stream = path.startswith("/events") or path.startswith("/chat/stream")
            # 幂等：带 Idempotency-Key 的 POST，同 key 重复请求返回同一结果。
            # 关键在**先原子占位再执行**——"先查后做后写"有 TOCTOU：两个同 key 请求
            # 会都在"查"处读到空，于是各执行一次副作用（正是网关重试/多副本要防的）。
            idem_reserved = False
            if idem_key and method == "POST" and not is_stream:
                cached_resp = _idem_response_from_store(idem_store, idem_key)
                if cached_resp is not None:
                    status_code = cached_resp.status_code
                    return cached_resp
                if not idem_store.reserve(idem_key):
                    # 另一个同 key 请求正在处理中：明确回 409，别让它并发执行第二遍
                    status_code = 409
                    return _problem(
                        409, "IDEMPOTENCY_IN_FLIGHT",
                        "同一 Idempotency-Key 的请求正在处理中，请稍后重试",
                        correlation_id, extra_headers={"Retry-After": "1"},
                    )
                idem_reserved = True
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Correlation-Id"] = correlation_id
            response.headers["X-Warden-Api-Version"] = API_VERSION
            if trace_ctx is not None:
                # 把本链路的 traceparent 回给调用方：它可作为"这次请求在链上的位置"的对账凭据
                response.headers["traceparent"] = trace_ctx.to_traceparent()
            if idem_key and method == "POST" and not is_stream:
                if 200 <= status_code < 300:
                    # **只缓存 2xx 成功结果**。此前是 `<500` 就缓存，于是瞬时 423（Run 锁）/409
                    # 会被永久写进幂等表——同 key 重试永远命中那条失败响应，客户端再也推进不了。
                    response = await _drain_and_rebuild(response, idem_store, idem_key)
                elif idem_reserved:
                    # 非 2xx（423/409/4xx/5xx）：不缓存，并释放占位，让同 key 可以重试
                    idem_store.release(idem_key)
            return response
        except Exception as exc:  # noqa: BLE001 - 网关兜底，不泄漏内部细节
            # 异常信息可能带上请求体/URL 里的密钥，落日志前先脱敏
            logger.exception(
                "网关异常 method=%s path=%s err=%s", method, path, redactor.redact(str(exc))
            )
            # 执行失败要释放占位，否则这个 key 会永久卡在"处理中"，客户端永远重试不了
            if idem_reserved and idem_key:
                idem_store.release(idem_key)
            # 兜底 500 也必须写回状态码：否则 finally 会把它当成 200 记进指标与审计，
            # 于是"服务在报 500"这件事在可观测面上完全不可见。
            status_code = 500
            return _problem(500, "INTERNAL_ERROR", "请求未能完成", correlation_id)
        finally:
            # 指标 path 标签用**路由模板**（如 /status/{run_id}），而不是原始 URL：
            # 原始 URL 带 run_id/scope，会让时间序列基数无界（每个 run 一条），Prometheus 会被打爆。
            # 未匹配到路由（早期返回 / 未命中）时回落固定串 "other"，同样是有界的。
            route = request.scope.get("route")
            path_label = getattr(route, "path", None) or "other"
            # T8 指标：请求数 + 耗时分布 + 5xx 错误数（耗时直方图：桶已在注册时绑定）
            m_http.inc(labels=(method, path_label))
            m_http_latency.observe(time.monotonic() - _start)
            if status_code >= 500:
                m_http_errors.inc(labels=(method, path_label))
            if audit_enabled:
                audit.record(  # type: ignore[union-attr]
                    correlation_id=correlation_id,
                    caller=caller,
                    operation=operation,
                    run_id=run_id,
                    method=method,
                    path=path,
                    status=status_code,
                )
            # 结束 span（打一行带 trace_id / 耗时的结构化日志并还原上下文）。
            # 放最后：这样 span 的耗时能覆盖到审计记录这一步。
            trace_cm.__exit__(None, None, None)

    _routes_ops.register(
        app,
        _owner_of=_owner_of,
        _owner_scope=_owner_scope,
        audit=audit,
        audit_store=audit_store,
        audit_unkeyed=audit_unkeyed,
        authenticator=authenticator,
        logger=logger,
        m_stuck=m_stuck,
        store=store,
    )

    _routes_spa.register(app)

    _routes_chat.register(
        app,
        _acquire_run=_acquire_run,
        _identity=_identity,
        bus=bus,
        logger=logger,
        registry=registry,
    )

    _routes_runs.register(
        app,
        _identity=_identity,
        registry=registry,
        store=store,
    )

    _routes_approvals.register(
        app,
        _acquire_run=_acquire_run,
        _owner_scope=_owner_scope,
        bus=bus,
        m_approvals=m_approvals,
        registry=registry,
        store=store,
    )

    _routes_chat.register_streaming(
        app,
        _acquire_run=_acquire_run,
        _identity=_identity,
        bus=bus,
        logger=logger,
        registry=registry,
        sse_gate=sse_gate,
        sse_heartbeat_seconds=(
            (lambda: SSE_HEARTBEAT_SECONDS)
            if sse_heartbeat_seconds is None
            else (lambda: sse_heartbeat_seconds)
        ),
        sse_max_concurrency=sse_max_concurrency,
    )

    @app.get("/capabilities")
    def capabilities() -> dict[str, Any]:
        """列出这个 Agent 服务目前可用的能力（工具 + 启用的特性）。"""
        tool_names = sorted(t.name for t in registry._catalog.all())
        skill_cat = registry.extra.get("skill_catalog")
        knowledge_sources = registry.extra.get("knowledge_sources")
        return {
            "tools": tool_names,
            "features": {
                "memory": "memory_service" in registry.extra,
                # 记忆是否落盘：进程内实现重启即丢，运维/前端需要能区分
                "memory_persistent": bool(registry.extra.get("memory_persistent")),
                "skills": [s for s in (skill_cat.aliases() if skill_cat else [])],
                "web": any(t.name.startswith("web.") for t in registry._catalog.all()),
                "mcp_server": registry.extra.get("mcp_server"),
                # 真实联网抓取是否开启（默认是离线 mock）——运维需要能一眼看到外网出口状态
                "web_fetch": registry.extra.get("web_fetch"),
                # 搜索 provider（离线 mock vs 真实联网搜索）——同样要能看到
                "web_search": registry.extra.get("web_search"),
                # 是不是离线演示 provider（None = 该能力没注册），以及“为什么退回 mock”。
                # 前端要能用它把“搜到了”标成“这是演示数据”，否则用户会以为真的联网搜了。
                "web_search_mock": registry.extra.get("web_search_mock"),
                "web_fetch_mock": registry.extra.get("web_fetch_mock"),
                "web_degraded_reason": registry.extra.get("web_degraded_reason") or "",
                # RAG：报出嵌入器名，避免“词频嵌入被当成语义检索”
                "knowledge_embedder": registry.extra.get("knowledge_embedder"),
                "knowledge_sources": len(knowledge_sources) if knowledge_sources else 0,
                # Run 锁用的是哪种实现：进程内锁在多副本下等于没锁，运维必须能一眼看到
                "run_lock": type(_run_lock).__name__,
                # 模型究竟是真实推理还是**演示脚本**。FakeModel 的回复看起来像真能力，
                # 不标出来就会被当成系统能力的证据（这是“能力错觉”，不是小事）。
                "demo_instance": _demo_instance,
                "model_kind": "fake" if _demo_instance else "real",
                "model_kind_now": "fake" if registry._default_model_id == "fake" else "real",
                # 沙箱：shell.run 在 HTTP 路径上**默认没有**（这里跑的是多租户 API，
                # 没有 per-tenant 文件系统边界，任意 shell 不适合直接开放）。
                # 万一被显式开启，隔离档必须如实上报——「只是语义层」不许讲成隔离。
                "sandbox_shell": bool(registry.extra.get("sandbox")),
                "sandbox_isolation": registry.extra.get("sandbox_isolation_note")
                or "未启用 shell.run（HTTP 路径默认不注册）",
                "sandbox_isolation_declared": _declared_isolation,
                "sandbox_isolation_detected": _detected_isolation,
            },
        }

    _routes_memory.register(
        app,
        _identity=_identity,
        registry=registry,
    )

    # ---- T10：托管 React 构建产物的静态资源（/assets/...）----
    # 若 web/dist 存在，把它的静态文件挂到 /assets，让 SPA 的 JS/CSS 能被同源加载，
    # 实现"单端口部署"（FastAPI 同时当 API 和前端服务器）。
    try:
        import os

        dist_dir = _web_dist_dir()
        assets_dir = os.path.join(dist_dir, "assets")
        if assets_dir and os.path.isdir(assets_dir):
            app.mount("/assets", StaticFiles(directory=assets_dir), name="warden-assets")
    except Exception:  # noqa: BLE001 - 静态挂载失败不连累 API
        logger.exception("React 静态资源挂载失败，仅 API 可用")

    # ---- 安全响应头（最后挂 = 最外层）----
    # 必须挂在网关中间件**之后**：Starlette 里后加的在最外层，这样 404 / 401 / 500
    # 这些**错误响应**也带上 CSP 与防套框——出错的那一页一样能被 iframe 套住、被嗅探。
    # 只改头不改体（纯 ASGI），所以 SSE 流式输出不受影响。
    security_headers.install(app)

    return app
