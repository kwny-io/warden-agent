"""请求限流：给 HTTP 入口一道"别把服务打垮"的闸。

为什么需要：审批闸门保护的是"Agent 能干什么"，限流保护的是"服务还活着"。
没有它，一把 key 的客户端（或一个失控的批量脚本）就能把单节点服务打满，
连 `/health/ready` 都探不到——可用性问题同样会让 Agent 不可交付。

实现选择：**固定窗口计数器**，无第三方依赖（与项目"零重依赖"的取向一致）。
   - 每个 key 一个窗口：窗口内计数 ≤ 上限就放行，否则拒绝并给出 Retry-After。
   - key 取"调用者身份"（认证时）或来源 IP（匿名时）。
   - 计数存在 `RateLimitStore` 里（见 `web/coordination.py`）：
     **单副本**用进程内实现；**多副本**换成存储实现，否则实际总限额会翻倍。

**边界**：进程内计数在多副本下每个副本各算一份（实际限额 ≈ 配置值 × 副本数）；
要全局限额，把 `store` 换成 `SqlRateLimitStore`（`WARDEN_SHARED_STATE=1`）。详见
`docs/deployment-boundaries.md`。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from warden_agent.core.settings import env_opt
from warden_agent.web.coordination import InProcessRateLimitStore, RateLimitStore


@dataclass(frozen=True)
class RateLimitConfig:
    """限流配置：`window_seconds` 秒内最多 `max_requests` 次。"""

    max_requests: int
    window_seconds: int


class RateLimiter:
    """固定窗口限流器。计数托管给 `RateLimitStore`（进程内 or 存储共享）。"""

    def __init__(
        self,
        max_requests: int,
        window_seconds: int,
        *,
        store: RateLimitStore | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if max_requests <= 0 or window_seconds <= 0:
            raise ValueError("限流参数必须为正整数")
        self.config = RateLimitConfig(max_requests, window_seconds)
        self._clock = clock or time.monotonic
        self._store: RateLimitStore = store or InProcessRateLimitStore()

    def check(self, key: str) -> tuple[bool, int]:
        """主路径：一次调用返回 `(是否放行, 被拒时还需等待秒数)`。

        计数由 store 原子累加（多副本下同样原子）。**拒绝也计数**——这是固定窗口的
        标准语义，否则持续打满的客户端永远落在窗口外沿、窗口不会滚动。
        """
        count, start = self._store.hit(key, self.config.window_seconds, self._clock())
        if count <= self.config.max_requests:
            return True, 0
        remaining = self.config.window_seconds - (self._clock() - start)
        return False, max(1, int(remaining + 0.999))

    def allow(self, key: str) -> bool:
        """本次请求是否放行。"""
        return self.check(key)[0]

    def retry_after(self, key: str) -> int:
        """被拒时的等待上界（秒）。

        只读估算、不消耗计数：真正精确的值请用 `check()` 的返回值——
        单独调本方法拿不到"本窗口起点"，返回窗口长度作为保守上界。
        """
        return self.config.window_seconds


def parse_rate_limit(
    spec: str, *, store: RateLimitStore | None = None
) -> RateLimiter | None:
    """解析 `每窗次数/窗口秒数`，例如 `600/60`。

    返回 None 表示不限流（`0` / `off`）。格式错误抛 ValueError——
    与其悄悄按默认值跑，不如让配置错误在启动时暴露。
    """
    text = (spec or "").strip()
    if not text or text.lower() in ("0", "off", "false", "no"):
        return None
    if "/" not in text:
        raise ValueError(
            f"WARDEN_RATE_LIMIT 格式错误：{spec!r}。应为 `次数/窗口秒数`，例如 600/60"
        )
    left, _, right = text.partition("/")
    if not left.strip().isdigit() or not right.strip().isdigit():
        raise ValueError(
            f"WARDEN_RATE_LIMIT 格式错误：{spec!r}。应为 `次数/窗口秒数`，例如 600/60"
        )
    return RateLimiter(int(left), int(right), store=store)


def client_key(
    caller_id: str | None, client_host: str | None, fallback: str = "anonymous"
) -> str:
    """限流桶的键：认证了就按调用者，否则按来源 IP。"""
    if caller_id:
        return f"caller:{caller_id}"
    if client_host:
        return f"ip:{client_host}"
    return fallback


def limiter_from_env(
    env: Mapping[str, str], *, store: RateLimitStore | None = None
) -> RateLimiter | None:
    """按环境变量造限流器。`WARDEN_RATE_LIMIT=600/60`；不设走默认，`0` 关闭。"""
    raw = env_opt("WARDEN_RATE_LIMIT", env)
    if raw is None:
        return RateLimiter(600, 60, store=store)  # 生产默认：每调用者每分钟 600 次
    return parse_rate_limit(raw, store=store)
