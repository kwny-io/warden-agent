"""记忆路由（从 `web/server.py` 机械拆分而来，handler 逐字照搬）。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from warden_agent.web.http_kit import _memory_scope_or_400, _memory_service_or_404
from warden_agent.web.session_registry import SessionRegistry


def register(
    app: FastAPI,
    *,
    _identity: Callable[[Request, str | None], str],
    registry: SessionRegistry,
) -> None:
    """注册 /memory/{scope} 与候选的 confirm/reject（注册顺序与拆分前一致）。"""

    @app.get("/memory/{scope}")
    def memory_view(request: Request, scope: str) -> list[dict[str, Any]]:
        """查看某作用域（run/session/user/workspace）下**调用者自己**的记忆。

        归属边界：记忆按 `owner`（用户 id）严格隔离——认证模式下只看得到自己写的，
        否则任一用户就能读到全部署所有人的记忆（跨租户泄露），甚至无从发现被投毒。
        注意：这里只返回**已生效（ACTIVE）**的记忆；待确认候选在
        `GET /memory/{scope}/candidates`。
        """
        enum_scope = _memory_scope_or_400(scope)
        mem = _memory_service_or_404(registry)
        owner = _identity(request, None)   # 身份来自凭证，不接受查询参数自称
        return [
            {"scope": i.scope.name, "key": i.key, "text": i.content.text,
             "status": i.status.name, "owner": i.owner}
            for i in mem.recall(enum_scope, limit=100, owner=owner)
        ]

    @app.get("/memory/{scope}/candidates")
    def memory_candidates(request: Request, scope: str) -> list[dict[str, Any]]:
        """列出**调用者自己**的待确认记忆候选（含与已有记忆冲突的）。

        为什么要有这个面：`memory.remember` 写的是"先进入待确认候选"——如果候选根本
        看不见，那个"确认环节"就只是文案（而看全量候选又会把同租户别人的候选露出去，
        所以按 owner 过滤在服务层就把住了）。`conflicts_with` 不为空时就是"会替掉旧条目"，
        控制台可以据此提醒。
        """
        enum_scope = _memory_scope_or_400(scope)
        mem = _memory_service_or_404(registry)
        owner = _identity(request, None)
        out: list[dict[str, Any]] = []
        for c in mem.candidates(enum_scope, owner=owner):
            item = c.item
            out.append({
                "uid": item.uid, "scope": item.scope.name, "key": item.key,
                "text": item.content.text, "status": item.status.name,
                "owner": item.owner, "conflicts_with": item.conflicts_with or "",
                "replaces_text": mem.conflict_preview(c),
            })
        return out

    @app.post("/memory/{scope}/candidates/{uid}/confirm")
    def memory_candidate_confirm(
        request: Request, scope: str, uid: str,
        replace_existing: bool = True,
    ) -> dict[str, Any]:
        """确认一条候选 → 生效。**只能确认自己的**（uid 不归你 = 404，不说"存在但不归你"）。"""
        enum_scope = _memory_scope_or_400(scope)
        mem = _memory_service_or_404(registry)
        owner = _identity(request, None)
        item = mem.approve_candidate(
            uid, scope=enum_scope, owner=owner, replace_existing=replace_existing,
        )
        if item is None:
            raise HTTPException(status_code=404, detail="候选不存在或不属于当前调用者")
        return {"uid": item.uid, "key": item.key, "text": item.content.text,
                "status": item.status.name}

    @app.post("/memory/{scope}/candidates/{uid}/reject")
    def memory_candidate_reject(request: Request, scope: str, uid: str) -> dict[str, Any]:
        """拒绝一条候选（软删，留审计痕迹）。同样只能拒自己的。"""
        enum_scope = _memory_scope_or_400(scope)
        mem = _memory_service_or_404(registry)
        owner = _identity(request, None)
        if not mem.reject_candidate(uid, scope=enum_scope, owner=owner):
            raise HTTPException(status_code=404, detail="候选不存在或不属于当前调用者")
        return {"uid": uid, "status": "TOMBSTONED"}
