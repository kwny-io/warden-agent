"""路由面守卫：**每一条 HTTP 路由都必须登记它的访问级别**，而且登记表会被真的跑一遍。

为什么需要它（而不是"已经有一堆 RBAC 测试了"）：
  RBAC 测试测的是**现在存在**的那些路由——它们回答"这几条守住了吗"，
  但回答不了"**新加的那条守住了吗**"。项目里已经发生过同类问题：
  `/metrics` 一开始只要求"已认证"，viewer（只读角色）也能读，与"只读=能看用户数据
  但不能动"的契约不符（见 `server.py` 里 `/metrics` 的 docstring）。
  没有清单守卫时，这类缺口只会被"下一位审计者"发现，而不是被 CI 发现。

  这与 `tests/test_config_surface.py`（环境变量面）、`tests/test_k8s_manifests.py`
  （清单面）是同一套思路：**把"新增东西必须做出声明"变成一条会红的测试**。

两道检查：
  1. **清单一致性**：`build_app()` 实际暴露的路由集合，必须与下面的登记表**逐条对应**
     （多一条、少一条都失败）——新增路由而不登记 = 测试红。
  2. **清单可执行**：登记为"公开"的，未带凭证必须能访问；登记为"需要认证"的，
     未带凭证必须 401；登记为"管理员"的，普通用户必须 403、管理员必须通过。
     —— 声明与行为不一致也会红（避免"登记表写得漂亮、实际没守着"）。

新增路由时怎么办：在 `ROUTES` 里加一行，写清访问级别与理由；
如果它的管理员限制藏在**请求体分支**里（如 `POST /models/select` 的 `scope=deployment`），
在 `ADMIN_BRANCHES` 里加一行——那一组也是可执行的。
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import httpx
import pytest
from tests.conftest import ScriptedModel, weather_tool

from warden_agent.model.model import ChatResponse
from warden_agent.policy.policy import PolicyEngine
from warden_agent.store.sqlite import SqliteStore
from warden_agent.web.auth import ROLE_ADMIN, ROLE_USER, TrustedCaller
from warden_agent.web.server import build_app


class Access(StrEnum):
    """一条路由的访问级别。"""

    PUBLIC = "public"                 # 无需凭证（探活/文档/演示首页）
    AUTHENTICATED = "authenticated"    # 需要凭证；数据按调用者归属收敛
    ADMIN = "admin"                    # 需要管理员角色


@dataclass(frozen=True)
class Route:
    method: str
    path: str
    access: Access
    note: str = ""


# ---------------------------------------------------------------------------
# 登记表：以 `build_app()` 实际暴露的路由为准（写这张表时逐条核对过 openapi）
# ---------------------------------------------------------------------------
ROUTES: tuple[Route, ...] = (
    # ---- 公开：探活与文档 ----
    Route("GET", "/health/live", Access.PUBLIC, "存活探针：负载均衡必访"),
    Route("GET", "/health/ready", Access.PUBLIC, "就绪探针：探活要能反映存储故障"),
    Route("GET", "/docs", Access.PUBLIC, "接口文档"),
    Route("GET", "/docs/oauth2-redirect", Access.PUBLIC, "FastAPI 文档自带回调页"),
    Route("GET", "/redoc", Access.PUBLIC, "接口文档（redoc）"),
    Route("GET", "/openapi.json", Access.PUBLIC, "OpenAPI 描述"),
    Route("GET", "/", Access.PUBLIC, "内置演示首页"),
    # ---- 认证即可（数据按调用者收敛）----
    Route("GET", "/models", Access.AUTHENTICATED, "看自己可选模型"),
    Route("POST", "/models/select", Access.AUTHENTICATED, "默认只切自己的（scope=self）"),
    Route("POST", "/chat/{run_id}", Access.AUTHENTICATED, "发起/续跑对话"),
    Route("POST", "/chat/stream/{run_id}", Access.AUTHENTICATED, "流式对话"),
    Route("GET", "/events/{run_id}", Access.AUTHENTICATED, "SSE 事件流"),
    Route("GET", "/status/{run_id}", Access.AUTHENTICATED, "Run 状态"),
    Route("GET", "/runs", Access.AUTHENTICATED, "自己的 Run 列表"),
    Route("POST", "/runs/{run_id}", Access.AUTHENTICATED, "取消 Run（与 CLI cancel 同一入口）"),
    Route("DELETE", "/runs/{run_id}", Access.AUTHENTICATED, "删除 Run"),
    Route("GET", "/users", Access.AUTHENTICATED, "用户列表"),
    Route("POST", "/users", Access.AUTHENTICATED, "登记/切换用户"),
    Route("GET", "/messages/{run_id}", Access.AUTHENTICATED, "会话消息"),
    Route("GET", "/approvals", Access.AUTHENTICATED, "待审批列表"),
    Route("GET", "/approvals/history", Access.AUTHENTICATED, "审批历史"),
    Route("POST", "/approve/{run_id}", Access.AUTHENTICATED, "批准（另受'不能批自己的单'约束）"),
    Route("POST", "/reject/{run_id}", Access.AUTHENTICATED, "拒绝"),
    Route("GET", "/audit", Access.AUTHENTICATED, "审计查询（按调用者收敛，admin 可见全局）"),
    Route("GET", "/recovery/plan", Access.AUTHENTICATED, "恢复计划（按调用者收敛，admin 全局）"),
    Route("GET", "/alerts/stuck", Access.AUTHENTICATED, "挂起告警（按调用者收敛，admin 全局）"),
    Route("GET", "/memory/{scope}", Access.AUTHENTICATED, "记忆查询（按调用者归属）"),
    Route("GET", "/memory/{scope}/candidates", Access.AUTHENTICATED,
          "待确认记忆候选（按调用者归属，含冲突项）"),
    Route("POST", "/memory/{scope}/candidates/{uid}/confirm", Access.AUTHENTICATED,
          "确认候选 → 生效（只能确认自己的）"),
    Route("POST", "/memory/{scope}/candidates/{uid}/reject", Access.AUTHENTICATED,
          "拒绝候选（只能拒自己的）"),
    Route("GET", "/capabilities", Access.AUTHENTICATED, "能力自述（含 mock/演示标注）"),
    # ---- 管理员 ----
    Route("GET", "/metrics", Access.ADMIN, "指标：含全局调用量/错误率，不给只读角色"),
)

# 管理员限制藏在**请求体分支**里、整条路由本身只要求"已认证"的情形。
# 这里也做成可执行的：写进来就必须真的挡住普通用户。
ADMIN_BRANCHES: tuple[tuple[str, str, dict[str, object]], ...] = (
    (
        "POST",
        "/models/select",
        {"id": "fake", "scope": "deployment"},
    ),
)

# 静态资源挂载（SPA）。它不是 API 路由，但必须排在最后：否则会遮住 API 路径。
STATIC_MOUNT = "/assets"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def _app(*, alice_role: str = ROLE_USER):  # noqa: ANN202 - 返回 FastAPI 实例
    return build_app(
        model=ScriptedModel([ChatResponse(content="好", finish_reason="stop")] * 4),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=SqliteStore(Path(tempfile.mkdtemp()) / "t.db"),
        api_keys={
            "k-alice": TrustedCaller("tenant-a", "user", "alice", role=alice_role),
            "k-bob": TrustedCaller("tenant-a", "user", "bob"),
        },
    )


def _client(*, alice_role: str = ROLE_USER) -> httpx.AsyncClient:
    """带鉴权的客户端：所有非公开路由都要求 `Authorization`。"""
    app = _app(alice_role=alice_role)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _fill_params(path: str) -> str:
    """把路径参数换成占位值。鉴权中间件在路由匹配前就拦，所以值本身无所谓。"""
    return path.replace("{run_id}", "run-x").replace("{scope}", "session")


def _live_routes() -> set[tuple[str, str]]:
    """`build_app()` 实际暴露的 (方法, 路径) 集合（去掉 HEAD 与静态挂载）。"""
    live: set[tuple[str, str]] = set()
    for r in _app().routes:
        path = getattr(r, "path", None)
        if path is None or path == STATIC_MOUNT:
            continue
        for method in sorted(getattr(r, "methods", []) or []):
            if method in ("HEAD", "OPTIONS"):
                continue
            live.add((method, path))
    return live


# ---------------------------------------------------------------------------
# 1) 清单一致性
# ---------------------------------------------------------------------------


def test_每条实际路由都在登记表里_且登记表没有多余项() -> None:
    declared = {(r.method, r.path) for r in ROUTES}
    live = _live_routes()
    undeclared = sorted(live - declared)
    stale = sorted(declared - live)
    assert not undeclared, (
        f"这些路由没有登记访问级别（新增路由请加进 ROUTES）：{undeclared}"
    )
    assert not stale, f"登记表里有已不存在的路由（请删掉）：{stale}"


def test_登记表内部不含重复项() -> None:
    keys = [(r.method, r.path) for r in ROUTES]
    assert len(keys) == len(set(keys)), f"登记表有重复：{keys}"


def test_静态资源挂载排在所有路由之后() -> None:
    """顺序类问题很隐蔽：挂载若排到前面，会把同前缀的 API 路由整段遮住。"""
    paths = [getattr(r, "path", "") for r in _app().routes]
    assert STATIC_MOUNT in paths
    assert paths[-1] == STATIC_MOUNT, f"静态挂载不在最后：{paths[-3:]}"


# ---------------------------------------------------------------------------
# 2) 清单可执行：声明 vs 行为
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ROUTES, ids=lambda r: f"{r.method} {r.path}")
@pytest.mark.asyncio
async def test_公开路由不需要凭证_其余路由没有凭证一律401(route: Route) -> None:
    """把登记表**跑一遍**：公开的必须进得去，非公开的必须拦在 401。

    只看"拦住了"不够——公开路由若被误加认证，探活/文档就会静默失效（上线第一步就炸），
    所以两个方向都要断言。
    """
    async with _client() as c:
        r = await c.request(route.method, _fill_params(route.path), json={})
        if route.access is Access.PUBLIC:
            assert r.status_code != 401, f"{route.path} 被误加了认证：{r.status_code}"
            assert r.status_code != 403, f"{route.path} 被误加了授权：{r.status_code}"
        else:
            assert r.status_code == 401, (
                f"{route.path} 未带凭证却返回 {r.status_code}（应为 401）"
            )


@pytest.mark.asyncio
async def test_管理面路由_普通用户403_管理员200() -> None:
    async with _client(alice_role=ROLE_USER) as c:
        r = await c.get("/metrics", headers={"Authorization": "Bearer k-alice"})
        assert r.status_code == 403, r.text
    async with _client(alice_role=ROLE_ADMIN) as c:
        r = await c.get("/metrics", headers={"Authorization": "Bearer k-alice"})
        assert r.status_code == 200
        assert "warden_" in r.text


@pytest.mark.asyncio
async def test_登记为管理员分支的请求_普通用户403_管理员放行() -> None:
    """ADMIN_BRANCHES 是可执行的：写进这张表就必须真的挡住普通用户。"""
    for method, path, body in ADMIN_BRANCHES:
        async with _client(alice_role=ROLE_USER) as c:
            r = await c.request(
                method, path, json=body, headers={"Authorization": "Bearer k-alice"}
            )
            assert r.status_code == 403, f"{method} {path} {body} 普通用户竟然通过"
        async with _client(alice_role=ROLE_ADMIN) as c:
            r = await c.request(
                method, path, json=body, headers={"Authorization": "Bearer k-alice"}
            )
            assert r.status_code == 200, f"{method} {path} {body} 管理员被挡住：{r.text}"


@pytest.mark.asyncio
async def test_管理员名单没配时没有任何人是管理员() -> None:
    """fail-closed：忘配名单绝不能变成"人人都是管理员"，管理面路由必须关着。

    这里刻意**不走** `build_app(api_keys=...)` 的显式角色，而是走配置解析路径
    （`resolve_auth`），因为它才是生产里真正决定角色的那条路。
    """
    from warden_agent.web.run_server import resolve_auth

    keys, mode = resolve_auth({"WARDEN_API_KEYS": "alice:k-alice"})
    assert mode == "bearer" and keys is not None
    assert keys["k-alice"].role == ROLE_USER  # 没配 admin 名单 → 谁都不是 admin
    app = build_app(
        model=ScriptedModel([ChatResponse(content="好", finish_reason="stop")] * 4),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=SqliteStore(Path(tempfile.mkdtemp()) / "t.db"),
        api_keys=keys,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await c.get("/metrics", headers={"Authorization": "Bearer k-alice"})
        assert r.status_code == 403


@pytest.mark.asyncio
async def test_本地匿名模式不做鉴权_但那只在未配凭证时成立() -> None:
    """本项目的本地开发口径：不配 `WARDEN_API_KEYS` ⇒ 不鉴权（行为不变）。

    为什么仍要写这条：它锁住"**匿名模式只在未配凭证时出现**"这个边界条件——
    否则未来某次重构让"配了凭证但不认"也能落到匿名分支，就成了绕过鉴权的后门。
    """
    app = build_app(
        model=ScriptedModel([ChatResponse(content="好", finish_reason="stop")] * 4),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=SqliteStore(Path(tempfile.mkdtemp()) / "t.db"),
        api_keys=None,  # 未配凭证：本地匿名
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        assert (await c.get("/metrics")).status_code == 200
        assert (await c.get("/audit")).status_code in (200, 404)
