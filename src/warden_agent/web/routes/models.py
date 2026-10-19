"""模型目录与切换路由（从 `web/server.py` 机械拆分而来，handler 逐字照搬）。"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
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
from warden_agent.web.schemas import ModelProbeIn, ModelSelectIn
from warden_agent.web.session_registry import SessionRegistry

# 探测结果的缓存时长：界面轮询很勤（5s），不缓存就等于每 5 秒把一个请求打到厂商那儿去。
PROBE_TTL_S = 60.0


def _public_probe(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    """把内部缓存条目变成对外形状（去掉 `_mono` 这种进程内计时器）。"""
    if entry is None:
        return None
    return {k: v for k, v in entry.items() if not k.startswith("_")}


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

    # 探测结果按 (调用者作用域, 模型 id) 缓存：不同 key 的可用性本来就可能不一样，
    # 所以不跨作用域共享结论。
    #
    # 已知取舍：这份缓存是**进程内**的。多 worker / 多副本部署下，`last_probe` 可能
    # “在这台是通的、在另一台还是 null”，进程重启后也会丢失——它只是界面上的参考信息，
    # 不进任何判据（真正的证据是 `/models/probe` 当场那次结果）。要强一致就得塞进
    # store 并带上 `checked_at` 做新鲜度判断，那属于另一批事，这里先把边界写清。
    probes: dict[tuple[str, str], dict[str, Any]] = {}

    @app.get("/models")
    def models_view(request: Request) -> dict[str, Any]:
        """可用模型列表 + 当前使用的模型。

        `configured` 按调用者视角计算：自己导入过、或部署级配过，都算已配置。

        `last_probe` 是**上次主动探测**的结果（没有就是 `null`）：
        `configured=true` 只说明「有 key」，不说明「provider 通」——这两件事必须分开说，
        否则界面会把「有 key」展示成「可用」，用户发出第一条消息才被告知挂了（笔记 95）。
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
                    "last_probe": _public_probe(probes.get((scope, mid))),
                }
                for mid, info in model_catalog.items()
            ],
        }

    @app.post("/models/probe")
    def models_probe(body: ModelProbeIn, request: Request) -> dict[str, Any]:
        """主动探一次「这个 provider 到底通不通」。

        为什么要有这个接口：`/models` 只能回答「有没有 key」。provider 宕机 / 网络不通 /
        base_url 写错 / key 被吊销，这四种在「有 key」这件事上长得一模一样，
        而用户在界面上看到的却是「可用」。所以给一个能当场验证、并把**失败原因**原样带出去的接口。

        成本约束：只调 `GET /models`（不消耗 token），且结果缓存 `PROBE_TTL_S` 秒；
        `force=true` 才重新探（避免界面轮询把厂商打爆，也避免误点重复计费）。
        """
        scope = _credential_scope(request)
        info = model_catalog.get(body.id)
        if info is None:
            raise HTTPException(status_code=404, detail=f"未知模型: {body.id}")
        key = _registered_model_key(broker, body.id, scope)
        if info["needs_key"] and not key:
            raise HTTPException(
                status_code=400, detail=f"{info['name']} 需要先导入 API Key"
            )

        cache_key = (scope, body.id)
        cached = probes.get(cache_key)
        now = time.monotonic()
        if cached is not None and not body.force and now - cached["_mono"] < PROBE_TTL_S:
            public = _public_probe(cached)
            assert public is not None  # cached 非 None ⇒ 这里必非 None
            return public | {"cached": True}

        started = now
        try:
            info["probe"](key)
            result: dict[str, Any] = {"id": body.id, "ok": True, "reason": None}
        except Exception as e:  # noqa: BLE001 —— 探不通就是探不通，原因原样报
            # 抹两轮，因为 key 有***不经过本模块导入***的来路：启动时从 vault 恢复、运维直接
            # `broker.register(...)`——这些 key 从未进过进程级 redactor（那里只有部署级那把
            # 与 `/models/select` 当场导入的那把）。而 openai/httpx 的某些异常会把
            # `Authorization: Bearer sk-...` 拼进消息 ⇒ 明文会经 `reason` 端到端跑进浏览器。
            # （2026-10-19 复审发现；反向对照已验证：去掉这行，直接把 key 塞进 broker 的
            # 那条用例立刻变红。）
            reason = redactor.redact(f"{type(e).__name__}: {e}")
            if key:
                reason = SecretRedactor([key]).redact(reason)
            result = {
                "id": body.id,
                "ok": False,
                "reason": reason,
            }
        result.update(
            {
                "latency_ms": int((time.monotonic() - started) * 1000),
                "checked_at": datetime.now(UTC).isoformat(),
                "cached": False,
            }
        )
        probes[cache_key] = {**result, "_mono": time.monotonic()}
        return result

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
