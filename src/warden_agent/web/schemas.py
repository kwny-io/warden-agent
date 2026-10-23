"""HTTP 请求/响应模型（从 `web/server.py` 机械拆分而来，逐字照搬）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


# ---- HTTP 请求/响应模型 ----
class ChatRequestIn(BaseModel):
    text: str


class ModelProbeIn(BaseModel):
    """POST /models/probe 的请求体：主动探一次「这个 provider 到底通不通」。

    `force=True` 才绕过 60 秒缓存重探——界面不该因为轮询就把厂商打爆。

    `base_url` / `model` 只对 `id="custom"` 有意义：那时它们是**还没落库的候选值**，
    用于"先探通、再保存"（用户在地址栏里敲完就想知道连不连得上，而不是先存进配置
    再探——存错了还得再改一次）。

    `api_key` 同理：**还没落库的那把钥匙**。没有它，"先探通再保存"对需要密钥的模型
    就不可能——探针会先回一句"需要先导入 API Key"，等于逼用户把一份可能写错的配置
    先存进去才能验证它写错没写错。探针**不落库**，这把 key 用完即弃。
    """

    id: str
    force: bool = False
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None


class ModelSelectIn(BaseModel):
    """POST /models/select 的请求体（模型切换 / 导入 API Key）。"""

    id: str
    api_key: str | None = None
    # "self"（默认）= 只切自己的会话；"deployment" = 切**部署默认**（所有人没自选过的会话都受影响）
    # ——后者是运维动作，**只有管理员**能做（见 /models/select 的 403 分支）。
    scope: str = "self"
    # 自建接入（id="custom"）才用：OpenAI 兼容端点与模型名。
    # 显式给出即视为**改写**这份作用域的端点配置（与 api_key 一并加密落库）。
    base_url: str | None = None
    model: str | None = None


class UserCreateIn(BaseModel):
    """POST /users 的请求体（登记中控台用户）。"""

    user_id: str


class RunDeleteIn(BaseModel):
    """DELETE /runs 的请求体（批量删除会话）。

    为什么 id 走**请求体**而不是路径：路径只能装一个 id，而批量删除的语义就是"一批"。
    代价是网关那条按路径取 run_id 的归属授权在这条路由上**取不到任何 id**，
    所以归属校验必须由端点自己逐个做（见 `routes/runs.py`），不能指望中间件。
    """

    run_ids: list[str]


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
