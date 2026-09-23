"""维护清扫（保留策略）：把只会变大的表按策略清掉，别让库无界增长。

为什么需要（交付级审计发现的真实缺口）：多张表**只增不减**——
幂等表的**成功响应快照**永不删除、共享事件表无上限、限流计数行永不消失、
过期记忆只标 TOMBSTONED 不物理删。SQLite 单副本短期看不出问题，多副本长跑一定撑大。

`sweep()` 一次做三件事（事件表由 `append_event(keep=)` 在写入时裁剪，不在这里全表扫）：
  1. **幂等表**：删掉 TTL 之前创建的行（`WARDEN_IDEMPOTENCY_TTL_S`，默认 1 天）；
  2. **限流计数**：删掉窗口早已结束的行（无窗口信息，用足够大的年龄阈值兜底）；
  3. **记忆**：物理删除过期 / 墓碑行（需传入 memory_repository）。

三重取向：**尽力而为**（某一步失败不影响其余）、**可观测**（返回各步计数并打日志）、
**幂等**（重复跑无害）。运维可定时跑（`run_server` 的后台清扫线程，或 `warden prune`）。
"""

from __future__ import annotations

import datetime as _dt
import logging
import threading
from typing import Any

logger = logging.getLogger("warden.maintenance")

DEFAULT_IDEMPOTENCY_TTL_S = 86400.0     # 幂等记录保留 1 天
DEFAULT_RATE_LIMIT_MAX_AGE_S = 86400.0  # 限流计数行最多留 1 天（窗口最长也是按天级）

# 卡死 Run 数的告警阈值与 gauge 标签（与 web/server.py 的 warden_stuck_runs 对齐）
DEFAULT_STUCK_SECONDS = 3600.0
_STUCK_LABEL = "60m"


def _sweep_failure(task: str) -> None:
    """记录一次清扫步骤失败（带 task 标签）。

    此前清扫失败只有 warning 日志、没有指标——库在悄悄无界增长，告警规则也接不上。
    现在失败可见（`warden_maintenance_sweep_failures_total{task=...}`）。
    埋点失败绝不抛异常。
    """
    try:
        from warden_agent.core.metrics import metrics

        metrics().counter(
            "warden_maintenance_sweep_failures_total",
            "维护清扫各步骤失败次数（失败则库可能无界增长，需告警）",
            ["task"],
        ).inc(labels=(task,))
    except Exception:  # noqa: BLE001 - 埋点不能影响清扫流程
        pass


def set_stuck_gauge(count: int, *, older_than: str = _STUCK_LABEL) -> None:
    """把"当前卡死 Run 数"写入 `warden_stuck_runs`；**无卡死也写 0**，保证序列始终存在。

    为什么需要：`warden_stuck_runs` 此前只在 /metrics 被抓取时现算（scrape 刷新），
    若无人抓取（或抓取前）序列根本不存在，`> 0` 的告警规则就无从评估。
    在清扫线程里每轮写一次（含 0），序列就始终存在。
    """
    try:
        from warden_agent.core.metrics import metrics

        metrics().gauge(
            "warden_stuck_runs", "等待人工处理超过阈值的 Run 数", ["older_than"]
        ).set(float(count), labels=(older_than,))
    except Exception:  # noqa: BLE001 - 埋点不能影响清扫流程
        pass


def refresh_stuck_gauge(
    store: object,
    checkpoints: object,
    *,
    older_than_seconds: float = DEFAULT_STUCK_SECONDS,
    older_than: str = _STUCK_LABEL,
) -> int:
    """计算卡死 Run 数并写入 gauge，返回该数。**计算失败时不动 gauge**（保留上次值，
    绝不写 0——写 0 等于把告警悄悄消掉）。"""
    from warden_agent.runtime.alerting import stuck_awaiting_human

    count = len(stuck_awaiting_human(
        store, checkpoints, older_than_seconds=older_than_seconds,
    ))
    set_stuck_gauge(count, older_than=older_than)
    return count


def sweep(
    store: object,
    *,
    idempotency_ttl_s: float = DEFAULT_IDEMPOTENCY_TTL_S,
    rate_limit_max_age_s: float = DEFAULT_RATE_LIMIT_MAX_AGE_S,
    memory_repository: Any = None,
    now: _dt.datetime | None = None,
) -> dict[str, int]:
    """按保留策略清扫一次，返回各表删除条数。任何一步失败都只记日志、不影响其余。"""
    moment = now or _dt.datetime.now(_dt.UTC)
    counts = {"idempotency": 0, "rate_limits": 0, "memories": 0}

    purge_idem = getattr(store, "purge_expired_idempotency", None)
    if callable(purge_idem):
        try:
            before_iso = (moment - _dt.timedelta(seconds=idempotency_ttl_s)).isoformat()
            counts["idempotency"] = int(purge_idem(before_iso))
        except Exception:  # noqa: BLE001 - 清扫失败不能拖垮调用方
            logger.warning("清扫幂等表失败（忽略，继续其它）", exc_info=True)
            _sweep_failure("idempotency")

    purge_rl = getattr(store, "purge_stale_rate_limits", None)
    if callable(purge_rl):
        try:
            before_epoch = (moment - _dt.timedelta(seconds=rate_limit_max_age_s)).timestamp()
            counts["rate_limits"] = int(purge_rl(before_epoch))
        except Exception:  # noqa: BLE001
            logger.warning("清扫限流表失败（忽略，继续其它）", exc_info=True)
            _sweep_failure("rate_limits")

    purge_mem = getattr(memory_repository, "purge_expired", None)
    if callable(purge_mem):
        try:
            counts["memories"] = int(purge_mem(moment))
        except Exception:  # noqa: BLE001
            logger.warning("清扫记忆表失败（忽略，继续其它）", exc_info=True)
            _sweep_failure("memories")

    return counts


class MaintenanceSweeper:
    """后台定时清扫线程（daemon）。`interval_s<=0` 表示不启动。"""

    def __init__(
        self,
        store: object,
        *,
        interval_s: float,
        memory_repository: Any = None,
        idempotency_ttl_s: float = DEFAULT_IDEMPOTENCY_TTL_S,
        rate_limit_max_age_s: float = DEFAULT_RATE_LIMIT_MAX_AGE_S,
        checkpoint_store: Any = None,
        stuck_older_than_s: float = DEFAULT_STUCK_SECONDS,
    ) -> None:
        self._store = store
        self._interval = interval_s
        self._memory = memory_repository
        self._ttl = idempotency_ttl_s
        self._max_age = rate_limit_max_age_s
        # 【O10】可选的存档点存储：给了就每轮刷新 warden_stuck_runs（无卡死也写 0），
        # 让告警序列始终存在，而不是等 /metrics 被抓取时才出现。
        # 未显式传入时，从 store 自动推导（SqliteStore/PG 都支持存档点）——
        # 这样产品路径无需额外接线即生效。
        if checkpoint_store is None:
            from warden_agent.runtime.checkpoint import checkpoint_store_for

            checkpoint_store = checkpoint_store_for(store)
        self._checkpoints = checkpoint_store
        self._stuck_older_than = stuck_older_than_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._interval <= 0:
            return
        self._thread = threading.Thread(
            target=self._loop, name="warden-maintenance", daemon=True
        )
        self._thread.start()

    @property
    def interval(self) -> float:
        """清扫间隔秒（<=0 表示未启用；供启动日志展示）。"""
        return self._interval

    def _loop(self) -> None:
        # 先等一个间隔再扫：启动瞬间没必要立刻全表扫一遍。
        while not self._stop.wait(self._interval):
            try:
                counts = sweep(
                    self._store,
                    idempotency_ttl_s=self._ttl,
                    rate_limit_max_age_s=self._max_age,
                    memory_repository=self._memory,
                )
                if any(counts.values()):
                    logger.info("维护清扫：%s", counts)
                # 【O10】顺手刷新卡死 Run 数：无卡死也写 0，保证序列始终存在。
                if self._checkpoints is not None:
                    try:
                        refresh_stuck_gauge(
                            self._store, self._checkpoints,
                            older_than_seconds=self._stuck_older_than,
                        )
                    except Exception:  # noqa: BLE001 - 刷新失败保留上次值，绝不写 0
                        logger.debug("刷新 warden_stuck_runs 失败（保留上次值）",
                                     exc_info=True)
            except Exception:  # noqa: BLE001 - 后台线程绝不能因异常退出
                logger.warning("维护清扫异常（忽略，下轮再试）", exc_info=True)

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
