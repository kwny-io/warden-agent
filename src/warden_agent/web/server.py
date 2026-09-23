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
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from warden_agent.core import tracing
from warden_agent.core.metrics import metrics
from warden_agent.core.settings import env_opt
from warden_agent.credential.broker import CredentialBroker, SecretRedactor, default_broker
from warden_agent.credential.vault import DEPLOYMENT_SCOPE, as_vault
from warden_agent.model.model import AgentChatModel, Message
from warden_agent.policy.policy import PolicyEngine
from warden_agent.runtime.locking import (
    InProcessRunLock,
    RunLease,
    RunLock,
    new_owner_id,
)
from warden_agent.runtime.session import AgentSession, FinalReply, NeedsApproval
from warden_agent.store.sqlite import SqliteStore
from warden_agent.tool.catalog import ToolCatalog
from warden_agent.tool.stability import build_stability_executor
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
from warden_agent.web.health import HealthResult, liveness, readiness
from warden_agent.web.outbound import OutboundLimiter
from warden_agent.web.ratelimit import RateLimiter, client_key

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
# SSE 长连接并发上限：默认 100。长连接一直占线程/连接，不限并发时少量客户端就能耗尽连接池。
DEFAULT_SSE_MAX_CONCURRENCY = 100
# 会话内存缓存上限：默认 1000。任何 GET /status/<随机 id> 都会建一个 AgentSession，
# 不透顶时内存可被客户端任意 run_id 撑爆；到上限按 LRU 淘汰最久未访问的会话。
DEFAULT_SESSION_CACHE_MAX = 1000


class SseConcurrencyGate:
    """SSE 长连接并发闸门：到上限**立刻拒绝**（不排队），名额用完释放才回收。

    用 `threading.Lock`：流的生成器在 Starlette 的线程池里迭代，释放可能发生在工作线程，
    `asyncio.Lock` 跨线程不安全；临界区只做自增/自减，开销可忽略。
    `limit <= 0` 表示不限并发（`acquire()` 永远成功）。
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._active = 0
        self._lock = threading.Lock()

    def acquire(self) -> bool:
        """名额占得到返回 True；已到上限返回 False（调用方应立刻回 503）。"""
        with self._lock:
            if self.limit > 0 and self._active >= self.limit:
                return False
            self._active += 1
            return True

    def release(self) -> None:
        """归还名额（幂等石旁：多发/多减不会把计数打到负）。"""
        with self._lock:
            self._active = max(0, self._active - 1)

    @property
    def active(self) -> int:
        """当前在飞的长连接数（供运维/测试观察）。"""
        with self._lock:
            return self._active


def _split_version(value: str) -> tuple[str, str] | None:
    """把 `1.0` 拆成 `("1", "0")`；形态不对返回 None。"""
    parts = value.strip().split(".")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    return parts[0], parts[1]


async def _drain_and_rebuild(
    response: Any, store: IdempotencyStore, key: str
) -> Any:
    """消费 body 流，把**完整快照**（状态码/头/body）写进幂等表，返回可重放的新 Response。

    注意快照以**这次响应自己**为准，而不是去改那条"处理中"占位——占位的 status_code 是 0。
    """
    body_bytes = b"".join([chunk async for chunk in response.body_iterator])
    store.put(key, {
        "status_code": response.status_code,
        "headers": dict(response.headers),
        "body": body_bytes,
    })

    headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
    return JSONResponse(
        status_code=response.status_code,
        content=json.loads(body_bytes) if body_bytes else None,
        headers=headers,
    )


def _idem_response_from_store(store: IdempotencyStore, key: str) -> Any | None:
    item = store.get(key)
    if not item or item.get("body") is None:
        return None
    body = item.get("body")
    content: Any
    try:
        content = json.loads(body) if isinstance(body, (bytes, str)) else body
    except (json.JSONDecodeError, TypeError):
        content = body
    return JSONResponse(
        status_code=item["status_code"],
        content=content,
        headers={k: v for k, v in item["headers"].items() if k.lower() != "content-length"},
    )



# ---- 阶段13：problem+json 错误（RFC 7807 problem+json）----
# 统一错误响应契约：**所有**错误（含端点里的 `raise HTTPException` 与请求体校验失败）
# 都经下面的异常处理器变成同一种 problem+json 形状，不再混着 FastAPI 默认的 `{"detail": ...}`。
# 状态码 → errorCode 的映射；端点需要更具体的 code 时可直接调 `_problem`。
_STATUS_ERROR_CODES: dict[int, str] = {
    400: "BAD_REQUEST",
    401: "AUTHENTICATION_REQUIRED",
    403: "AUTHORIZATION_DENIED",
    404: "NOT_FOUND",
    409: "CONFLICT",
    413: "PAYLOAD_TOO_LARGE",
    422: "VALIDATION_FAILED",
    423: "RUN_LOCKED",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
    501: "NOT_IMPLEMENTED",
    503: "SERVICE_UNAVAILABLE",
}
_API_TITLES: dict[int, str] = {
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    409: "Conflict",
    413: "Payload Too Large",
    422: "Unprocessable Entity",
    423: "Locked",
    429: "Too Many Requests",
    500: "Internal Server Error",
    501: "Not Implemented",
    503: "Service Unavailable",
}


def _problem(
    status: int,
    code: str,
    detail: str,
    correlation_id: str,
    extra_headers: dict[str, str] | None = None,
) -> JSONResponse:
    """构造 RFC 7807 风格的 problem+json 响应（RFC 7807 problem+json）。"""
    return JSONResponse(
        status_code=status,
        content={
            "type": f"urn:warden:problem:{code.lower()}",
            "title": _API_TITLES.get(status, "Error"),
            "status": status,
            "errorCode": code,
            "correlationId": correlation_id,
            "detail": detail,
            "timestamp": datetime.now(UTC).isoformat(),
        },
        headers={
            "Content-Type": "application/problem+json",
            "X-Warden-Api-Version": "1.0",
            "X-Correlation-Id": correlation_id,
            **(extra_headers or {}),
        },
    )


def _problem_for_status(
    status: int,
    detail: str,
    correlation_id: str,
    extra_headers: dict[str, str] | None = None,
) -> JSONResponse:
    """按 HTTP 状态码给出统一的 problem+json（异常处理器用，errorCode 由状态码映射）。"""
    return _problem(
        status, _STATUS_ERROR_CODES.get(status, "ERROR"), detail,
        correlation_id, extra_headers,
    )


def _extract_run_id(path: str) -> str | None:
    """从请求路径里挖出 run_id（用于授权与审计），挖不到返回 None。

    覆盖所有"针对某个 Run"的路由：chat / status / approve / reject / events / runs /
    messages。少覆盖一条，归属授权（owner_authorizer）在那条路上就等于没开——
    所以这里和路由表必须同步维护。
    """
    segments = path.rstrip("/").split("/")
    if len(segments) >= 3:
        # /chat/stream/{run_id} → ["", "chat", "stream", id]
        if segments[1] == "chat" and len(segments) == 4 and segments[2] == "stream":
            return segments[3]
        if segments[1] in (
            "chat", "status", "approve", "reject", "events", "runs", "messages",
        ):
            return segments[2]
    return None


def _is_public(path: str) -> bool:
    """无需认证即可访问的路径：文档、演示首页、健康检查（负载均衡探活必经之路）。"""
    public = {
        "/",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/health/live",
        "/health/ready",
    }
    return path in public


# ---- HTTP 请求/响应模型 ----
class ChatRequestIn(BaseModel):
    text: str


class ModelSelectIn(BaseModel):
    """POST /models/select 的请求体（模型切换 / 导入 API Key）。"""

    id: str
    api_key: str | None = None
    # "self"（默认）= 只切自己的会话；"deployment" = 切**部署默认**（所有人没自选过的会话都受影响）
    # ——后者是运维动作，**只有管理员**能做（见 /models/select 的 403 分支）。
    scope: str = "self"


class UserCreateIn(BaseModel):
    """POST /users 的请求体（登记中控台用户）。"""

    user_id: str


class ChatResponseOut(BaseModel):
    run_id: str
    status: str
    kind: str  # "final" | "needs_approval" | "error"
    text: str | None = None
    approval: dict[str, Any] | None = None
    messages: list[dict[str, Any]] | None = None


class SessionRegistry:
    """管理所有在跑的会话：按 run_id 存 AgentSession，并锁住线程安全。"""

    def __init__(
        self,
        model: AgentChatModel,
        catalog: ToolCatalog,
        policy: PolicyEngine,
        store: SqliteStore,
        system_prompt: str = "你是一个能使用工具的助手。",
        extra: dict[str, Any] | None = None,
        default_model_id: str = "",
        stability: Any = None,
        planner: Any = None,
        intent: Any = None,
        max_context_chars: int = 0,
        checkpoint_store: Any = None,
        max_sessions: int = DEFAULT_SESSION_CACHE_MAX,
        session_ttl_s: float = 0.0,
    ) -> None:
        self._model = model
        self._default_model_id: str = default_model_id
        # 按归属者（用户）记的模型选择：多用户下不能让任一个人切走所有人的模型
        # （那是跨租户影响：全体对话改走他的 key，或被他切成离线假模型）。
        self._owner_models: dict[str, tuple[str, AgentChatModel]] = {}
        self._catalog = catalog
        self._policy = policy
        self._store = store
        self._system_prompt = system_prompt
        self._stability = stability
        self._planner = planner
        self._intent = intent
        self._max_context_chars = max_context_chars
        self._checkpoint_store = checkpoint_store
        self.extra = extra or {}  # 额外能力（如 memory_service / skill_catalog）
        # 有界 LRU 缓存：`_sessions` 是 OrderedDict（队尾=最近访问），`_max_sessions`
        # 到上限时淘汰队首（最久未访问）。`_session_seen` 记访问时刻供 TTL 淘汰。
        # 为什么必须有界：任何 `GET /status/<随机 id>` 都会新建一个 AgentSession，
        # 不限的话客户端用一个循环就能把内存撑爆。
        self._sessions: OrderedDict[str, AgentSession] = OrderedDict()
        self._session_seen: dict[str, float] = {}
        self._max_sessions = max_sessions
        self._session_ttl_s = session_ttl_s
        # 防 run_id 抢注：首个“看到”某个 Run 的 principal（进程内）。
        # 客户端自带的 run_id 是先到先得，不记下来就能被另一 principal 后手接管。
        self._created_by: dict[str, str] = {}
        # RLock（可重入）：`get()` 持锁时还要调 `model_for()` 解析归属者模型，
        # 用普通 Lock 会自锁死。
        self._lock = threading.RLock()

    def note_creator(self, run_id: str, principal: str) -> None:
        """记下某个 Run 的首个创建者（首次生效，后续调用不覆盖）。"""
        with self._lock:
            self._created_by.setdefault(run_id, principal)

    def creator_of(self, run_id: str) -> str | None:
        """某个 Run 在**本进程内**被谁首先创建；没记过则 None。"""
        with self._lock:
            return self._created_by.get(run_id)

    def _evict_stale(self) -> None:
        """清掉超过 TTL 未访问的会话（TTL<=0 则不做）。调用方须持锁。"""
        if self._session_ttl_s <= 0:
            return
        now = time.monotonic()
        for run_id in [
            rid for rid, ts in self._session_seen.items()
            if now - ts > self._session_ttl_s
        ]:
            self._evict(run_id)

    def _evict_overflow(self) -> None:
        """超过容量上限就淘汰队首（最久未访问）；`max_sessions<=0` = 不限。须持锁。"""
        if self._max_sessions <= 0:
            return
        while len(self._sessions) > self._max_sessions:
            run_id, sess = self._sessions.popitem(last=False)
            self._session_seen.pop(run_id, None)
            self._close_session(sess, run_id)

    def _evict(self, run_id: str) -> None:
        """按 run_id 淘汰一个会话（含关闭）。须持锁。"""
        sess = self._sessions.pop(run_id, None)
        if sess is not None:
            self._session_seen.pop(run_id, None)
            self._close_session(sess, run_id)

    def _close_session(self, sess: AgentSession, run_id: str) -> None:
        """关闭被淘汰的会话（释放它持有的资源）；关闭失败不得影响缓存淘汰。

        注意：会话持有的模型/存储/记忆是**共享**的，`AgentSession.close()` 只做标记；
        这里把“关闭”做成一个可覆盖的钩子（鸭子类型），方便扩展与测试。
        """
        close = getattr(sess, "close", None)
        if not callable(close):
            return
        try:
            close()
        except Exception:  # noqa: BLE001 - 关闭失败不能拖垮请求
            logger.warning("关闭被淘汰的会话失败 run=%s", run_id, exc_info=True)

    def get(self, run_id: str) -> AgentSession:
        """取会话；没有就基于数据库恢复/新建一个。"""
        with self._lock:
            self._evict_stale()
            sess = self._sessions.get(run_id)
            if sess is None:
                sess = AgentSession(
                    run_id=run_id,
                    model=self._model,
                    catalog=self._catalog,
                    policy_engine=self._policy,
                    store=self._store,
                    system_prompt=self._system_prompt,
                    stability=self._stability,
                    # 认知能力：与 AgentLoop 共用 loop/cognition.py 同一实现，
                    # 所以"记忆按需取用 / 阶段规划 / 意图路由 / 上下文裁剪"在产品路径也生效
                    planner=self._planner,
                    intent=self._intent,
                    memory=self.extra.get("memory_service"),
                    memory_scope=self.extra.get("memory_scope"),
                    max_context_chars=self._max_context_chars,
                    checkpoint_store=self._checkpoint_store,
                )
                self._sessions[run_id] = sess
            else:
                # 已有会话：按它的归属者解析模型（这也是 approve/reject 等驱动路径
                # 不需要各自再解析一次的原因）
                sess.model = self.model_for(sess.run.user_id or None)[1]
            # LRU：本次访问移到队尾；记下访问时刻供 TTL 淘汰；再按上限淘汰最久未访问的。
            self._sessions.move_to_end(run_id)
            self._session_seen[run_id] = time.monotonic()
            self._evict_overflow()
            return sess

    def remove(self, run_id: str) -> None:
        """把会话从内存下线（删除会话时用，数据库由调用方清理）。"""
        with self._lock:
            self._sessions.pop(run_id, None)
            self._session_seen.pop(run_id, None)
            self._created_by.pop(run_id, None)

    def run_ids(self) -> list[str]:
        """当前在内存里的会话 id 快照（遍历用；不要在调用期间持有锁）。"""
        with self._lock:
            return list(self._sessions.keys())

    def set_model(self, model: AgentChatModel, *, model_id: str = "",
                  owner: str | None = None) -> None:
        """切换模型。

        `owner=None` → 部署默认（所有会话）；给定时 → **只切该归属者的会话**，
        并把选择记在该用户名下。多用户部署里普通用户只能影响自己的会话。
        """
        with self._lock:
            if owner is None:
                self._model = model
                self._default_model_id = model_id
            else:
                self._owner_models[owner] = (model_id, model)
            for sess in self._sessions.values():
                if owner is None or sess.run.user_id == owner:
                    sess.model = model

    def model_for(self, owner: str | None = None) -> tuple[str, AgentChatModel]:
        """解析某归属者当前该用的 (model_id, model)：没选过就回落到部署默认。"""
        with self._lock:
            if owner:
                got = self._owner_models.get(owner)
                if got is not None:
                    return got
            return (self._default_model_id, self._model)

    def apply_owner_model(self, sess: AgentSession) -> None:
        """把会话的模型同步成"它的归属者选的那个"（首次绑定归属后调一次）。"""
        sess.model = self.model_for(sess.run.user_id or None)[1]


def _serialize_messages(messages: list[Message]) -> list[dict[str, Any]]:
    return [
        {
            "role": m.role,
            "content": m.content,
            "tool_call": (m.tool_call.to_dict() if m.tool_call else None),
        }
        for m in messages
    ]


def _checkpoint_store_for(store: Any) -> Any:
    """给具备 checkpoint 能力的存储套上 `CheckpointStore` 适配器（见 runtime 层实现）。"""
    from warden_agent.runtime.checkpoint import checkpoint_store_for

    return checkpoint_store_for(store)


def _plan_to_dict(plan: Any, owner: str | None, owner_of: Any) -> dict[str, Any]:
    """把 RecoveryPlan 转成 JSON 可序列化的字典，并按归属过滤（owner 给定时）。"""
    def visible(cp: Any) -> bool:
        return owner is None or owner_of(cp.run_id) == owner

    return {
        "decisions": {
            rid: d
            for rid, d in plan.decisions.items()
            if owner is None or owner_of(rid) == owner
        },
        "to_resume": [cp.to_dict() for cp in plan.to_resume if visible(cp)],
        "to_retry": [cp.to_dict() for cp in plan.to_retry if visible(cp)],
        "awaiting_human": [cp.to_dict() for cp in plan.awaiting_human if visible(cp)],
        "terminal": [cp.to_dict() for cp in plan.terminal if visible(cp)],
    }


def _model_key_name(model_id: str) -> str:
    """模型 API Key 在凭证 broker 里的登记名。"""
    return f"model:{model_id}"


def _credential_scope(request: Request) -> str:
    """凭证作用域 = 调用者身份。

    多租户下 `tenant_id` 是**整租户共用**的（`WARDEN_TENANT`，默认 `local`），
    用它做隔离会让同租户的 A、B 两用户互相读到对方导入的 key。所以这里用
    `user_id`（= principal_id，来自凭证本身）作为隔离维度；匿名开发模式
    （未开鉴权）回落到部署级作用域。
    """
    caller: TrustedCaller | None = getattr(request.state, "caller", None)
    return caller.user_id if caller is not None else DEPLOYMENT_SCOPE


def _has_model_key(broker: CredentialBroker, model_id: str, scope: str) -> bool:
    """某作用域（或部署级）是否已登记该模型的 key——部署级 key 全体可用。"""
    name = _model_key_name(model_id)
    return broker.has(name, scope) or (
        scope != DEPLOYMENT_SCOPE and broker.has(name, DEPLOYMENT_SCOPE)
    )


def _registered_model_key(
    broker: CredentialBroker, model_id: str, scope: str
) -> str | None:
    """从 broker 取回某模型已导入的 key（走短租约；未登记返回 None）。

    先看调用者自己的，再回落到部署级（启动时配的那把，全体共用）。
    """
    name = _model_key_name(model_id)
    for candidate in (scope, DEPLOYMENT_SCOPE):
        if not broker.has(name, candidate):
            continue
        lease = broker.issue(name, scope=candidate)
        return lease.value.fields.get("api_key")
    return None


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
    session_cache_max: int = DEFAULT_SESSION_CACHE_MAX,
    session_ttl_s: float = 0.0,
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
    from warden_agent.model.fake import FakeModel

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

    @app.get("/models")
    def models_view(request: Request) -> dict[str, Any]:
        """可用模型列表 + 当前使用的模型。

        `configured` 按调用者视角计算：自己导入过、或部署级配过，都算已配置。
        """
        scope = _credential_scope(request)
        owner = _identity(request, None)
        return {
            # 当前模型按**调用者自己的选择**报（没选过则显示部署默认）
            "current": registry.model_for(owner)[0] or current_model_id,
            "models": [
                {
                    "id": mid,
                    "name": info["name"],
                    "needs_key": info["needs_key"],
                    "configured": (not info["needs_key"])
                    or _has_model_key(broker, mid, scope),
                }
                for mid, info in model_catalog.items()
            ],
        }

    @app.post("/models/select")
    def models_select(body: ModelSelectIn, request: Request) -> dict[str, Any]:
        """切换模型；带 api_key 视为"导入"（凭凭证 broker 加密落库，重启后仍在）。

        导入的 key 记在**调用者自己的作用域**下：同租户的其他用户读不到、用不了；
        部署级（启动配置）的那把则全体可见。

        **切换只作用于调用者自己**：否则任一认证用户就能把所有人的对话切成离线假模型
        （跨租户 DoS），或切到用自己的 key 计费（把别人的额度记到自己头上）。
        """
        scope = _credential_scope(request)
        info = model_catalog.get(body.id)
        if info is None:
            raise HTTPException(status_code=404, detail=f"未知模型: {body.id}")
        key = body.api_key or _registered_model_key(broker, body.id, scope)
        if info["needs_key"] and not key:
            raise HTTPException(
                status_code=400, detail=f"{info['name']} 需要先导入 API Key"
            )
        owner = _identity(request, None)
        caller: TrustedCaller | None = getattr(request.state, "caller", None)
        if body.scope not in ("self", "deployment"):
            raise HTTPException(status_code=400, detail="scope 只能是 self 或 deployment")
        if body.scope == "deployment":
            # 切部署默认模型影响**所有人**（包括没自选过的会话）——这是运维动作，
            # 只有管理员能做。普通用户仍可切"自己的"（scope=self，默认）。
            if caller is None or not caller.is_admin:
                raise HTTPException(
                    status_code=403,
                    detail="切换部署级默认模型需要管理员角色（普通用户可用 scope=self 切自己的）",
                )
            # 部署默认 = owner=None（所有会话，且成为新会话的默认）
            registry.set_model(info["make"](key), model_id=body.id, owner=None)
        else:
            registry.set_model(info["make"](key), model_id=body.id, owner=owner)
        if body.api_key:
            broker.register(_model_key_name(body.id), {"api_key": body.api_key}, scope)
            redactor.add(body.api_key)
        return {"ok": True, "current": registry.model_for(owner)[0] or body.id,
                "scope": body.scope}

    # ---- T8 可观测性：指标定义（全局注册表，Prometheus 文本输出）----
    m = metrics()
    m_http = m.counter("warden_http_requests_total", "HTTP 请求总数", ["method", "path"])
    m_http_errors = m.counter("warden_http_errors_total", "HTTP 5xx 错误数", ["method", "path"])
    m_http_latency = m.histogram(
        "warden_http_request_duration_seconds",
        "HTTP 请求耗时(秒)",
        [0.01, 0.05, 0.1, 0.5, 1.0],
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
    authorizer = (
        RunOperationAuthorizer(
            combine_authorizers(permission_authorizer(), owner_authorizer(_owner_of))
        )
        if authenticator is not None
        else RunOperationAuthorizer()
    )

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
            # 只对带 body 的方法判定；先看 Content-Length（廉价），没有（chunked）就边读边计数——
            # 否则一个不声明长度的客户端能把任意大的 body 灌进来。读完（未超限）后把已读字节
            # 挂回 `request._body`：Starlette 的 BaseHTTPMiddleware 会把它透给下游，
            # 下游照常能读到 body（口径与 request.body() 一致，但内存被上限封顶）。
            if max_request_bytes > 0 and method in ("POST", "PUT", "PATCH"):
                declared = request.headers.get("content-length")
                too_big = (
                    declared is not None
                    and declared.isdigit()
                    and int(declared) > max_request_bytes
                )
                if too_big:
                    status_code = 413
                    return _problem(
                        413, "PAYLOAD_TOO_LARGE",
                        f"请求体超过上限 {max_request_bytes} 字节",
                        correlation_id,
                    )
                if declared is None:
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

    # ---- 阶段13：健康检查（liveness / readiness）----
    @app.get("/health/live", include_in_schema=False)
    def health_live() -> dict[str, Any]:
        r = liveness()
        return {"status": r.status, "checks": r.checks}

    @app.get("/health/ready", include_in_schema=False)
    def health_ready() -> JSONResponse:
        r: HealthResult = readiness(store)
        checks = dict(r.checks)
        status = r.status
        if audit_unkeyed:
            # 审计开着却没有链密钥：能发现手改/删行，却挡不住会重算整条链的人。
            # 就绪探针如实降级，让编排/监控能看见，而不是等出事才发现“以为在防篡改”。
            checks["audit_chain"] = "unkeyed"
            status = "degraded"
        return JSONResponse(
            status_code=200 if status == "ok" else 503,
            content={"status": status, "checks": checks},
        )

    # ---- T8 可观测性：指标出口（Prometheus text，可被 Grafana 抓取）----
    # 挂起数的缓存：抓取时"现算"能保证数是对的（不漂移），但每次抓取都扫库在抓取密集时
    # 是浪费。这里加一个短 TTL 缓存：TTL 内复用上次结果，TTL 外重新扫描。
    # 取 15s 与 Prometheus 默认抓取间隔一致——**不会让"挂起数"比告警评估周期更旧**。
    _stuck_cache: dict[str, float] = {"at": -1e9, "value": 0.0}
    _STUCK_TTL_S = 15.0

    def _refresh_stuck_gauge() -> None:
        """把"挂太久的 Run 数"写进 gauge（带 TTL 缓存）。失败绝不让 /metrics 挂掉。

        为什么现算而不是在写入路径累加：累加式 gauge 在重启、多副本下都会漂移
        （每个副本只知道自己见过的那部分），而告警恰恰最怕"数不对"。抓取时对存储做一次
        只读扫描得到的是**当前真实值**；再加 TTL 缓存，避免"抓取越频繁、扫得越多"。

        ⚠️ 刷新失败时**保留上一次的值**，绝不写 0：写 0 等于把告警悄悄消掉
        （`warden_stuck_runs > 0` 立刻变假），那是比"指标空缺"严重得多的错。
        """
        now = time.monotonic()
        if now - _stuck_cache["at"] < _STUCK_TTL_S:
            m_stuck.set(_stuck_cache["value"], labels=("60m",))   # 缓存命中：不扫库
            return
        try:
            from warden_agent.runtime.alerting import stuck_awaiting_human

            cp_store = _checkpoint_store_for(store)
            if cp_store is None:
                return
            stuck = stuck_awaiting_human(store, cp_store, older_than_seconds=3600.0)
            value = float(len(stuck))
        except Exception:  # noqa: BLE001 - 指标刷新失败不能影响 /metrics 本身
            logger.debug("刷新 warden_stuck_runs 失败（保留上次值，不影响 /metrics）",
                         exc_info=True)
            return
        _stuck_cache["at"] = now
        _stuck_cache["value"] = value
        m_stuck.set(value, labels=("60m",))

    @app.get("/metrics", include_in_schema=False)
    def metrics_view(request: Request) -> PlainTextResponse:
        """Prometheus 指标出口。**仅管理员**可读。

        为什么收紧：指标是**运维面**（全局请求量/错误率/挂起数/各 path 延时），不是用户数据面。
        此前只要求认证，viewer（只读角色）也能读——与"只读=能看用户数据但不能动"的契约不符。
        本地匿名开发模式（未开启鉴权）保持开放，行为不变。
        """
        caller: TrustedCaller | None = getattr(request.state, "caller", None)
        if authenticator is not None and (caller is None or not caller.is_admin):
            raise HTTPException(status_code=403, detail="查看指标需要管理员角色")
        _refresh_stuck_gauge()
        return PlainTextResponse(metrics().render())

    # ---- 阶段13：审计查询 ----
    @app.get("/audit")
    def audit_view(request: Request) -> list[dict[str, Any]]:
        """返回最近的审计轨迹（含 correlation_id / 调用者 / 操作 / 结果状态）。

        认证模式下只返回**调用者自己**的记录。为什么是"按人"而不是"按租户"：
        本项目的多用户配置（`WARDEN_API_KEYS`）默认共用同一个 `WARDEN_TENANT`（默认 `local`），
        即**租户 ≠ 用户**——只按 tenant 过滤，等于把同租户其他人的操作账本（含 run_id、
        身份、访问路径）交给任意一个认证用户，既能窥探又能枚举账号。
        这里与 `/runs`、`/approvals` 保持同一口径：按调用者收敛。
        **管理员例外**：看租户全部（运维需要全局视图来判断"是不是有人在乱用"）。
        """
        if audit is None:
            raise HTTPException(status_code=404, detail="未开启审计(audit_store=None)")
        caller: TrustedCaller | None = getattr(request.state, "caller", None)
        tenant = caller.tenant_id if caller is not None else None
        # 管理员看租户全部；普通用户只看自己（见 _owner_scope）
        principal = _owner_scope(request)
        records = audit_store.query(  # type: ignore[union-attr]
            limit=200, tenant_id=tenant, principal_id=principal
        )
        return [r.to_dict() for r in records]

    # ---- 跨 Run 恢复：崩溃/重启后"哪些该续、哪些该重试、哪些该等人" ----
    @app.get("/recovery/plan")
    def recovery_plan(request: Request) -> dict[str, Any]:
        """读取全部存档点，产出恢复计划（只读判断，不执行任何动作）。

        用途：进程崩溃/重启后，工作进程或运维据此决定续跑/重试/等待人工，
        而不是"全部从头再跑一遍"。`RecoveryController` 本身只判断不执行，
        真正的执行由调用方负责。认证模式下只暴露调用者自己名下的 Run。
        """
        from warden_agent.runtime.recovery import RecoveryController

        cp_store = _checkpoint_store_for(store)
        if cp_store is None:
            raise HTTPException(status_code=501, detail="当前存储不支持存档点")
        plan = RecoveryController(cp_store).plan()
        owner = _owner_scope(request)   # 管理员 → None（全局视图）
        return _plan_to_dict(plan, owner, _owner_of)

    @app.get("/alerts/stuck")
    def alerts_stuck(request: Request, older_than_min: float = 60.0) -> dict[str, Any]:
        """**等待人工处理超时**的 Run —— 给监控/告警用。

        为什么需要它：Run 进入 `WAITING_APPROVAL` 之后不会有任何动静（没有超时、没有重试、
        没有通知），线上没人盯就一直挂着。这个端点把"挂太久了"变成可被定时抓取的事实：
        返回非空列表就该有人去看。认证模式下只暴露调用者自己名下的 Run。

        ⚠️ 口径："等了多久"用 Run 的最后活动时间近似（不是"进入等待那一刻"），所以只会**低估**、
        不会虚报——"报了警"是可信的；"没报警"不等于一定没挂久。
        **管理员**看全部（值班人要知道"整个系统有没有人卡着"）。
        """
        from warden_agent.runtime.alerting import stuck_awaiting_human

        cp_store = _checkpoint_store_for(store)
        if cp_store is None:
            raise HTTPException(status_code=501, detail="当前存储不支持存档点")
        owner = _owner_scope(request)   # 管理员 → None（全局视图）
        stuck = stuck_awaiting_human(
            store, cp_store, older_than_seconds=older_than_min * 60.0, owner=owner
        )
        return {
            "older_than_min": older_than_min,
            "count": len(stuck),
            "runs": [
                {
                    "run_id": s.run_id,
                    "status": s.status,
                    "waiting_seconds": s.waiting_seconds,
                    "detail": s.detail,
                    # 时长算自哪里：approval=精确（进入等待审批的时刻）；
                    # last_activity=近似（只会低估）；unknown=拿不到
                    "source": s.source,
                }
                for s in stuck
            ],
        }

    # 首页：返回可视化演示控制台（HTML），让服务"看得见"。
    # T10 起优先返回 React 构建产物（web/dist）；若未构建则回退到旧版静态 index.html。
    _dist_html: str | None = None

    def _repo_dir() -> str:
        # server.py 位于 <repo>/src/warden_agent/web/server.py，向上 3 层回到仓库根
        import os

        here = os.path.dirname(os.path.abspath(__file__))  # .../warden_agent/web
        return os.path.abspath(os.path.join(here, "..", "..", ".."))  # <repo>

    def _web_dist_dir() -> str:
        import os

        dist = env_opt("WARDEN_WEB_DIST")
        if dist:
            return dist
        return os.path.join(_repo_dir(), "web", "dist")

    def _find_spa_index() -> str | None:
        # 顺序找构建产物：环境变量指定的路径 > 仓库内 web/dist
        p = os.path.join(_web_dist_dir(), "index.html")
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return f.read()
            except OSError:
                return None
        return None

    @app.get("/", include_in_schema=False)
    def index() -> HTMLResponse:
        html = _find_spa_index()
        if html is None:  # 未构建 React 前端，回退到旧演示控制台
            try:
                from importlib.resources import files

                html = files("warden_agent.web.static").joinpath("index.html").read_text("utf-8")
            except Exception:  # 找不到模板时给个简单占位，不至于 500
                html = "<h1>Warden Agent</h1><p>未找到演示页面。</p>"
        return HTMLResponse(html)

    @app.post("/chat/{run_id}")
    def chat(
        run_id: str, body: ChatRequestIn, request: Request, user_id: str = "demo-user"
    ) -> ChatResponseOut:
        lease = _acquire_run(run_id)
        try:
            sess = registry.get(run_id)
            if not sess.run.user_id:
                # 首轮对话建立归属：认证模式下身份来自凭证（user_id 参数被忽略）
                sess.run.user_id = _identity(request, user_id)
                # 归属刚定下来 → 把模型同步成"这个用户选的那个"（没选过就是部署默认）
                registry.apply_owner_model(sess)
            try:
                outcome = sess.start(body.text)
            except Exception as e:  # 工具未注册 / 被 DENY 等
                logger.exception("chat 失败 run=%s", run_id)
                bus.publish(run_id, {"event": "error", "message": str(e)})
                raise HTTPException(status_code=400, detail=str(e)) from e

            if isinstance(outcome, FinalReply):
                bus.publish(run_id, {"event": "final", "text": outcome.text})
                return ChatResponseOut(
                    run_id=run_id,
                    status=sess.status().name,
                    kind="final",
                    text=outcome.text,
                    messages=_serialize_messages(outcome.messages),
                )
            if isinstance(outcome, NeedsApproval):
                bus.publish(
                    run_id,
                    {"event": "needs_approval", "approval": outcome.approval.tool_name},
                )
                return ChatResponseOut(
                    run_id=run_id,
                    status=sess.status().name,
                    kind="needs_approval",
                    approval={
                        "approval_id": outcome.approval.approval_id,
                        "tool_name": outcome.approval.tool_name,
                        "arguments": outcome.approval.arguments,
                        "reason": outcome.approval.reason,
                    },
                )
            raise HTTPException(status_code=500, detail="未知结果类型")
        finally:
            lease.stop()

    @app.get("/status/{run_id}")
    def status(run_id: str) -> dict[str, Any]:
        sess = registry.get(run_id)
        return {"run_id": run_id, "status": sess.status().name}

    @app.get("/runs")
    def runs(request: Request, user_id: str = "") -> list[dict[str, Any]]:
        """对话列表：最近活跃的会话。

        认证模式下只返回**调用者自己**的会话（身份来自凭证，`user_id` 参数被忽略）；
        匿名开发模式下按 `user_id` 过滤，不传则返回全部。
        """
        caller: TrustedCaller | None = getattr(request.state, "caller", None)
        owner = caller.user_id if caller is not None else (user_id or None)
        return store.list_runs(limit=50, owner=owner)

    @app.post("/runs/{run_id}")
    def create_run(
        run_id: str, request: Request, user_id: str = "demo-user"
    ) -> dict[str, Any]:
        """预创建会话：归属当前身份，立即可见于该用户的对话列表。幂等。

        已存在的会话不改变归属（归属由已认证凭证锁定，不能被后续请求改写）。
        """
        identity = _identity(request, user_id)
        store.create_user(identity)
        sess = registry.get(run_id)
        if store.load_run(run_id) is None:
            sess.run.user_id = identity
            store.save_run(sess.run)
        return {
            "run_id": run_id,
            "status": sess.status().name,
            "user_id": sess.run.user_id,
        }

    @app.get("/users")
    def users_view(request: Request) -> list[dict[str, Any]]:
        """已登记的中控台用户。

        认证模式下只返回调用者自己——否则等于开放了一个"枚举他人账号"的接口。
        """
        caller: TrustedCaller | None = getattr(request.state, "caller", None)
        if caller is not None:
            return [u for u in store.list_users() if u["user_id"] == caller.user_id]
        return store.list_users()

    @app.post("/users")
    def users_create(body: UserCreateIn, request: Request) -> dict[str, Any]:
        """登记用户（幂等：已存在则不动）。

        认证模式下只能登记调用者自己，不能替别人建档。
        """
        uid = body.user_id.strip()
        if not uid:
            raise HTTPException(status_code=400, detail="user_id 不能为空")
        caller: TrustedCaller | None = getattr(request.state, "caller", None)
        if caller is not None and uid != caller.user_id:
            raise HTTPException(status_code=403, detail="无权为其他账号建档")
        store.create_user(uid)
        return {"ok": True, "user_id": uid}

    @app.delete("/runs/{run_id}")
    def delete_run(run_id: str) -> dict[str, Any]:
        """删除会话：数据库（状态/对话/待审批/checkpoint）清空，内存会话下线。"""
        registry.remove(run_id)
        store.delete_run(run_id)
        return {"ok": True, "run_id": run_id}

    @app.get("/messages/{run_id}")
    def messages(run_id: str) -> list[dict[str, Any]]:
        """返回某会话的完整对话记录（前端刷新后恢复聊天区用）。"""
        sess = registry.get(run_id)
        return _serialize_messages(sess.messages)

    @app.get("/approvals")
    def approvals(request: Request) -> list[dict[str, Any]]:
        """列出"等待审批"的会话（审批队列）。

        认证模式下只列**调用者自己**名下的会话——审批队列是人工闸门入口，
        跨租户可见会让别人看到甚至替你决策高危操作。
        """
        caller: TrustedCaller | None = getattr(request.state, "caller", None)
        result = []
        for run_id in registry.run_ids():
            sess = registry.get(run_id)
            if caller is not None and sess.run.user_id != caller.user_id:
                continue
            pending = sess.pending_approval()
            if pending is not None:
                result.append(
                    {
                        "run_id": run_id,
                        "approval_id": pending.approval_id,
                        "tool_name": pending.tool_name,
                        "arguments": pending.arguments,
                        "reason": pending.reason,
                    }
                )
        return result

    @app.get("/approvals/history")
    def approvals_history(request: Request) -> list[dict[str, Any]]:
        """审批决策历史（已批准 / 已拒绝，最新的在前）。

        认证模式下只返回调用者名下 Run 的决策（审批历史含工具名与参数，属租户数据）；
        **管理员**看全部（与 /audit 同一口径）。
        """
        owner = _owner_scope(request)   # 管理员 → None（全局视图）
        return store.list_approval_history(limit=20, owner=owner)

    @app.post("/approve/{run_id}")
    def approve(run_id: str) -> ChatResponseOut:
        lease = _acquire_run(run_id)
        try:
            sess = registry.get(run_id)
            pending = sess.pending_approval()
            if pending is None:
                raise HTTPException(status_code=409, detail="该会话没有等待审批的请求")
            m_approvals.inc(labels=("approve",))
            outcome = sess.approve()
            store.record_approval_decision(
                run_id, pending.approval_id, pending.tool_name,
                pending.arguments, "approved",
            )
            return _outcome_response(run_id, sess, outcome)
        finally:
            lease.stop()

    @app.post("/reject/{run_id}")
    def reject(run_id: str) -> ChatResponseOut:
        lease = _acquire_run(run_id)
        try:
            sess = registry.get(run_id)
            pending = sess.pending_approval()
            if pending is None:
                raise HTTPException(status_code=409, detail="该会话没有等待审批的请求")
            m_approvals.inc(labels=("reject",))
            outcome = sess.reject()
            store.record_approval_decision(
                run_id, pending.approval_id, pending.tool_name,
                pending.arguments, "rejected",
            )
            return _outcome_response(run_id, sess, outcome)
        finally:
            lease.stop()

    def _outcome_response(
        run_id: str, sess: AgentSession, outcome: FinalReply | NeedsApproval
    ) -> ChatResponseOut:
        """把会话结果（最终回答 / 又遇到审批）转成响应。approve 与 reject 共用。"""
        if isinstance(outcome, FinalReply):
            bus.publish(run_id, {"event": "final", "text": outcome.text})
            return ChatResponseOut(
                run_id=run_id,
                status=sess.status().name,
                kind="final",
                text=outcome.text,
                messages=_serialize_messages(outcome.messages),
            )
        if isinstance(outcome, NeedsApproval):
            # 与 /chat 路径对齐：又遇到审批也要发事件。此前 approve/reject 这条路
            # 拿到 NeedsApproval 只回响应、不发事件，/events 订阅方永远等不到它。
            bus.publish(
                run_id,
                {"event": "needs_approval", "approval": outcome.approval.tool_name},
            )
            return ChatResponseOut(
                run_id=run_id,
                status=sess.status().name,
                kind="needs_approval",
                approval={
                    "approval_id": outcome.approval.approval_id,
                    "tool_name": outcome.approval.tool_name,
                    "arguments": outcome.approval.arguments,
                    "reason": outcome.approval.reason,
                },
            )
        raise HTTPException(status_code=500, detail="未知结果类型")

    @app.post("/chat/stream/{run_id}")
    async def chat_stream(
        request: Request, run_id: str, body: ChatRequestIn, user_id: str = "demo-user"
    ) -> StreamingResponse:
        """流式对话（SSE 打字机）：模型边生成边把增量推给前端。
        前端拿到增量直接渲染，就能看到"逐字打出"的效果。"""
        # 并发闸门：到上限立刻 503，不排队（排队只会让一堆请求一起超时）。
        if not sse_gate.acquire():
            raise HTTPException(
                status_code=503,
                detail=(
                    f"SSE 长连接已达并发上限 {sse_max_concurrency}，请稍后重试"
                    "（WARDEN_SSE_MAX_CONNECTIONS 可调）"
                ),
            )
        # 流式用**手动 start/stop** 的租约：锁在端点里取（抢不到就地 423），
        # 但必须活到 SSE 流结束——所以释放放在生成器的 finally 里，而不是端点作用域。
        # 租约自带心跳续租，所以"流很久"也不会中途过期被接管。
        try:
            lease = _acquire_run(run_id)
            try:
                sess = registry.get(run_id)
                if not sess.run.user_id:
                    # 归属只能用 `_identity` 派生（认证模式下来自凭证，忽略查询参数），
                    # 与非流式 `/chat` 一致——否则客户端能用 `?user_id=alice` 把消息写进别人名下。
                    sess.run.user_id = _identity(request, user_id)
                    registry.apply_owner_model(sess)  # 用该用户自己选的模型
            except Exception:
                # 建流**之前**出错也必须释放租约：租约带后台心跳续租，漏掉的话这个 run
                # 会被永久占住（一直回 423），直到进程重启。流内的释放见 generate() 的 finally。
                lease.stop()
                raise
        except Exception:
            # 建流前任何失败（含上面抢不到 Run 锁的 423）都要把并发名额还回去。
            sse_gate.release()
            raise

        def generate() -> Any:
            try:
                for event in sess.stream(body.text):
                    # SSE 格式：每条事件以 "data: <json>\n\n" 结尾
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            except Exception as e:  # DENY / 工具错误等，以 error 事件结束
                logger.exception("流式 chat 失败 run=%s", run_id)
                err = {"type": "error", "message": str(e)}
                yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
            finally:
                # 流结束（含客户端断开触发 GeneratorExit）才释放：
                # 整段流式期间都在驱动这个 run，提前释放等于开门让人并发写。
                # 并发名额同生共死：与租约一起在这里还回去。
                lease.stop()
                sse_gate.release()

        # 关键响应头：no-cache 防止代理缓冲；X-Accel-Buffering 关掉 nginx 缓冲，
        # 否则增量会被攒住不实时发出（部署必配）。
        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @app.get("/events/{run_id}")
    async def events(run_id: str) -> StreamingResponse:
        """SSE：监听某会话的事件（最终结果 / 审批请求 / 错误）。

        走可插拔事件总线：单副本是进程内实现（条件变量唤醒、无延迟），
        多副本换成存储实现（多个副本订阅同一张事件表），客户端连任一副本都能收到。
        """
        # 与 /chat/stream 共用同一并发闸门：否则只限 /chat/stream 是形同虚设——
        # 用 /events 一样能无限量开长连接把连接池占满。名额在生成器 finally 里释放
        # （含客户端断开触发的 GeneratorExit）。
        if not sse_gate.acquire():
            raise HTTPException(
                status_code=503,
                detail=(
                    f"SSE 长连接已达并发上限 {sse_max_concurrency}，请稍后重试"
                    "（WARDEN_SSE_MAX_CONNECTIONS 可调）"
                ),
            )

        def generate() -> Any:
            try:
                seq = 0
                while True:
                    items = bus.poll(run_id, seq, timeout=15.0)
                    if not items:
                        yield ": keep-alive\n\n"  # 心跳，防中间层掐连接
                        continue
                    stop = False
                    for s, ev in items:
                        seq = s
                        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                        if ev.get("event") in ("final", "error"):
                            stop = True
                    if stop:
                        break
            finally:
                # 流结束（含客户端断开）就归还并发名额，防泄漏
                sse_gate.release()

        return StreamingResponse(generate(), media_type="text/event-stream")

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
                # RAG：报出嵌入器名，避免"词频嵌入被当成语义检索"
                "knowledge_embedder": registry.extra.get("knowledge_embedder"),
                "knowledge_sources": len(knowledge_sources) if knowledge_sources else 0,
                # Run 锁用的是哪种实现：进程内锁在多副本下等于没锁，运维必须能一眼看到
                "run_lock": type(_run_lock).__name__,
            },
        }

    @app.get("/memory/{scope}")
    def memory_view(request: Request, scope: str) -> list[dict[str, Any]]:
        """查看某作用域（run/session/user/workspace）下**调用者自己**的记忆。

        归属边界：记忆按 `owner`（用户 id）严格隔离——认证模式下只看得到自己写的，
        否则任一用户就能读到全部署所有人的记忆（跨租户泄露），甚至无从发现被投毒。
        """
        mem = registry.extra.get("memory_service")
        if mem is None:
            raise HTTPException(status_code=404, detail="未启用记忆(memory=True)")
        from warden_agent.memory import MemoryScope

        try:
            enum_scope = MemoryScope[scope.upper()]
        except KeyError:
            raise HTTPException(status_code=400, detail=f"未知作用域: {scope}") from None
        owner = _identity(request, None)   # 身份来自凭证，不接受查询参数自称
        return [
            {"scope": i.scope.name, "key": i.key, "text": i.content.text,
             "status": i.status.name, "owner": i.owner}
            for i in mem.recall(enum_scope, limit=100, owner=owner)
        ]

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

    return app
