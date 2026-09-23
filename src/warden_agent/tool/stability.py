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
    非 pure 工具只对**抛出的瞬时异常**（`retry_on_errors`）重试，**超时不重试**——
    超时只是"放弃等待"，被卡调用仍在后台跑，重放会让有副作用的操作执行两次。
  - **默认关闭、向后兼容**：`StabilityConfig()` 全是"不超时(0)/不重试(1)/无退避"，
    不配就不会改变原有行为；现有测试零影响。
  - **超时实现说明**：真函数工具卡死时，进程内线程无法被"强杀"。我们用一个**工作线程 +
    deadline** 执行：超时则立刻返回"超时"信号并把控制权交还 loop（looop 不再卡死），
    被卡的工作线程退居后台、不再等待。对 `pure` 工具（无副作用）这完全干净；对非 pure
    工具这也优先保证"循环不悬挂"。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from warden_agent.tool.catalog import ToolSpec

# 降级结果的统一前缀（与 multiagent 的 [降级] 词汇一致，让下游/模型一眼可辨）
_DEGRADED_PREFIX = "[降级]"
_CIRCUIT_PREFIX = "[熔断]"

_TIMEOUT_MSG = "TimeoutError: 工具执行超时"
_CIRCUIT_MSG = "工具连续失败，熔断保护，暂不调用"

# 被"放弃等待"的工具调用数（超时后线程仍在后台跑）。Python 无法强杀线程，
# 所以只能：① 用**守护线程**（不阻塞进程退出）；② 给这个数设**上限**——
# 卡死的工具被反复调用时，宁可拒绝新调用，也不让线程无界增长拖垮进程。
_stuck_lock = threading.Lock()
_stuck_threads = 0
_MAX_STUCK_THREADS = 8


@dataclass
class StabilityConfig:
    """工具调用稳定性层的配置（全部默认"关闭"，配了才生效）。"""

    timeout_seconds: float = 0                                # 0=不设超时
    max_attempts: int = 1                                     # 1=不做自动重试
    backoff_base: float = 0.5   # 指数退避底数:第 n 次重试等 base*2^(n-1)
    backoff_max: float = 8.0                                  # 退避上限，防无限拉长
    retry_on_errors: tuple[type[BaseException], ...] = (
        TimeoutError, ConnectionError, OSError,
    )                                    # 命中这些"瞬时异常"才重试（超时另算，见 _should_retry）
    retryable_pure: bool = True                # pure（无副作用）工具失败额外允许重试
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

        用**守护线程 + Event** 执行（不用 ThreadPoolExecutor：它的工作线程是非守护的，
        超时后 `shutdown(wait=False)` 只是不 join，线程仍活着，而且 concurrent.futures
        会注册 atexit 钩子在解释器退出时 join 它们——卡死的工具会**拖住进程退出**）。

        超时后的线程无法强杀（进程内线程的固有限制），所以配套两条兜底：
          - 线程是 **daemon**：不阻塞进程退出；
          - 卡死线程数有**上限**：达到上限后拒绝新调用并给出明确原因，
            避免"卡死的工具被反复调用 → 线程无界增长"把进程拖垮。
        """
        timeout = self.config.timeout_seconds
        if timeout <= 0:
            try:
                return _AttemptOutcome(result=fn(**arguments))
            except Exception as e:  # noqa: BLE001 - 工具错误要转成可读信号
                return _AttemptOutcome(exc=e)

        global _stuck_threads
        with _stuck_lock:
            if _stuck_threads >= _MAX_STUCK_THREADS:
                return _AttemptOutcome(exc=RuntimeError(
                    f"已有 {_stuck_threads} 个工具调用卡死未返回，拒绝再启动新调用"
                    "（防线程无界增长；卡死的工具应尽快修掉或调大超时）"
                ))

        done = threading.Event()
        box: dict[str, Any] = {}
        # 用锁保护的共享状态协调"谁负责加减计数"，避免"线程刚好在超时瞬间结束"的竞态
        state = {"finished": False, "counted": False}

        def _target() -> None:
            global _stuck_threads
            try:
                box["result"] = fn(**arguments)
            except BaseException as e:  # noqa: BLE001 - 含 KeyboardInterrupt 也要让 wait 返回
                box["exc"] = e
            finally:
                done.set()
                with _stuck_lock:
                    state["finished"] = True
                    if state["counted"] and _stuck_threads > 0:
                        _stuck_threads -= 1

        worker = threading.Thread(target=_target, name="warden-tool-call", daemon=True)
        worker.start()
        if done.wait(timeout):
            if "exc" in box:
                return _AttemptOutcome(exc=box["exc"])
            return _AttemptOutcome(result=box["result"])

        # 超时：放弃等待。线程转后台；若它此刻还没结束，就把这次记为"卡死"，
        # 等它将来真正返回时再自减（若它已经结束，就不计——见 state 的协调）。
        with _stuck_lock:
            if not state["finished"]:
                state["counted"] = True
                _stuck_threads += 1
        return _AttemptOutcome(timed_out=True)

    # ---- 重试决策 ----

    def _should_retry(self, out: _AttemptOutcome, is_pure: bool) -> bool:
        """失败后问：还重试吗？

        三种情况分开判定，**关键是"超时"不能和"抛错"同等对待**：

          - **超时（timed_out）**：我们只是"放弃等待"，被卡的那次调用**仍在后台线程里跑**
            （见 `_run_once` 的 `shutdown(wait=False)`），副作用照常发生。此时重放会让
            `fs.delete` 这类工具**执行两次** —— 一次人工审批换来两次删除。所以超时**只有
            pure（无副作用）工具能重试**。
          - **抛瞬时异常**（`retry_on_errors`：连接类/OSError 等）：工具"抛错"表示这次调用
            没完成；重试是抗抖动的主要手段。这里沿用原有设计（非 pure 也可重试），
            前提是**有副作用的工具要保证幂等**——否则应由工具自己处理，别抛瞬时异常。
          - pure 工具：任何失败都可安全重试。
        """
        if out.error_str is None:
            return False  # 成功
        if out.timed_out:
            # 超时 != 没副作用：被卡调用还在跑，重放 = 有副作用操作执行两次
            return bool(is_pure and self.config.retryable_pure)
        if out.exc is not None and isinstance(out.exc, self.config.retry_on_errors):
            return True  # 瞬时异常：这次没成功，重试抗抖动
        return bool(is_pure and self.config.retryable_pure)


# 工具稳定性层的"生产默认值"：保守，只做**防卡死 + 抗瞬时故障**，不激进重试。
#
# 关键点：重试对**有副作用**的工具是危险的（`fs.delete` 重试 = 删两次）。判定分两种：
#   · **超时**：只是"放弃等待"，被卡的那次调用还在后台跑，重放 = 有副作用操作执行两次
#     —— 所以超时**只有 pure 工具**能重试；
#   · **瞬时异常**（连接类/OSError 等）：这次调用没完成，重试抗抖动 —— 非 pure 也重试，
#     但前提是工具自身幂等（有副作用的工具要保证幂等，否则应由工具自己处理，别抛瞬时异常）。
# 想更保守可把工具声明成 `pure=True`，或自行实现幂等。
DEFAULT_STABILITY_CONFIG = StabilityConfig(
    timeout_seconds=30.0,   # 单次调用硬时限，防工具卡死拖住整个会话
    max_attempts=2,         # 只多试一次，避免放大瞬时故障
    backoff_base=0.5,
    backoff_max=4.0,
    circuit_threshold=5,    # 连续失败 5 次 → 短路，不再反复打一个已经坏掉的工具
    circuit_cooldown=30.0,  # 冷却 30 秒后半开试一次
)


def build_stability_executor(
    spec: bool | StabilityConfig | StableToolExecutor | None,
) -> StableToolExecutor | None:
    """把「要不要开稳定性层」的多种写法统一解析成执行器。

    - None / False       → None：工具直接执行，行为与以前**完全一致**（默认）
    - True               → `DEFAULT_STABILITY_CONFIG`
    - StabilityConfig    → 按给定配置构造
    - StableToolExecutor → 原样返回（便于测试注入替身）

    放在本模块而不是 `agent.py`：`build_agent`（SDK 面）和 `web/build_app`（产品面）
    都要用它，放这里可避免两者互相依赖。
    """
    if spec is None or spec is False:
        return None
    if spec is True:
        return StableToolExecutor(DEFAULT_STABILITY_CONFIG)
    if isinstance(spec, StabilityConfig):
        return StableToolExecutor(spec)
    return spec


__all__ = [
    "DEFAULT_STABILITY_CONFIG",
    "StabilityConfig",
    "StableResult",
    "StableToolExecutor",
    "build_stability_executor",
]
