"""出站限速与配额：给"Agent 主动往外发请求"这道闸。

为什么需要（与 `ratelimit.py` 是**相反方向**的两件事）：
  - `ratelimit.py` 管**入站**：保护你的服务不被客户端打垮。
  - 本模块管**出站**：保护外部世界、你的出口 IP 和你的钱包不被**你自己的 Agent** 打垮。

单个请求的边界（超时 10s / 响应体 200KB / 跳转 3 次）只界定"一次"，不界定"多少次"：
  - 模型一轮里决定抓 50 个链接 → 50 次独立工具调用，每次都合规，总量无人管；
  - 并发用户相乘 → 出站连接数打满本机 fd / 出口带宽，服务自己被拖慢；
  - 高频打同一站点 → 对方 429/403 或上 WAF，工具从此静默失败（比报错更难查）；
  - 若 provider 是**按次计费**的检索 API → 一次失控循环能把月度配额几分钟跑光。

四道闸，缺一道都留着口子：
  1. **全局速率**：每 window 秒最多 N 次出站（跨所有 host）。
  2. **单 host 速率**：对同一个站点保持礼貌，不把它打成 DoS 目标。
  3. **并发上限**：同时在飞的出站请求数（信号量，进程内）。
  4. **日配额**：一天总出站次数上限，防"跑光计费额度"。

计数走 `RateLimitStore`（见 `web/coordination.py`）——与入站限流共用同一套存储接缝，
所以**多副本下把 store 换成存储实现，限额才是全局的**（否则实际额度 ≈ 配置 × 副本数）。
并发信号量天然是进程内的，多副本下是"每副本各 N"（与熔断状态同理）。

**拒绝的语义**：不抛异常，返回一句可读原因（`[限流] ...`）。工具不该因为配额用尽
就把整个会话循环打崩——交回给模型，让它知道"现在不行"。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from warden_agent.core.settings import env_opt, env_str
from warden_agent.web.coordination import InProcessRateLimitStore, RateLimitStore

# 全局桶的键（所有 host 共用）
_GLOBAL_KEY = "outbound:global"


@dataclass(frozen=True)
class OutboundConfig:
    """出站闸门参数。`daily_quota=0` 表示不限日配额。"""

    max_requests: int = 120           # 全局：每 window_seconds 秒最多这么多次
    window_seconds: int = 60
    host_max_requests: int = 20       # 单 host：每 host_window_seconds 秒最多这么多次
    host_window_seconds: int = 60
    max_concurrency: int = 8          # 同时在飞的出站请求上限（进程内）
    daily_quota: int = 0              # 每日总出站次数上限；0 = 不限


@dataclass(frozen=True)
class OutboundDecision:
    """一次出站许可的裁决。`allowed=False` 时 `reason` 说明哪道闸拦下的。"""

    allowed: bool
    reason: str = ""
    retry_after: int = 0

    @property
    def denied(self) -> bool:
        return not self.allowed


class OutboundLimiter:
    """四道闸的出站限流器。用法：先 `acquire`，放行后必须 `release`。

        decision = limiter.acquire("example.com")
        if decision.denied:
            return f"[限流] {decision.reason}"
        try:
            ...发请求...
        finally:
            limiter.release()
    """

    def __init__(
        self,
        config: OutboundConfig | None = None,
        *,
        store: RateLimitStore | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.config = config or OutboundConfig()
        if self.config.max_concurrency <= 0:
            raise ValueError("max_concurrency 必须为正整数")
        self._store: RateLimitStore = store or InProcessRateLimitStore()
        self._clock = clock or time.monotonic
        self._semaphore = threading.BoundedSemaphore(self.config.max_concurrency)
        # 每个线程各自记录"我手上有没有槽位"。不能用实例级布尔：限流器会被多个线程共享，
        # 共享标志会让 A 线程的 release 去还 B 线程的槽位（或把信号量越还越多）。
        self._local = threading.local()

    # ---- 判定 ----
    def acquire(self, target: str) -> OutboundDecision:
        """申请一次出站许可。`target` 是目标 host（搜索等无 host 的场景给 provider 名）。

        放行时会占用一个并发槽位——调用方**必须**在 `finally` 里 `release()`。
        """
        cfg = self.config
        now = self._clock()

        count, start = self._store.hit(_GLOBAL_KEY, cfg.window_seconds, now)
        if count > cfg.max_requests:
            return OutboundDecision(
                False,
                f"出站总速率已达上限（每 {cfg.window_seconds} 秒 {cfg.max_requests} 次）",
                _retry_after(now, start, cfg.window_seconds),
            )

        host_key = f"outbound:host:{target}"
        count, start = self._store.hit(host_key, cfg.host_window_seconds, now)
        if count > cfg.host_max_requests:
            return OutboundDecision(
                False,
                f"对 {target} 的请求过于频繁"
                f"（每 {cfg.host_window_seconds} 秒 {cfg.host_max_requests} 次）",
                _retry_after(now, start, cfg.host_window_seconds),
            )

        if cfg.daily_quota > 0:
            count, _ = self._store.hit(f"outbound:quota:{_utc_day()}", 86_400, now)
            if count > cfg.daily_quota:
                return OutboundDecision(
                    False,
                    f"今日出站配额已用尽（{cfg.daily_quota} 次/日，UTC {_utc_day()} 后重置）",
                )

        # 并发槽位：拿不到就直接拒（不阻塞），避免把会话循环挂死在等锁上
        if not self._semaphore.acquire(blocking=False):
            return OutboundDecision(
                False,
                f"出站并发已达上限（{cfg.max_concurrency}）",
                retry_after=1,
            )
        self._local.held = True
        return OutboundDecision(True)

    def release(self) -> None:
        """归还并发槽位。未持有则忽略（幂等，不会把信号量越还越多）。"""
        if getattr(self._local, "held", False):
            self._local.held = False
            self._semaphore.release()

    @contextmanager
    def guard(self, target: str) -> Iterator[OutboundDecision]:
        """上下文管理器形式：放行则执行 `with` 体，拒绝则 `decision.denied` 为真。"""
        decision = self.acquire(target)
        try:
            yield decision
        finally:
            self.release()


def _retry_after(now: float, window_start: float, window_seconds: int) -> int:
    """固定窗口下还要等多久（秒，向上取整，至少 1）。"""
    return max(1, int(window_seconds - (now - window_start) + 0.999))


def _utc_day() -> str:
    """取当前 UTC 日期串，用于"同一天共用一个配额桶"。

    这里刻意用**墙钟**而不是限流器注入的 `monotonic` 时钟：日配额是运维语义，必须对齐
    自然日（跨重启、跨副本都要落在同一个桶里）；单调时钟的起点是实现细节，不能拿来分天。
    """
    return datetime.now(UTC).strftime("%Y-%m-%d")


def parse_outbound_limit(spec: str) -> tuple[int, int] | None:
    """解析 `次数/窗口秒数`（如 `120/60`）。`0`/`off` 返回 None（关闭全局速率闸）。"""
    text = (spec or "").strip()
    if not text or text.lower() in ("0", "off", "false", "no"):
        return None
    if "/" not in text:
        raise ValueError(
            f"WARDEN_OUTBOUND_LIMIT 格式错误：{spec!r}。应为 `次数/窗口秒数`，例如 120/60"
        )
    left, _, right = text.partition("/")
    if not left.strip().isdigit() or not right.strip().isdigit():
        raise ValueError(
            f"WARDEN_OUTBOUND_LIMIT 格式错误：{spec!r}。应为 `次数/窗口秒数`，例如 120/60"
        )
    return int(left), int(right)


def _non_negative_int(raw: str | None, *, name: str, default: int) -> int:
    """把环境变量原文解析成非负整数。空 = 用默认；格式错 = 启动时暴露（不静默用默认值）。

    刻意接收**原文**而不是 (env, name)：这样变量名在调用处是字面量，
    `tests/test_config_surface.py` 的 AST 守卫才能扫到它、并核对配置注册表。
    """
    text = (raw or "").strip()
    if not text:
        return default
    if not text.isdigit():
        raise ValueError(f"{name} 格式错误：{raw!r}。应为非负整数")
    return int(text)


def outbound_from_env(
    env: Mapping[str, str], *, store: RateLimitStore | None = None
) -> OutboundLimiter:
    """按环境变量造出站限流器（默认**开启**，参数保守）。

    默认开启的理由：单次请求的边界已经有了，但"总量无界"这口子没有任何理由默认敞着；
    配置项都在，要放宽直接调环境变量。日配额默认 0（不限）——硬性停机上限应当由运维
    显式决定，与"Agent 能不能出网"同一个取向。
    """
    global_spec = parse_outbound_limit(env_str("WARDEN_OUTBOUND_LIMIT", "", env))
    max_requests, window_seconds = global_spec or (120, 60)
    host_spec = parse_outbound_limit(env_str("WARDEN_OUTBOUND_HOST_LIMIT", "", env))
    host_max, host_window = host_spec or (20, 60)
    concurrency_raw = env_opt("WARDEN_OUTBOUND_MAX_CONCURRENCY", env)
    quota_raw = env_opt("WARDEN_OUTBOUND_DAILY_QUOTA", env)
    config = OutboundConfig(
        max_requests=max_requests,
        window_seconds=window_seconds,
        host_max_requests=host_max,
        host_window_seconds=host_window,
        max_concurrency=_non_negative_int(
            concurrency_raw, name="WARDEN_OUTBOUND_MAX_CONCURRENCY", default=8
        ),
        daily_quota=_non_negative_int(
            quota_raw, name="WARDEN_OUTBOUND_DAILY_QUOTA", default=0
        ),
    )
    return OutboundLimiter(config, store=store)
