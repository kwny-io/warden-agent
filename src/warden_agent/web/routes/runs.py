"""会话/用户/状态路由（从 `web/server.py` 机械拆分而来，handler 逐字照搬）。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from warden_agent.store.sqlite import SqliteStore
from warden_agent.web.auth import TrustedCaller
from warden_agent.web.http_kit import _serialize_messages
from warden_agent.web.schemas import UserCreateIn
from warden_agent.web.session_registry import SessionRegistry


def register(
    app: FastAPI,
    *,
    _identity: Callable[[Request, str | None], str],
    registry: SessionRegistry,
    store: SqliteStore,
) -> None:
    """注册 /status、/runs、/users、/messages（注册顺序与拆分前一致）。"""

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
