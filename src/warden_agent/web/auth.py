"""HTTP 认证与授权：把"谁在调 API"变成可审计、可校验的身份。

  - HttpCallerResolver    → 从请求解析出可信调用者身份（认证）。
  - TrustedCallerContext  → 不可变身份四元组（tenant/principalType/principalId/productId）。
  - RunOperationAuthorizer→ 对"某个 Run 操作"做授权，不一致就拒绝。
  - HttpAuthenticationException / HttpAuthorizationException → 401 / 403（problem+json）。

本模块给了一个自包含的 API Key 认证实现（网关/反向代理会做同样的身份注入，这里
是单机版的等价物）：调用方带 `Authorization: Bearer <key>`，服务端把它解析成
TrustedCaller。未配置密钥（api_key=None）时保持"本地开发开放"的旧行为，全部放行。
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from starlette.requests import Request


class HttpAuthenticationError(Exception):
    """未认证（401）：请求没有有效凭据。"""


class HttpAuthorizationError(Exception):
    """未授权（403）：身份合法但无权执行该 Run 操作。"""


@dataclass(frozen=True)
class TrustedCaller:
    """认证后的身份四元组。

    字段一律不得为空字符串、不得超长，约束在构造时校验。
    """

    tenant_id: str
    principal_type: str
    principal_id: str
    product_id: str = "local"

    def __post_init__(self) -> None:
        tenant_id = _normalize(self.tenant_id, "tenantId")
        principal_type = _normalize(self.principal_type, "principalType")
        principal_id = _normalize(self.principal_id, "principalId")
        product_id = _normalize(self.product_id, "productId")
        object.__setattr__(self, "tenant_id", tenant_id)
        object.__setattr__(self, "principal_type", principal_type)
        object.__setattr__(self, "principal_id", principal_id)
        object.__setattr__(self, "product_id", product_id)

    def to_dict(self) -> dict[str, str]:
        return {
            "tenant_id": self.tenant_id,
            "principal_type": self.principal_type,
            "principal_id": self.principal_id,
            "product_id": self.product_id,
        }

    @property
    def user_id(self) -> str:
        """该调用者在"会话归属"维度的身份。

        以 `principal_id` 为准——这是本模块的核心约定：**用户身份来自凭证，不来自请求**。
        历史实现里 `user_id` 是客户端自带的查询参数（`?user_id=xxx`），等于让调用方
        自己声明"我是谁"，拿到一把 key 就能冒充任意用户。认证开启后，端点一律用
        这里返回的值作为归属，查询参数被忽略（见 server._identity）。
        """
        return self.principal_id


def _normalize(value: str, field: str) -> str:
    text = str(value).strip()
    if not text or len(text) > 256:
        raise ValueError(f"{field} must contain 1..256 characters")
    return text


# 本地/未认证时的兜底身份：代表"当前进程本机调用"。
LOCAL_CALLER = TrustedCaller("local", "service", "local-client", "cli")


class RunOperation(StrEnum):
    """Agent 对外暴露的"运行操作"，授权的最小粒度。"""

    START = "START"
    QUERY = "QUERY"
    SUBMIT_INPUT = "SUBMIT_INPUT"
    COMMAND = "COMMAND"
    READ_EVENTS = "READ_EVENTS"
    SUBSCRIBE_EVENTS = "SUBSCRIBE_EVENTS"


def operation_for(method: str, path: str) -> RunOperation:
    """把 (HTTP 方法, 路径) 归到某个 RunOperation，供授权与审计使用。

    与路由表对应：
      POST /chat/{id} / /chat/stream/{id}   → SUBMIT_INPUT
      POST /runs/{id}                        → START（预创建会话）
      GET  /status/{id} / /approvals        → QUERY
      POST /approve|reject/{id}             → COMMAND
      GET  /events/{id}                     → SUBSCRIBE_EVENTS
      GET  /memory/{scope} / /audit         → QUERY
    """
    method = method.upper()
    path = path.split("?")[0].rstrip("/") or "/"
    if method == "GET" and (path.startswith("/status") or path == "/approvals"):
        return RunOperation.QUERY
    if method == "POST" and (path.startswith("/approve") or path.startswith("/reject")):
        return RunOperation.COMMAND
    if method == "GET" and path.startswith("/events"):
        return RunOperation.SUBSCRIBE_EVENTS
    if method == "POST" and path.startswith("/runs"):
        return RunOperation.START
    if method == "POST" and (path.startswith("/chat/stream") or path.startswith("/chat")):
        return RunOperation.SUBMIT_INPUT
    if method == "GET" and (path.startswith("/memory") or path == "/audit"):
        return RunOperation.QUERY
    return RunOperation.QUERY


class ApiKeyAuthenticator:
    """把 `Authorization: Bearer <key>` 解析成 TrustedCaller。

    构造时传一个 {api_key: TrustedCaller} 映射。密钥比对用 secrets.compare_digest
    （常数时间，防时序侧信道）；未知/缺失/格式错误一律抛 HttpAuthenticationError。
    """

    def __init__(self, keys: Mapping[str, TrustedCaller]) -> None:
        if not keys:
            raise ValueError("ApiKeyAuthenticator 至少要有一个 key")
        self._keys: dict[str, TrustedCaller] = dict(keys)

    def authenticate(self, request: Request) -> TrustedCaller:
        header = request.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise HttpAuthenticationError("缺少或格式错误的 Authorization: Bearer <key>")
        token = token.strip()
        for known, caller in self._keys.items():
            if secrets.compare_digest(known, token):
                return caller
        raise HttpAuthenticationError("API Key 无效")


AuthorizeFn = Callable[[TrustedCaller, RunOperation, str | None], None]


class RunOperationAuthorizer:
    """可插拔的授权门。

    构造时不传回调 = 允许一切已认证调用者执行任何操作；传回调则在
    authorize() 里按 (caller, operation, run_id) 决定放行或抛 HttpAuthorizationError。
    """

    def __init__(self, fn: AuthorizeFn | None = None) -> None:
        self._fn = fn

    def authorize(
        self,
        caller: TrustedCaller,
        operation: RunOperation,
        run_id: str | None,
    ) -> None:
        if self._fn is not None:
            self._fn(caller, operation, run_id)


def owner_authorizer(load_owner: Callable[[str], str | None]) -> AuthorizeFn:
    """构造"按 Run 归属"的授权回调：调用者只能碰自己名下的 Run。

    `load_owner(run_id)` 返回该 Run 的归属用户（无归属或不存在时返回 None）。

    放行规则：
      - `run_id` 为空（与具体 Run 无关的操作，如 /models、/health）→ 放行；
      - Run **不存在**或**尚无归属** → 放行，由端点按调用者身份建立归属
        （否则"首次对话/预创建会话"会被自己的授权门挡死）；
      - Run 已有归属且等于调用者身份 → 放行；
      - 其余（归属是别人）→ 抛 HttpAuthorizationError（403）。

    这是把"多租户"从"按字段过滤"变成真正边界的关键一步：此前 `user_id` 可被
    客户端任意指定，且授权器默认空转（全放行），拿到一把 key 就能读写他人会话。
    """
    def authorize_owner(
        caller: TrustedCaller,
        operation: RunOperation,
        run_id: str | None,
    ) -> None:
        if run_id is None:
            return
        owner = load_owner(run_id)
        if owner and owner != caller.user_id:
            raise HttpAuthorizationError(
                f"调用者 {caller.user_id!r} 无权操作归属 {owner!r} 的会话 {run_id!r}"
            )

    return authorize_owner
