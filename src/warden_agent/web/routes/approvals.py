"""审批队列与审批决策路由（从 `web/server.py` 机械拆分而来，handler 逐字照搬）。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from warden_agent.runtime.locking import RunLease
from warden_agent.runtime.session import AgentSession, FinalReply, NeedsApproval
from warden_agent.store.sqlite import SqliteStore
from warden_agent.web.auth import TrustedCaller
from warden_agent.web.coordination import EventBus
from warden_agent.web.http_kit import _approval_actor, _serialize_messages
from warden_agent.web.schemas import ApprovalDecisionIn, ChatResponseOut
from warden_agent.web.session_registry import SessionRegistry


def register(
    app: FastAPI,
    *,
    _acquire_run: Callable[[str], RunLease],
    _owner_scope: Callable[[Request], str | None],
    bus: EventBus,
    m_approvals: Any,
    registry: SessionRegistry,
    store: SqliteStore,
) -> None:
    """注册 /approvals、/approvals/history、/approve/{run_id}、/reject/{run_id}。"""

    @app.get("/approvals")
    def approvals(request: Request) -> list[dict[str, Any]]:
        """列出"等待审批"的会话（审批队列）。

        认证模式下只列**调用者自己**名下的会话——审批队列是人工闸门入口，
        跨租户可见会让别人看到甚至替你决策高危操作。
        **admin 例外**：admin 是本部署里的审批人角色，职责分离要求"批的人不是发起人"，
        他必须能看**全局**队列（否则只能靠旁人报 run_id 给他）；这与 `/audit`、
        挂起/恢复列表的"admin 全局视图"同一口径，普通用户仍然只看自己的。
        """
        caller: TrustedCaller | None = getattr(request.state, "caller", None)
        sees_all = caller is not None and caller.is_admin
        result = []
        for run_id in registry.run_ids():
            sess = registry.get(run_id)
            if not sees_all and caller is not None and sess.run.user_id != caller.user_id:
                continue
            pending = sess.pending_approval()
            if pending is not None:
                # 形状与 `/chat` 响应体、总线事件同源（`ApprovalRequest.to_dict`）：
                # 这里修前是手写字典，与 to_dict “恰好长得一样”——
                # to_dict 多一个字段，队列就少一个，而前端两份数据喂给同一个面板。
                result.append({"run_id": run_id, **pending.to_dict()})
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
    def approve(
        run_id: str,
        request: Request,
        body: ApprovalDecisionIn | None = None,
    ) -> ChatResponseOut:
        lease = _acquire_run(run_id)
        try:
            sess = registry.get(run_id)
            pending = sess.pending_approval()
            if pending is None:
                raise HTTPException(status_code=409, detail="该会话没有等待审批的请求")
            run = store.load_run(run_id)
            approver_id, approver_role = _approval_actor(
                request, pending, run.user_id if run is not None else None, body
            )
            m_approvals.inc(labels=("approve",))
            outcome = sess.approve()
            store.record_approval_decision(
                run_id, pending.approval_id, pending.tool_name,
                pending.arguments, "approved",
                approver_id=approver_id, approver_role=approver_role,
            )
            return _outcome_response(run_id, sess, outcome)
        finally:
            lease.stop()

    @app.post("/reject/{run_id}")
    def reject(
        run_id: str,
        request: Request,
        body: ApprovalDecisionIn | None = None,
    ) -> ChatResponseOut:
        lease = _acquire_run(run_id)
        try:
            sess = registry.get(run_id)
            pending = sess.pending_approval()
            if pending is None:
                raise HTTPException(status_code=409, detail="该会话没有等待审批的请求")
            run = store.load_run(run_id)
            approver_id, approver_role = _approval_actor(
                request, pending, run.user_id if run is not None else None, body
            )
            m_approvals.inc(labels=("reject",))
            outcome = sess.reject()
            store.record_approval_decision(
                run_id, pending.approval_id, pending.tool_name,
                pending.arguments, "rejected",
                approver_id=approver_id, approver_role=approver_role,
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
                # 形状与 `/chat` 响应体同源（`ApprovalRequest.to_dict`），
                # 别把 `tool_name` 字符串塞进来。
                {"event": "needs_approval", "approval": outcome.approval.to_dict()},
            )
            return ChatResponseOut(
                run_id=run_id,
                status=sess.status().name,
                kind="needs_approval",
                # 同一个领域对象的对外形状只能有一处定义。
                approval=outcome.approval.to_dict(),
            )
        raise HTTPException(status_code=500, detail="未知结果类型")
