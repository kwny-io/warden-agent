"""HTTP 请求/响应模型（从 `web/server.py` 机械拆分而来，逐字照搬）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


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


class ApprovalDecisionIn(BaseModel):
    """POST /approve|/reject 的可选请求体：把决策**绑定到具体审批单**。

    为什么可选而不是必填：中控台前端现在的调用不带 body（`POST /approve/{run_id}`），
    改成必填会直接打断既有集成。带上时后端会校验它与当前挂起的审批单一致——
    前端看到的是 A，期间会话又挂起了 B（先批后跑、又撞上下一个高危工具），
    按"批 A"的意图去批 B 是语义错位，必须失败让它刷新。
    """

    approval_id: str | None = None


class ChatResponseOut(BaseModel):
    run_id: str
    status: str
    kind: str  # "final" | "needs_approval" | "error"
    text: str | None = None
    approval: dict[str, Any] | None = None
    messages: list[dict[str, Any]] | None = None
