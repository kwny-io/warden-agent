"""工具调用稳定性层 —— 超时 / 指数退避重试 / 降级兜底，一条全链路稳定性管线。

背景（为什么需要它）：
  工具一多、一接真实外部依赖，就免不了瞬时故障：网络抖动、上游限流、接口偶发 5xx、
  某个工具卡死不动。如果 loop 对这些毫无防御，后果是：
    - 一个卡死的工具会**同步卡住整个 Agent 循环**（普通函数工具在进程序里同步跑，没有超时）;
    - 瞬时故障一重试就成功，但**没退避**的话会紧密重试、放大对上游的冲击;
    - 重试都失败时没有兜底，只能把错误甩给模型，甚至崩溃。

出色做法（本模块落地的）：
  一条"稳定执行"的统一管线，接在"具体执行工具"这一层：
    1. **超时**：给工具调用设一个硬时限，超时就返回"超时"信号，**绝不让 loop 卡死**。
    2. **指数退避重试**：瞬时故障（超时/连接类）自动按指数退避重试 `base*2^(n-1)`，
       有上限、不紧密重试，扛住限流/抖动。
    3. **降级兜底**：重试耗尽且配了 `fallback` 时，返回一条降级结果（带 [降级] 标记），
       而不是崩溃 / 甩错误给模型。

设计要点（对齐项目现有约定）：
  - **结果不是异常**：沿用 `execution/broker.py` 的"结果带标志"哲学——单次尝试返回
    `_AttemptOutcome`（result / exc / timed_out），而不是每类错误抛一种异常。loop 拿它
    组装 `(result, error)`，和 `_safe_execute` 的契约一致，下游零改动。
  - **可重试信号复用 `ToolSpec.pure`**：pure=True（无副作用）的工具失败可放心重试；
    非 pure 只对 `retry_on_errors` 里的瞬时错误重试（避免重放有副作用的操作）。
  - **默认关闭、向后兼容**：`StabilityConfig()` 全是"不超时(0)/不重试(1)/无退避"，
    不配就不会改变原有行为；现有测试零影响。
  - **超时实现说明**：真函数工具卡死时，进程内线程无法被"强杀"。我们用一个**工作线程 +
    deadline** 执行：超时则立刻返回"超时"信号并把控制权交还 loop（looop 不再卡死），
    被卡的工作线程退居后台、不再等待。对 `pure` 工具（无副作用）这完全干净；对非 pure
    工具这也优先保证"循环不悬挂"。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from warden_agent.tool.catalog import ToolSpec

# 降级结果的统一前缀（与 multiagent 的 [降级] 词汇一致，让下游/模型一眼可辨）
_DEGRADED_PREFIX = "[降级]"
_CIRCUIT_PREFIX = "[熔断]"

_TIMEOUT_MSG = "TimeoutError: 工具执行超时"
_CIRCUIT_MSG = "工具连续失败，熔断保护，暂不调用"


@dataclass
class StabilityConfig:
    """工具调用稳定性层的配置（全部默认"关闭"，配了才生效）。"""

    timeout_seconds: float = 0                                # 0=不设超时
    max_attempts: int = 1                                     # 1=不做自动重试
    backoff_base: float = 0.5   # 指数退避底数:第 n 次重试等 base*2^(n-1)
    backoff_max: float = 8.0                                  # 退避上限，防无限拉长
    retry_on_errors: tuple[type[BaseException], ...] = (
        TimeoutError, ConnectionError, OSError,
    )                                          # 命中这些瞬时错误才重试
    retryable_pure: bool = True                               # pure 工具失败额外允许重试
    fallback: Callable[[], Any] | None = None                 # 重试耗尽后的显式降级兜底
    circuit_threshold: int = 0                                # 熔断：连续失败 N 次触发短路(0=关)
    circuit_cooldown: float = 0.0                             # 熔断持续秒数，过后半开试一次


@dataclass
class _AttemptOutcome:
    """单次工具调用的结果（沿用 broker 的"结果带标志"哲学）。"""

    result: Any = None
    exc: BaseException | None = None
    timed_out: bool = False

    @property
    def error_str(self) -> str | None:
        if self.timed_out:
            return _TIMEOUT_MSG
        if self.exc is not None:
            return f"{type(self.exc).__name__}: {self.exc}"
        return None


@dataclass
class StableResult:
    """一次"稳定执行"的最终结论：要么结果，要么错误；附上尝试次数/是否降级。"""

    result: Any = None
    error: str | None = None
    attempts: int = 1
    degraded: bool = False
    timed_out: bool = False


class StableToolExecutor:
    """把"调用一个工具"变成"稳定地调用一个工具"：超时 + 退避重试 + 降级 + 熔断。"""

    def __init__(self, config: StabilityConfig | None = None) -> None:
        self.config = config or StabilityConfig()
        # 熔断状态（per-tool）：工具名 → 连续失败计数 / 短路到期时间 / 半开标记
        self._fail_counts: dict[str, int] = {}
        self._open_until: dict[str, float] = {}
        self._half_open: dict[str, bool] = {}

    # ---- 对外入口：稳定执行一个工具 ----

    def _reset(self, name: str) -> None:
        """工具成功后清空该工具的熔断状态（关闭短路）。"""
        self._fail_counts.pop(name, None)
        self._open_until.pop(name, None)
        self._half_open.pop(name, None)

    def _record_failure(self, name: str) -> None:
        """工具最终失败：计数 +1，达到阈值则打开短路。"""
        cfg = self.config
        if cfg.circuit_threshold <= 0:
            return
        n = self._fail_counts.get(name, 0) + 1
        if self._half_open.pop(name, False):
            # 半开试探失败 → 双倍惩罚：直接重新打开短路（重计短路时长）
            n = cfg.circuit_threshold
        if n >= cfg.circuit_threshold:
            self._open_until[name] = time.monotonic() + cfg.circuit_cooldown
            self._fail_counts[name] = 0  # 短路期间计数重置，半开后重新数
        else:
            self._fail_counts[name] = n

    def _is_open(self, name: str) -> bool:
        """熔断是否打开（在短路期内且未到半开试探时刻）。"""
        if self.config.circuit_threshold <= 0:
            return False
        until = self._open_until.get(name)
        if until is None:
            return False
        if time.monotonic() < until:
            return True  # 短路期内
        # 已过冷却期：进入半开（本次调用作为试探）
        self._open_until.pop(name, None)
        self._half_open[name] = True
        return False

    def execute(self, spec: ToolSpec, arguments: dict[str, Any]) -> StableResult:
        """稳定执行 `spec.function(**arguments)`，返回 StableResult。

        - 超时：单次尝试有硬时限，超时返回超时信号，不卡死调用方。
        - 重试：失败按策略（瞬时错误 或 pure）指数退避重试，最多 max_attempts。
        - 降级：耗尽且配了 fallback → 返回 [降级] 结果；否则返回最后错误。
        - 熔断：连续失败达到阈值 → 短路期内直接返回 [熔断]，不调、不重试；冷却后半开试一次。
        """
        fn = getattr(spec, "function", None)
        is_pure = bool(getattr(spec, "pure", False))
        name = spec.name
        # 【熔断】短路期内：跳过调用和重试，直接返回熔断信号（走降级语义）
        if self._is_open(name):
            return StableResult(
                result=f"{_CIRCUIT_PREFIX}{_CIRCUIT_MSG}（{name}）",
                degraded=True, error=f"{_CIRCUIT_MSG}",
            )

        if fn is None:
            return StableResult(error=f"{type(spec).__name__}: 工具不可执行")

        result = self._run_on_failure(name, fn, arguments, is_pure)
        if result.error is not None or result.degraded:
            # 真实工具未成功（硬错误 或 降级兜底）→ 计入熔断的"连续失败"
            self._record_failure(name)
        else:
            self._reset(name)  # 真实工具成功 → 关闭短路
        return result

    def _run_on_failure(self, name: str, fn: Callable[..., Any],
                        arguments: dict[str, Any], is_pure: bool) -> StableResult:
        """执行 + 重试/降级（不含熔断裁决，熔断由 execute 统一管）。"""
        cfg = self.config
        if cfg.max_attempts <= 1:
            out = self._run_once(fn, arguments)
            return StableResult(
                result=out.result, error=out.error_str, timed_out=out.timed_out,
            )

        last: _AttemptOutcome | None = None
        attempts = 0
        for attempt in range(cfg.max_attempts):
            attempts += 1
            out = self._run_once(fn, arguments)
            if out.error_str is None:
                return StableResult(result=out.result, attempts=attempts)
            last = out
            if attempt < cfg.max_attempts - 1 and self._should_retry(out, is_pure):
                delay = min(cfg.backoff_base * (2 ** attempt), cfg.backoff_max)
                time.sleep(delay)
            else:
                break

        error = last.error_str if last is not None else None
        if cfg.fallback is not None:
            try:
                fb = cfg.fallback()
                return StableResult(
                    result=f"{_DEGRADED_PREFIX}{fb}", attempts=attempts,
                    degraded=True, error=error, timed_out=bool(last and last.timed_out),
                )
            except Exception:  # noqa: BLE001 - fallback 自身失败则返回原错误
                pass
        return StableResult(
            error=error, attempts=attempts,
            timed_out=bool(last and last.timed_out),
        )

    # ---- 单次运行：超时包装 ----

    def _run_once(self, fn: Callable[..., Any], arguments: dict[str, Any]) -> _AttemptOutcome:
        """执行 fn(**arguments)；超时则返回"超时"信号而不卡死。

        用一个单工作线程 + deadline 执行：`future.result(timeout)` 到点即抛超时，
        我们在 finally 里 `shutdown(wait=False)` 立即交还控制权（不 join 那条被卡线程）。
        这是进程内线程无法真杀的限制下，"保证调用方不悬挂"的工程解法。
        """
        timeout = self.config.timeout_seconds
        if timeout <= 0:
            try:
                return _AttemptOutcome(result=fn(**arguments))
            except Exception as e:  # noqa: BLE001 - 工具错误要转成可读信号
                return _AttemptOutcome(exc=e)

        ex = ThreadPoolExecutor(max_workers=1)
        try:
            fut = ex.submit(fn, **arguments)
            try:
                return _AttemptOutcome(result=fut.result(timeout=timeout))
            except TimeoutError:
                return _AttemptOutcome(timed_out=True)
            except Exception as e:  # noqa: BLE001
                return _AttemptOutcome(exc=e)
        finally:
            # wait=False：不等待被卡的工作线程，立刻把控制权还回 loop
            ex.shutdown(wait=False, cancel_futures=False)

    # ---- 重试决策 ----

    def _should_retry(self, out: _AttemptOutcome, is_pure: bool) -> bool:
        """失败后问：还重试吗？瞬时错误 或（pure 且允许 pure 重试）→ 重试。"""
        if out.error_str is None:
            return False  # 成功
        if out.timed_out:
            return True  # 超时是典型的瞬时故障
        if out.exc is not None and isinstance(out.exc, self.config.retry_on_errors):
            return True
        return bool(is_pure and self.config.retryable_pure)


__all__ = ["StabilityConfig", "StableResult", "StableToolExecutor"]
