"""健康/指标/审计/恢复/告警路由（从 `web/server.py` 机械拆分而来，handler 逐字照搬）。"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from warden_agent.core.metrics import metrics
from warden_agent.runtime.alerting import ALERT_LIST_LIMIT, stuck_awaiting_human
from warden_agent.store.sqlite import SqliteStore
from warden_agent.web.audit import AuditLogger, AuditStore
from warden_agent.web.auth import ApiKeyAuthenticator, TrustedCaller
from warden_agent.web.health import HealthResult, liveness, readiness
from warden_agent.web.http_kit import _checkpoint_store_for, _plan_to_dict


def register(
    app: FastAPI,
    *,
    _owner_of: Callable[[str], str | None],
    _owner_scope: Callable[[Request], str | None],
    audit: AuditLogger | None,
    audit_store: AuditStore | None,
    audit_unkeyed: bool,
    authenticator: ApiKeyAuthenticator | None,
    logger: logging.Logger,
    m_stuck: Any,
    store: SqliteStore,
) -> None:
    """注册 /health/live、/health/ready、/metrics、/audit、/recovery/plan、/alerts/stuck。"""

    # ---- 阶段13：健康检查（liveness / readiness）----
    @app.get("/health/live", include_in_schema=False)
    def health_live() -> dict[str, Any]:
        r = liveness()
        return {"status": r.status, "checks": r.checks}

    @app.get("/health/ready", include_in_schema=False)
    def health_ready() -> JSONResponse:
        r: HealthResult = readiness(store)
        checks = dict(r.checks)
        status = r.status
        if audit_unkeyed:
            # 审计开着却没有链密钥：能发现手改/删行，却挡不住会重算整条链的人。
            # 就绪探针如实降级，让编排/监控能看见，而不是等出事才发现“以为在防篡改”。
            checks["audit_chain"] = "unkeyed"
            status = "degraded"
        return JSONResponse(
            status_code=200 if status == "ok" else 503,
            content={"status": status, "checks": checks},
        )

    # ---- T8 可观测性：指标出口（Prometheus text，可被 Grafana 抓取）----
    # 挂起数的缓存：抓取时"现算"能保证数是对的（不漂移），但每次抓取都扫库在抓取密集时
    # 是浪费。这里加一个短 TTL 缓存：TTL 内复用上次结果，TTL 外重新扫描。
    # 取 15s 与 Prometheus 默认抓取间隔一致——**不会让"挂起数"比告警评估周期更旧**。
    _stuck_cache: dict[str, float] = {"at": -1e9, "value": 0.0}
    _STUCK_TTL_S = 15.0

    def _refresh_stuck_gauge() -> None:
        """把"挂太久的 Run 数"写进 gauge（带 TTL 缓存）。失败绝不让 /metrics 挂掉。

        为什么现算而不是在写入路径累加：累加式 gauge 在重启、多副本下都会漂移
        （每个副本只知道自己见过的那部分），而告警恰恰最怕"数不对"。抓取时对存储做一次
        只读扫描得到的是**当前真实值**；再加 TTL 缓存，避免"抓取越频繁、扫得越多"。

        ⚠️ 刷新失败时**保留上一次的值**，绝不写 0：写 0 等于把告警悄悄消掉
        （`warden_stuck_runs > 0` 立刻变假），那是比"指标空缺"严重得多的错。
        """
        now = time.monotonic()
        if now - _stuck_cache["at"] < _STUCK_TTL_S:
            m_stuck.set(_stuck_cache["value"], labels=("60m",))   # 缓存命中：不扫库
            return
        try:
            from warden_agent.runtime.alerting import stuck_awaiting_human

            cp_store = _checkpoint_store_for(store)
            if cp_store is None:
                return
            stuck = stuck_awaiting_human(store, cp_store, older_than_seconds=3600.0)
            value = float(len(stuck))
        except Exception:  # noqa: BLE001 - 指标刷新失败不能影响 /metrics 本身
            logger.debug("刷新 warden_stuck_runs 失败（保留上次值，不影响 /metrics）",
                         exc_info=True)
            return
        _stuck_cache["at"] = now
        _stuck_cache["value"] = value
        m_stuck.set(value, labels=("60m",))

    @app.get("/metrics", include_in_schema=False)
    def metrics_view(request: Request) -> PlainTextResponse:
        """Prometheus 指标出口。**仅管理员**可读。

        为什么收紧：指标是**运维面**（全局请求量/错误率/挂起数/各 path 延时），不是用户数据面。
        此前只要求认证，viewer（只读角色）也能读——与"只读=能看用户数据但不能动"的契约不符。
        本地匿名开发模式（未开启鉴权）保持开放，行为不变。
        """
        caller: TrustedCaller | None = getattr(request.state, "caller", None)
        if authenticator is not None and (caller is None or not caller.is_admin):
            raise HTTPException(status_code=403, detail="查看指标需要管理员角色")
        _refresh_stuck_gauge()
        return PlainTextResponse(metrics().render())

    # ---- 阶段13：审计查询 ----
    @app.get("/audit")
    def audit_view(request: Request) -> list[dict[str, Any]]:
        """返回最近的审计轨迹（含 correlation_id / 调用者 / 操作 / 结果状态）。

        认证模式下只返回**调用者自己**的记录。为什么是"按人"而不是"按租户"：
        本项目的多用户配置（`WARDEN_API_KEYS`）默认共用同一个 `WARDEN_TENANT`（默认 `local`），
        即**租户 ≠ 用户**——只按 tenant 过滤，等于把同租户其他人的操作账本（含 run_id、
        身份、访问路径）交给任意一个认证用户，既能窥探又能枚举账号。
        这里与 `/runs`、`/approvals` 保持同一口径：按调用者收敛。
        **管理员例外**：看租户全部（运维需要全局视图来判断"是不是有人在乱用"）。
        """
        if audit is None:
            raise HTTPException(status_code=404, detail="未开启审计(audit_store=None)")
        caller: TrustedCaller | None = getattr(request.state, "caller", None)
        tenant = caller.tenant_id if caller is not None else None
        # 管理员看租户全部；普通用户只看自己（见 _owner_scope）
        principal = _owner_scope(request)
        records = audit_store.query(  # type: ignore[union-attr]
            limit=200, tenant_id=tenant, principal_id=principal
        )
        return [r.to_dict() for r in records]

    # ---- 跨 Run 恢复：崩溃/重启后"哪些该续、哪些该重试、哪些该等人" ----
    @app.get("/recovery/plan")
    def recovery_plan(request: Request, run_id: str | None = None) -> dict[str, Any]:
        """读取全部存档点，产出恢复计划（只读判断，不执行任何动作）。

        用途：进程崩溃/重启后，工作进程或运维据此决定续跑/重试/等待人工，
        而不是"全部从头再跑一遍"。`RecoveryController` 本身只判断不执行，
        真正的执行由调用方负责。认证模式下只暴露调用者自己名下的 Run。

        带 `?run_id=` 时**额外直接回答"这一次能不能续"**（控制台/值班人按单个 Run 问，
        不必自己从四组列表里反推）：多返回 `run_id` / `decision` / `resumable` 三个字段。
        `resumable` 就是 `RecoveryController.can_resume()` 的结论——"有存档点、且没被判失败、
        也没在等人工"才算能续（`await_human` 与 `retry` 都算不能自动续）。
        别人的 Run 一律 **404**（与其它按归属收敛的端点一致，不泄漏"存在与否"）。
        """
        from warden_agent.runtime.recovery import RecoveryController

        cp_store = _checkpoint_store_for(store)
        if cp_store is None:
            raise HTTPException(status_code=501, detail="当前存储不支持存档点")
        controller = RecoveryController(cp_store)
        plan = controller.plan()
        owner = _owner_scope(request)   # 管理员 → None（全局视图）
        payload = _plan_to_dict(plan, owner, _owner_of)
        if run_id is not None:
            if owner is not None and _owner_of(run_id) != owner:
                raise HTTPException(status_code=404, detail="没有这个 Run")
            payload["run_id"] = run_id
            payload["decision"] = plan.action_for(run_id)
            payload["resumable"] = controller.can_resume(run_id)
        return payload

    @app.get("/alerts/stuck")
    def alerts_stuck(
        request: Request, older_than_min: float = 60.0, limit: int = ALERT_LIST_LIMIT
    ) -> dict[str, Any]:
        """**等待人工处理超时**的 Run —— 给监控/告警用。

        为什么需要它：Run 进入 `WAITING_APPROVAL` 之后不会有任何动静（没有超时、没有重试、
        没有通知），线上没人盯就一直挂着。这个端点把"挂太久了"变成可被定时抓取的事实：
        返回非空列表就该有人去看。认证模式下只暴露调用者自己名下的 Run。

        体积有上界（第五十七批实测：5000 个 Run 的完整响应是 1.29MB，
        而监控每几分钟抓一次）：`limit` 默认只返回最该先看的 `ALERT_LIST_LIMIT` 个，
        `limit=0` = 不设上限（要全量就明说）。`count` 始终是**真实总数**，
        另外给出 `listed` / `omitted` / `truncated`——**少给了必须能看出来**，
        不能像第五十二批那样"静默少跑 40 条"还以为全绿。

        ⚠️ 口径："等了多久"用 Run 的最后活动时间近似（不是"进入等待那一刻"），所以只会**低估**、
        不会虚报——"报了警"是可信的；"没报警"不等于一定没挂久。
        **管理员**看全部（值班人要知道"整个系统有没有人卡着"）。
        """
        cp_store = _checkpoint_store_for(store)
        if cp_store is None:
            raise HTTPException(status_code=501, detail="当前存储不支持存档点")
        owner = _owner_scope(request)   # 管理员 → None（全局视图）
        stuck = stuck_awaiting_human(
            store, cp_store, older_than_seconds=older_than_min * 60.0, owner=owner
        )
        shown = stuck if limit <= 0 else stuck[:limit]
        return {
            "older_than_min": older_than_min,
            "count": len(stuck),
            "listed": len(shown),
            "omitted": len(stuck) - len(shown),
            "truncated": len(shown) < len(stuck),
            "runs": [
                {
                    "run_id": s.run_id,
                    "status": s.status,
                    "waiting_seconds": s.waiting_seconds,
                    "detail": s.detail,
                    # 时长算自哪里：approval=精确（进入等待审批的时刻）；
                    # last_activity=近似（只会低估）；unknown=拿不到
                    "source": s.source,
                }
                for s in shown
            ],
        }
