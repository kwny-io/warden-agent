"""会话/用户/状态路由（从 `web/server.py` 机械拆分而来，handler 逐字照搬）。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from warden_agent.store.sqlite import SqliteStore
from warden_agent.web.auth import TrustedCaller
from warden_agent.web.http_kit import _serialize_messages
from warden_agent.web.schemas import RunDeleteIn, UserCreateIn
from warden_agent.web.session_registry import SessionRegistry

# 单次批量删除的 id 上限：一次请求删除过多会话会把「逐个校验归属 + 逐个删五张表」
# 变成一次长事务，而这条路由又落在限流桶里。200 足够覆盖「手删很累」的真实场景。
MAX_BATCH_DELETE = 200


def register(
    app: FastAPI,
    *,
    _identity: Callable[[Request, str | None], str],
    _owner_of: Callable[[str], str | None],
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

    @app.delete("/runs")
    def delete_runs(body: RunDeleteIn, request: Request) -> dict[str, Any]:
        """批量删除会话（左栏「勾选模式 + 全选 + 一次删掉」的后端）。

        **归属必须在这里逐个校验**，不能指望网关：网关的归属授权靠
        `_extract_run_id(path)` 从**路径**里取 run_id，而这条路由的 id 全在请求体里，
        网关取到的是 `None` → `owner_authorizer` 直接放行。少了下面这个循环，
        任何登录用户只要猜/知道别人的 run_id，就能把它连库带记忆一起删掉。

        语义选择：**要么全删、要么一个都不删**。逐个删到一半失败（比如第 3 个是别人的）
        会留下「一部分没了、一部分还在」且用户不知道哪些没删——批量操作的失败必须
        是原子的，否则重试与对账都无解。
        """
        # 去重 + 去空：界面上全选后重复点几次不该变成重复删除
        # （`delete_run` 是幂等的，但重复 id 会让返回的 deleted 列表误导调用方）。
        ids = list(dict.fromkeys(x.strip() for x in body.run_ids if x and x.strip()))
        if not ids:
            raise HTTPException(status_code=400, detail="run_ids 不能为空")
        if len(ids) > MAX_BATCH_DELETE:
            raise HTTPException(
                status_code=400,
                detail=f"一次最多删除 {MAX_BATCH_DELETE} 个会话（收到 {len(ids)} 个）",
            )
        caller: TrustedCaller | None = getattr(request.state, "caller", None)
        if caller is not None:
            # 先全部校验，再动手删：校验与删除分成两轮，才能保证原子性。
            for run_id in ids:
                owner = _owner_of(run_id)
                # 无归属（不存在 / 还没落库归属）放行——与 owner_authorizer 同一口径，
                # 否则「删掉一个刚建还没归属的会话」会被自己挡死。
                if owner and owner != caller.user_id:
                    # 不把归属者是谁写进错误信息：那是给攻击者的账号枚举情报
                    # （同 owner_authorizer）。
                    raise HTTPException(
                        status_code=403,
                        detail=f"无权操作会话 {run_id!r}：它不属于当前调用者",
                    )
        for run_id in ids:
            registry.remove(run_id)
            store.delete_run(run_id)
        return {"ok": True, "deleted": ids, "count": len(ids)}

    @app.get("/messages/{run_id}")
    def messages(run_id: str) -> list[dict[str, Any]]:
        """返回某会话的完整对话记录（前端刷新后恢复聊天区用）。"""
        sess = registry.get(run_id)
        return _serialize_messages(sess.messages)
