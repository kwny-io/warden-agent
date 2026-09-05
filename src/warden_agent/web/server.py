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

import json
import logging
import queue
import threading
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from warden_agent.core.metrics import metrics
from warden_agent.model.model import AgentChatModel, Message
from warden_agent.policy.policy import PolicyEngine
from warden_agent.runtime.session import AgentSession, FinalReply, NeedsApproval
from warden_agent.store.sqlite import SqliteStore
from warden_agent.tool.catalog import ToolCatalog
from warden_agent.web.audit import AuditLogger, AuditStore
from warden_agent.web.auth import (
    ApiKeyAuthenticator,
    HttpAuthenticationError,
    HttpAuthorizationError,
    RunOperationAuthorizer,
    TrustedCaller,
    operation_for,
)
from warden_agent.web.health import HealthResult, liveness, readiness

logger = logging.getLogger(__name__)

# ---- HTTP contract：统一 API 版本（所有响应都带这个头，客户端可据此协商）----
API_VERSION = "1.0"


def _cache_idem_response(
    store: dict[str, Any], key: str, response: Any
) -> None:
    """把响应缓存进幂等表。

    注意：本函数不消费 body 流——因为中间件里拿到 response 时 body 尚未被消费，
    但流只能消费一次。这里只把"可缓存"的信息结构记下，真正的 body 读取同步在
    _gateway 里用 async for 完成并重建 response。
    """
    store[key] = {
        "status_code": response.status_code,
        "headers": dict(response.headers),
        # body 由调用方（_gateway）填充
        "body": None,
    }


async def _drain_and_rebuild(
    response: Any, store: dict[str, Any], key: str
) -> Any:
    """消费 response 的 body 流，缓存进幂等表，返回一个可重放的新 Response。"""
    body_bytes = b"".join([chunk async for chunk in response.body_iterator])
    item = store.get(key) or {}
    item["body"] = body_bytes
    store[key] = item

    headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
    return JSONResponse(
        status_code=response.status_code,
        content=json.loads(body_bytes) if body_bytes else None,
        headers=headers,
    )


def _idem_response_from_store(store: dict[str, Any], key: str) -> Any | None:
    item = store.get(key)
    if not item:
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
# 统一错误码契约（对标 RuntimeApiErrorCode）：所有业务错误都用这里的 code + status
API_ERROR_CODES = {
    "BAD_REQUEST": 400,
    "AUTHENTICATION_REQUIRED": 401,
    "AUTHORIZATION_DENIED": 403,
    "RUN_INVALID_STATE": 409,
    "NOT_FOUND": 404,
    "CONFLICT": 409,
    "SERVICE_UNAVAILABLE": 503,
    "INTERNAL_ERROR": 500,
}
_API_TITLES: dict[int, str] = {
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    409: "Conflict",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


def _problem(status: int, code: str, detail: str, correlation_id: str) -> JSONResponse:
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
        },
    )


def _extract_run_id(path: str) -> str | None:
    """从请求路径里挖出 run_id（用于授权与审计），挖不到返回 None。"""
    segments = path.rstrip("/").split("/")
    if len(segments) >= 3:
        # /chat/stream/{run_id} → ["", "chat", "stream", id]
        if segments[1] == "chat" and len(segments) == 4 and segments[2] == "stream":
            return segments[3]
        if segments[1] in ("chat", "status", "approve", "reject", "events"):
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
    ) -> None:
        self._model = model
        self._catalog = catalog
        self._policy = policy
        self._store = store
        self._system_prompt = system_prompt
        self.extra = extra or {}  # 额外能力（如 memory_service / skill_catalog）
        self._sessions: dict[str, AgentSession] = {}
        self._lock = threading.Lock()

    def get(self, run_id: str) -> AgentSession:
        """取会话；没有就基于数据库恢复/新建一个。"""
        with self._lock:
            sess = self._sessions.get(run_id)
            if sess is None:
                sess = AgentSession(
                    run_id=run_id,
                    model=self._model,
                    catalog=self._catalog,
                    policy_engine=self._policy,
                    store=self._store,
                    system_prompt=self._system_prompt,
                )
                self._sessions[run_id] = sess
            return sess


def _serialize_messages(messages: list[Message]) -> list[dict[str, Any]]:
    return [
        {
            "role": m.role,
            "content": m.content,
            "tool_call": (m.tool_call.to_dict() if m.tool_call else None),
        }
        for m in messages
    ]


def build_app(
    model: AgentChatModel,
    catalog: ToolCatalog,
    policy: PolicyEngine,
    store: SqliteStore,
    system_prompt: str = "你是一个能使用工具的助手。",
    memory: bool = False,
    skills: dict[str, str] | str | None = None,
    web: bool = False,
    mcp_server: str | None = None,
    git_workdir: str | None = None,
    *,
    api_keys: Mapping[str, TrustedCaller] | None = None,
    audit_store: AuditStore | None = None,
) -> FastAPI:
    """构建 FastAPI 应用。工厂方式便于测试注入假实现。

    memory/skills/web/mcp_server：让 Agent 在 HTTP 服务里也能用这些能力（复用
    agent.augment_catalog，把对应工具注册进目录）。

    阶段13 新增（产品级 HTTP 服务）：
      api_keys    ：{API Key: TrustedCaller}。非空则开启认证，业务接口都要带
                    `Authorization: Bearer <key>`；None=本地开发开放（不鉴权）。
      audit_store ：审计后端（AuditStore 实现）。给出则开启审计，每请求记一条；
                    None=不落审计。
    """
    from warden_agent.agent import augment_catalog

    extra = augment_catalog(
        catalog,
        memory=memory,
        skills=skills,
        web=web,
        mcp_server=mcp_server,
        git_workdir=git_workdir,
    )
    registry = SessionRegistry(model, catalog, policy, store, system_prompt, extra)
    app = FastAPI(title="Warden Agent Python", version=API_VERSION)

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

    # ---- 阶段13：认证 + 审计中间件 ----
    authenticator = ApiKeyAuthenticator(api_keys) if api_keys else None
    authorizer = RunOperationAuthorizer()
    audit = AuditLogger(audit_store) if audit_store is not None else None
    # 幂等表：Idempotency-Key → 缓存的响应（进程内即可，重启清空可接受）
    idem_store: dict[str, Any] = {}
    # 中间件闭包里带"当前是否开启"标志，`_is_public`/`_extract_run_id` 复用在端点里
    audit_enabled = audit is not None

    @app.middleware("http")
    async def _gateway(request: Request, call_next: Any) -> Any:
        """统一入口：分配 correlation_id → 认证 → 授权 → 执行业务 → 落审计 + 记指标。"""
        correlation_id = request.headers.get("X-Correlation-Id") or uuid.uuid4().hex
        method = request.method
        path = request.url.path
        run_id = _extract_run_id(path)
        operation = operation_for(method, path)
        caller: TrustedCaller | None = None
        status_code = 200
        _start = time.monotonic()  # T8：请求开始计时
        try:
            if authenticator is not None and not _is_public(path):
                try:
                    caller = authenticator.authenticate(request)
                except HttpAuthenticationError as e:
                    return _problem(401, "AUTHENTICATION_REQUIRED", str(e), correlation_id)
                if caller is not None:
                    try:
                        authorizer.authorize(caller, operation, run_id)
                    except HttpAuthorizationError as e:
                        return _problem(403, "AUTHORIZATION_DENIED", str(e), correlation_id)
            # 幂等：带 Idempotency-Key 的 POST，同 key 重复请求返回同一结果。
            # 流式端点(SSE)不参与幂等缓存（消费流会破坏它）。
            idem_key = request.headers.get("Idempotency-Key")
            is_stream = path.startswith("/events") or path.startswith("/chat/stream")
            if idem_key and method == "POST" and not is_stream:
                cached_resp = _idem_response_from_store(idem_store, idem_key)
                if cached_resp is not None:
                    return cached_resp
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Correlation-Id"] = correlation_id
            response.headers["X-Warden-Api-Version"] = API_VERSION
            if idem_key and method == "POST" and not is_stream and status_code < 500:
                # 仅缓存成功结果；5xx 不缓存以便重试。消费流并重建可重放响应。
                _cache_idem_response(idem_store, idem_key, response)
                response = await _drain_and_rebuild(response, idem_store, idem_key)
            return response
        except Exception:  # noqa: BLE001 - 网关兜底，不泄漏内部细节
            logger.exception("网关异常 method=%s path=%s", method, path)
            return _problem(500, "INTERNAL_ERROR", "请求未能完成", correlation_id)
        finally:
            # T8 指标：请求数 + 耗时分布 + 5xx 错误数（耗时直方图：桶已在注册时绑定）
            m_http.inc(labels=(method, path))
            m_http_latency.observe(time.monotonic() - _start)
            if status_code >= 500:
                m_http_errors.inc(labels=(method, path))
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

    # ---- 阶段13：健康检查（liveness / readiness）----
    @app.get("/health/live", include_in_schema=False)
    def health_live() -> dict[str, Any]:
        r = liveness()
        return {"status": r.status, "checks": r.checks}

    @app.get("/health/ready", include_in_schema=False)
    def health_ready() -> JSONResponse:
        r: HealthResult = readiness(store)
        return JSONResponse(
            status_code=200 if r.status == "ok" else 503,
            content={"status": r.status, "checks": r.checks},
        )

    # ---- T8 可观测性：指标出口（Prometheus text，可被 Grafana 抓取）----
    @app.get("/metrics", include_in_schema=False)
    def metrics_view() -> PlainTextResponse:
        return PlainTextResponse(metrics().render())

    # ---- 阶段13：审计查询 ----
    @app.get("/audit")
    def audit_view() -> list[dict[str, Any]]:
        """返回最近的审计轨迹（含 correlation_id / 调用者 / 操作 / 结果状态）。"""
        if audit is None:
            raise HTTPException(status_code=404, detail="未开启审计(audit_store=None)")
        records = audit_store.query(limit=200)  # type: ignore[union-attr]
        return [r.to_dict() for r in records]

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

        if os.environ.get("WARDEN_WEB_DIST"):
            return os.environ["WARDEN_WEB_DIST"]
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

    # 事件总线（SSE）：run_id -> Queue，状态变化时广播
    event_buses: dict[str, queue.Queue[dict[str, Any]]] = {}
    buses_lock = threading.Lock()

    def _bus(run_id: str) -> queue.Queue[dict[str, Any]]:
        with buses_lock:
            q = event_buses.get(run_id)
            if q is None:
                q = queue.Queue()
                event_buses[run_id] = q
            return q

    @app.post("/chat/{run_id}")
    def chat(run_id: str, body: ChatRequestIn) -> ChatResponseOut:
        sess = registry.get(run_id)
        try:
            outcome = sess.start(body.text)
        except Exception as e:  # 工具未注册 / 被 DENY 等
            logger.exception("chat 失败 run=%s", run_id)
            _bus(run_id).put({"event": "error", "message": str(e)})
            raise HTTPException(status_code=400, detail=str(e)) from e

        if isinstance(outcome, FinalReply):
            _bus(run_id).put({"event": "final", "text": outcome.text})
            return ChatResponseOut(
                run_id=run_id,
                status=sess.status().name,
                kind="final",
                text=outcome.text,
                messages=_serialize_messages(outcome.messages),
            )
        if isinstance(outcome, NeedsApproval):
            _bus(run_id).put({"event": "needs_approval", "approval": outcome.approval.tool_name})
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

    @app.get("/status/{run_id}")
    def status(run_id: str) -> dict[str, Any]:
        sess = registry.get(run_id)
        return {"run_id": run_id, "status": sess.status().name}

    @app.get("/approvals")
    def approvals() -> list[dict[str, Any]]:
        """列出所有"等待审批"的会话（审批队列）。"""
        result = []
        for run_id in list(registry._sessions.keys()):
            sess = registry.get(run_id)
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

    @app.post("/approve/{run_id}")
    def approve(run_id: str) -> ChatResponseOut:
        sess = registry.get(run_id)
        if sess.pending_approval() is None:
            raise HTTPException(status_code=409, detail="该会话没有等待审批的请求")
        m_approvals.inc(labels=("approve",))
        outcome = sess.approve()
        if isinstance(outcome, FinalReply):
            _bus(run_id).put({"event": "final", "text": outcome.text})
            return ChatResponseOut(
                run_id=run_id,
                status=sess.status().name,
                kind="final",
                text=outcome.text,
                messages=_serialize_messages(outcome.messages),
            )
        if isinstance(outcome, NeedsApproval):
            # 审批一个后又遇到下一个审批
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

    @app.post("/reject/{run_id}")
    def reject(run_id: str) -> ChatResponseOut:
        sess = registry.get(run_id)
        if sess.pending_approval() is None:
            raise HTTPException(status_code=409, detail="该会话没有等待审批的请求")
        m_approvals.inc(labels=("reject",))
        outcome = sess.reject()
        if isinstance(outcome, FinalReply):
            _bus(run_id).put({"event": "final", "text": outcome.text})
            return ChatResponseOut(
                run_id=run_id,
                status=sess.status().name,
                kind="final",
                text=outcome.text,
                messages=_serialize_messages(outcome.messages),
            )
        if isinstance(outcome, NeedsApproval):
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
    def chat_stream(run_id: str, body: ChatRequestIn) -> StreamingResponse:
        """流式对话（SSE 打字机）：模型边生成边把增量推给前端。
        前端拿到增量直接渲染，就能看到"逐字打出"的效果。"""
        sess = registry.get(run_id)

        def generate() -> Any:
            try:
                for event in sess.stream(body.text):
                    # SSE 格式：每条事件以 "data: <json>\n\n" 结尾
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            except Exception as e:  # DENY / 工具错误等，以 error 事件结束
                logger.exception("流式 chat 失败 run=%s", run_id)
                err = {"type": "error", "message": str(e)}
                yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"

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
        """SSE：监听某会话的事件（最终结果 / 审批请求 / 错误）。"""
        q = _bus(run_id)

        def generate() -> Any:
            while True:
                item = q.get()
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                if item.get("event") in ("final", "error"):
                    break

        return StreamingResponse(generate(), media_type="text/event-stream")

    @app.get("/capabilities")
    def capabilities() -> dict[str, Any]:
        """列出这个 Agent 服务目前可用的能力（工具 + 启用的特性）。"""
        tool_names = sorted(t.name for t in registry._catalog.all())
        skill_cat = registry.extra.get("skill_catalog")
        return {
            "tools": tool_names,
            "features": {
                "memory": "memory_service" in registry.extra,
                "skills": [s for s in (skill_cat.aliases() if skill_cat else [])],
                "web": any(t.name.startswith("web.") for t in registry._catalog.all()),
                "mcp_server": registry.extra.get("mcp_server"),
            },
        }

    @app.get("/memory/{scope}")
    def memory_view(scope: str) -> list[dict[str, Any]]:
        """查看某作用域（run/session/user）下已确认的记忆。"""
        mem = registry.extra.get("memory_service")
        if mem is None:
            raise HTTPException(status_code=404, detail="未启用记忆(memory=True)")
        from warden_agent.memory import MemoryScope

        try:
            enum_scope = MemoryScope[scope.upper()]
        except KeyError:
            raise HTTPException(status_code=400, detail=f"未知作用域: {scope}") from None
        return [
            {"scope": i.scope.name, "key": i.key, "text": i.content.text, "status": i.status.name}
            for i in mem.recall(enum_scope, limit=100)
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
