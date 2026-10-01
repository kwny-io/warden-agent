"""模型目录与切换路由（从 `web/server.py` 机械拆分而来，handler 逐字照搬）。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from warden_agent.credential.broker import CredentialBroker, SecretRedactor
from warden_agent.web.auth import TrustedCaller
from warden_agent.web.http_kit import (
    _credential_scope,
    _has_model_key,
    _model_key_name,
    _registered_model_key,
)
from warden_agent.web.schemas import ModelSelectIn
from warden_agent.web.session_registry import SessionRegistry


def register(
    app: FastAPI,
    *,
    _identity: Callable[[Request, str | None], str],
    broker: CredentialBroker,
    current_model_id: str,
    model_catalog: dict[str, dict[str, Any]],
    registry: SessionRegistry,
    redactor: SecretRedactor,
) -> None:
    """注册 /models 与 /models/select（注册顺序与拆分前一致：/models 在前）。"""

    @app.get("/models")
    def models_view(request: Request) -> dict[str, Any]:
        """可用模型列表 + 当前使用的模型。

        `configured` 按调用者视角计算：自己导入过、或部署级配过，都算已配置。
        """
        scope = _credential_scope(request)
        owner = _identity(request, None)
        return {
            # 当前模型按**调用者自己的选择**报（没选过则显示部署默认）
            "current": registry.model_for(owner)[0] or current_model_id,
            "models": [
                {
                    "id": mid,
                    "name": info["name"],
                    "needs_key": info["needs_key"],
                    "configured": (not info["needs_key"])
                    or _has_model_key(broker, mid, scope),
                }
                for mid, info in model_catalog.items()
            ],
        }

    @app.post("/models/select")
    def models_select(body: ModelSelectIn, request: Request) -> dict[str, Any]:
        """切换模型；带 api_key 视为"导入"（凭凭证 broker 加密落库，重启后仍在）。

        导入的 key 记在**调用者自己的作用域**下：同租户的其他用户读不到、用不了；
        部署级（启动配置）的那把则全体可见。

        **切换只作用于调用者自己**：否则任一认证用户就能把所有人的对话切成离线假模型
        （跨租户 DoS），或切到用自己的 key 计费（把别人的额度记到自己头上）。
        """
        scope = _credential_scope(request)
        info = model_catalog.get(body.id)
        if info is None:
            raise HTTPException(status_code=404, detail=f"未知模型: {body.id}")
        key = body.api_key or _registered_model_key(broker, body.id, scope)
        if info["needs_key"] and not key:
            raise HTTPException(
                status_code=400, detail=f"{info['name']} 需要先导入 API Key"
            )
        owner = _identity(request, None)
        caller: TrustedCaller | None = getattr(request.state, "caller", None)
        if body.scope not in ("self", "deployment"):
            raise HTTPException(status_code=400, detail="scope 只能是 self 或 deployment")
        if body.scope == "deployment":
            # 切部署默认模型影响**所有人**（包括没自选过的会话）——这是运维动作，
            # 只有管理员能做。普通用户仍可切"自己的"（scope=self，默认）。
            if caller is None or not caller.is_admin:
                raise HTTPException(
                    status_code=403,
                    detail="切换部署级默认模型需要管理员角色（普通用户可用 scope=self 切自己的）",
                )
            # 部署默认 = owner=None（所有会话，且成为新会话的默认）
            registry.set_model(info["make"](key), model_id=body.id, owner=None)
        else:
            registry.set_model(info["make"](key), model_id=body.id, owner=owner)
        if body.api_key:
            broker.register(_model_key_name(body.id), {"api_key": body.api_key}, scope)
            redactor.add(body.api_key)
        return {"ok": True, "current": registry.model_for(owner)[0] or body.id,
                "scope": body.scope}
