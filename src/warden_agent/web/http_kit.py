"""HTTP 层纯辅助函数（从 `web/server.py` 机械拆分而来，逐字照搬）。

这里只放**不依赖 build_app 闭包**的模块级辅助：SSE 并发闸门、版本解析、
幂等重放、problem+json 构造、路径判定、模型头、序列化、凭证/审批/记忆辅助。
凡是依赖闭包自由变量的函数仍留在 `web/server.py` 或各自的 routes 模块里。
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from warden_agent.core.metrics import note
from warden_agent.core.settings import env_bool
from warden_agent.credential.broker import CredentialBroker
from warden_agent.credential.vault import DEPLOYMENT_SCOPE
from warden_agent.model.fake import FakeModel
from warden_agent.model.model import Message
from warden_agent.runtime.session import ApprovalRequest
from warden_agent.web.auth import TrustedCaller
from warden_agent.web.coordination import IdempotencyStore
from warden_agent.web.schemas import ApprovalDecisionIn


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
    """无需认证即可访问的路径：文档、演示首页、健康检查（负载均衡探活必经之路）。

    为什么把整个文档簇（含 `/docs/oauth2-redirect`）都算公开：它们是 FastAPI 自动生成的
    **静态页面**，不含任何业务数据；而 `/docs` 公开、它的子页却 401，会让"文档可用"
    这件事变得半真半假（Swagger 的 Authorize 流程正好会跳到那个子页）。
    ——这条是**路由面清单守卫**（`tests/test_route_surface.py`）跑出来的：登记为公开的路径
    里只有它没通过。

    为什么**控制台外壳**（`/assets/*` 与 `/index.html`）必须公开——这是本函数里最不显然的
    一条，也是曾经真出错的一条：浏览器加载 `<script type="module" src="/assets/index-xxx.js">`
    时**带不上** `Authorization` 头（HTML 规范如此，不是我们的代码能决定的）。
    于是只要 `/assets/*` 要鉴权，`/` 能打开、它引用的 JS 拿 401 → React 从不挂载 →
    页面上连"填访问密钥"的输入框都没有 → **在开了鉴权的部署里，控制台是白屏，谁也登不进来**。
    外壳里没有任何业务数据（构建产物是公开可得的代码），数据全在后面那些要鉴权的接口后面，
    所以这不是放松安全，而是把"登录入口本身"从门禁里拿出来。
    ——这条是**真浏览器 e2e**（`web/e2e/auth.spec.ts`）跑出来的：pytest 里
    "`/assets` 能加载"（不带鉴权）与"数据接口 401"（不带静态资源）分别都是绿的，
    但没人把这两件事放在同一个配置下跑过，于是缺口一直不在任何断言里。

    副作用：静态资源也顺带豁免限流（限流只挡业务接口）——这是想要的：
    外壳有缓存、请求量大，且不含数据。
    """
    public = {
        "/",
        "/index.html",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
        "/openapi.json",
        "/health/live",
        "/health/ready",
    }
    if path in public:
        return True
    # SPA 打包产物：前缀匹配（文件名带内容哈希，每次构建都会变，没法穷举）
    return path.startswith("/assets/")


def _is_approval_decision_path(path: str) -> bool:
    """是否是一条"审批决策"路径（`POST /approve|/reject/{run_id}`）。

    单独抽成函数，是因为它承载一条**权限例外**：admin（审批人角色）可以跨归属批别人
    名下的 Run。为什么不干脆把 admin 做成"可跨归属操作任何 Run"：那等于把运维账号
    变成万能写权限（能删、能改别人的会话），而职责分离只需要"能批"这一点。
    权限例外要窄到能一眼审完——所以按**路径**给，不按操作类型给（COMMAND 还盖着
    `DELETE /runs/{id}`、`POST /models/select` 这些不该跨归属的动作）。
    """
    p = path.split("?")[0].rstrip("/")
    return p.startswith("/approve/") or p.startswith("/reject/")


def _model_kind_headers(model: Any) -> dict[str, str]:
    """会话用的是演示模型（FakeModel）时，给响应挂上机器可读的「这是演示脚本」标记。

    为什么不能只靠 `/capabilities`：能力页是"要专门去看"的东西，而 `/chat` 的响应
    会被抓包工具、网关、集成脚本直接消费。把标记挂在**响应本身**上，才能在数据流里
    拦住"把演示输出当系统能力证据"。

    判定看的是**真正服务这个请求的模型对象**（`sess.model`），不是启动时的默认模型——
    `/models/select` 能把某个用户切到假模型，那时候按启动值判断就会漏报。

    值必须是 ASCII：HTTP 头按 latin-1 编码，中文会直接抛错。
    """
    if not isinstance(model, FakeModel):
        return {}
    return {
        "X-Warden-Model-Kind": "fake",
        # 199 = 自定义/非标准告警（RFC 9110 允许 199 段），网关与日志能直接看到
        "Warning": '199 warden-agent "demo instance: replies are scripted, not real inference"',
    }


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


#: 自建端点的三件套存在**同一条**凭证里（名字仍叫 `model:custom`）。
#: 为什么不是三张凭证：它们描述的是同一个"我要连哪儿"的决定，拆开存会出现
#: 「换了 base_url 却还留着旧 model 名」这种半更新状态。broker 的 fields 本来就是
#: 一个字符串字典，天然适合放一组配套参数。
CUSTOM_BASE_URL_FIELD = "base_url"
CUSTOM_MODEL_FIELD = "model"


def _check_custom_base_url(raw: str) -> str:
    """校验并规范化自建模型的 base_url；不合法就抛 400。

    **有意放行内网与回环地址**（`127.0.0.1` / `10.x` / `192.168.x` / 容器服务名）：
    这个功能存在的理由就是"公司不愿把对话发到公网大模型，自己起一个 OpenAI 兼容
    端点"。若照 `rag/knowledge.py` 那条 SSRF 规则去拦私有地址，功能当场失效。

    反过来，这里**只**挡两件事：
      1. 非 http/https（`file://` 读本地文件、`ftp://`、瞎写的字符串）；
      2. 没有主机名（`http://`、`http:///v1`）。
    其余的 URL 合法性交给 `urlparse` 与真正发请求时的 SDK。

    代价要说清：任何登录用户都能让服务端向一个自己指定的地址发请求（含内网地址）。
    这与"每用户自己配、只影响自己"是一体两面——不给出站就等于没有这个功能；
    要限制，应当在网络层（出站白名单）做，而不是把它藏进一个假装能防住的字符串检查。
    """
    url = (raw or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="自建接入需要 base_url（OpenAI 兼容端点）")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(
            status_code=400,
            detail=f"base_url 必须是 http/https 地址，收到的是 {parsed.scheme or '空'}：{url!r}",
        )
    if not parsed.hostname:
        raise HTTPException(status_code=400, detail=f"base_url 缺少主机名：{url!r}")
    return url.rstrip("/")


def _custom_fields(
    model_id: str, extra: dict[str, str], base_url: str | None, model_name: str | None
) -> dict[str, str]:
    """组出要交给 broker 的那份 fields（key 之外要不要再带上端点）。

    只有 `custom` 才把端点并进去：内置四家的端点由代码固定，允许请求体覆盖
    等于让用户把 DeepSeek 的 key 发到任意地址——正好是 custom 那条注释里
    明令禁止的密钥外泄路径。
    """
    fields = dict(extra)
    if model_id == "custom":
        if base_url:
            fields[CUSTOM_BASE_URL_FIELD] = base_url
        if model_name:
            fields[CUSTOM_MODEL_FIELD] = model_name
    return fields


def _custom_endpoint_or_none(
    broker: CredentialBroker, scope: str
) -> tuple[str, str] | None:
    """取回某作用域已登记的 `(base_url, model)`；没有登记就返回 None。

    沿用 `_registered_model_key` 的两级查找：先看自己的，再回落到部署级。
    两件都在同一条凭证里，所以要么都拿到、要么都拿不到——不会出现
    「有地址没模型名」的半张配置。
    """
    name = _model_key_name("custom")
    for candidate in (scope, DEPLOYMENT_SCOPE):
        if not broker.has(name, candidate):
            continue
        lease = broker.issue(name, scope=candidate)
        fields = lease.value.fields
        base_url = fields.get(CUSTOM_BASE_URL_FIELD)
        model = fields.get(CUSTOM_MODEL_FIELD)
        if base_url:
            return base_url, model or ""
    return None


def _credential_scope(request: Request) -> str:
    """凭证作用域 = 调用者身份。

    多租户下 `tenant_id` 是**整租户共用**的（`WARDEN_TENANT`，默认 `local`），
    用它做隔离会让同租户的 A、B 两用户互相读到对方导入的 key。所以这里用
    `user_id`（= principal_id，来自凭证本身）作为隔离维度；匿名开发模式
    （未开鉴权）回落到部署级作用域。
    """
    caller: TrustedCaller | None = getattr(request.state, "caller", None)
    return caller.user_id if caller is not None else DEPLOYMENT_SCOPE


#: 单操作员部署的逃生门：显式设成 1 才允许"自己批自己"（默认**不允许**）
ENV_APPROVAL_ALLOW_SELF = "WARDEN_APPROVAL_ALLOW_SELF"


def _approval_actor(
    request: Request,
    pending: ApprovalRequest,
    owner: str | None,
    body: ApprovalDecisionIn | None,
) -> tuple[str | None, str | None]:
    """审批动作的授权落点：**绑定审批单** + **职责分离**。返回 (approver_id, role)。

    与中间件的 RBAC/归属授权是两件事，都必须过：中间件回答"你能做审批这类动作吗、
    能碰这个 run 吗"；这里回答"**这一张**审批单，由**这个人**拍板合不合规"。

    职责分离（SoD）为什么默认开启：审批门禁的**全部意义**就是"高危动作要过第二个人"。
    如果发起人自己就能批，这道门在"某个被攻陷或误操作的账号"面前等于不存在——
    攻击者只要拿到一个 user 凭证，就能 `shell.run(rm -rf …)` 然后自己点批准。
    失败方向因此选"严格"：单操作员部署会撞到 403，报错里直接给出
    `WARDEN_APPROVAL_ALLOW_SELF=1` 这个显式开关（知道自己在放弃什么才能放弃它）。
    """
    caller: TrustedCaller | None = getattr(request.state, "caller", None)

    # 1) 绑单：调用方声明了"我批的是哪张单"，就必须与当前挂起的那张一致。
    if body is not None and body.approval_id and body.approval_id != pending.approval_id:
        note("warden_approval_binding_mismatch_total", "审批单绑定不匹配次数（前端审批单已过期）")
        raise HTTPException(
            status_code=409,
            detail=(
                f"审批单已变更：你提交的是 {body.approval_id}，当前挂起的是 "
                f"{pending.approval_id}。请刷新审批队列后重新决策。"
            ),
        )

    # 2) 职责分离：发起人不能批自己的单（admin 也不豁免——运维账号同样是账号）。
    if (
        caller is not None
        and owner
        and caller.user_id == owner
        and not env_bool(ENV_APPROVAL_ALLOW_SELF)
    ):
        note("warden_approval_sod_denied_total", "审批被职责分离拒绝的次数（自己批自己）")
        raise HTTPException(
            status_code=403,
            detail=(
                f"职责分离：发起该请求的用户（{owner}）不能批准自己的审批单，"
                f"请由其他 principal 审批（管理员名单 WARDEN_ADMIN_PRINCIPALS）；"
                f"单操作员部署可显式设 {ENV_APPROVAL_ALLOW_SELF}=1 解除该限制。"
            ),
        )

    if caller is None:
        return None, None
    return caller.user_id, caller.role


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


def _memory_scope_or_400(scope: str) -> Any:
    """把 URL 里的作用域名（run/session/user/workspace）转成 `MemoryScope`；未知则 400。

    以前这段逻辑写在路由体内并 `from ... import` 到局部——三个记忆路由（存量 + 候选）
    各拄一份就很容易漏一个，所以提成共用的。
    """
    from warden_agent.memory import MemoryScope

    try:
        return MemoryScope[scope.upper()]
    except KeyError:
        raise HTTPException(status_code=400, detail=f"未知作用域: {scope}") from None


def _memory_service_or_404(registry: Any) -> Any:
    """取记忆服务；未启用记忆（不是 `memory=True`）则 404，而不是拿 None 去调方法报 500。"""
    mem = registry.extra.get("memory_service")
    if mem is None:
        raise HTTPException(status_code=404, detail="未启用记忆(memory=True)")
    return mem
