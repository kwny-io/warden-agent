"""成本账：把"这次跑了多少钱"变成可查的数字（第116 批）。

为什么需要：模型调用是这套系统里**唯一按量计费**的动作，而它此前是黑箱——
`ChatResponse.usage`（`model/model.py` 的 `ModelUsage`）拿到 token 数之后，
没有任何人去汇总。于是"这个 run 花了多少"、"哪个用户烧得最多"、"预算该在第几步拦住"
三个问题全都答不上来；出问题时只能看云厂商账单反推，粒度是整个账号。

本模块只做三件事，都刻意做小：

  1. **记**：`CostMeter.record(model, usage)` 累加 prompt/completion token 与调用次数；
  2. **算**：按价格表折成美元（`PRICES`，每百万 token 单价）；
  3. **拦**：`AgentLoop` 在每次模型调用后检查 `max_cost_usd`，超了抛 `BudgetExceeded`。

三条边界（不隐，写在代码里而不是只写在文档里）：

  · **价格表是常量快照，会过期**。所以它可被环境变量覆盖（`WARDEN_PRICE_INPUT_PER_M` /
    `WARDEN_PRICE_OUTPUT_PER_M`，单位美元/百万 token，两个都给才生效）。
  · **未知模型不按 0 计**。记进 `unpriced_calls`，`usd` 只汇总**已定价**的部分——
    "没定价"与"免费"必须分开，否则换模型后成本账会静默失真（少算的钱最难发现）。
  · **只统计模型调用**。工具执行、存储、网络出口都不在内；这不是完整账单，
    是"模型这一项"的账。别拿它对外说"总成本"。

用法：

    meter = CostMeter()
    meter.record("deepseek-chat", ModelUsage(prompt_tokens=1200, completion_tokens=300))
    meter.snapshot()   # UsageTotals(prompt_tokens=1200, completion_tokens=300, calls=1, ...)

线程安全：`AgentLoop` 可能被多线程共用（web 并发、多 Agent），所以累加走一把 `Lock`。
"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@runtime_checkable
class TokenUsage(Protocol):
    """只认两个字段的**结构化**用量视图。

    为什么不 import `model.model.ModelUsage`：`core` 是最底层（tier 0），
    反向依赖 `model`（tier 1）会被 `tests/test_architecture.py` 判为破坏依赖方向。
    这里用 Protocol 描述"我需要的形状"，`ModelUsage` 天然满足它，
    两边都不必知道对方存在——这条边界由架构测试钉着。
    """

    prompt_tokens: int | None
    completion_tokens: int | None

# 每百万 token 的美元单价：(输入, 输出)。**这是快照，会过期**——
# 供应商价目表会改，所以这里同时支持环境变量覆盖。
# 为什么不在常量里也放缓存命中价：各家 cached 计价口径不统一，
# 需要时用 WARDEN_PRICE_INPUT_PER_M 覆盖成"混合后单价"更诚实。
PRICES: Mapping[str, tuple[float, float]] = {
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
}

_PER_MILLION = 1_000_000.0


class BudgetExceeded(RuntimeError):
    """单次 run 的模型花费超过预算上限（fail-closed：停下，而不是"再跑一步看看"）。

    为什么不是"记一条告警继续跑"：预算是**硬约束**，不是观测指标。
    继续跑意味着"用户已经越界，系统还在替他花钱"，那这条约束就不存在。
    """

    def __init__(self, spent_usd: float, limit_usd: float) -> None:
        self.spent_usd = spent_usd
        self.limit_usd = limit_usd
        super().__init__(
            f"本次运行模型花费已达 ${spent_usd:.4f}，超过预算 ${limit_usd:.4f}；"
            "已停止继续调用模型（要放宽就调 WARDEN_RUN_BUDGET_USD，或分多次运行）"
        )


@dataclass(frozen=True)
class UsageTotals:
    """一次 run（或一段累计窗口）的用量快照。不可变：拿到就是那一瞬的事实。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    unpriced_calls: int = 0     # 价格表里没有的模型调用次数（**不是** 0 元）
    usd: float = 0.0            # 只汇总已定价部分

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def as_dict(self) -> dict[str, float | int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "calls": self.calls,
            "unpriced_calls": self.unpriced_calls,
            "usd": round(self.usd, 6),
        }


def price_for(model: str, env: Mapping[str, str] | None = None) -> tuple[float, float] | None:
    """查单价：环境变量覆盖优先，其次常量表；都没有返回 None（=**未定价**，不是免费）。"""
    source = env if env is not None else os.environ
    raw_in = source.get("WARDEN_PRICE_INPUT_PER_M")
    raw_out = source.get("WARDEN_PRICE_OUTPUT_PER_M")
    if raw_in and raw_out:
        try:
            return (float(raw_in), float(raw_out))
        except ValueError:
            # 写错就退回常量表，不拿一个坏价格去做预算判断（宁可少拦，不可误拦）
            pass
    return PRICES.get(model)


@dataclass
class CostMeter:
    """累加器：多个模型、多次调用汇成一份用量快照。

    `env` 可注入（测试用），默认读进程环境。
    """

    env: Mapping[str, str] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _prompt: int = field(default=0, init=False, repr=False)
    _completion: int = field(default=0, init=False, repr=False)
    _calls: int = field(default=0, init=False, repr=False)
    _unpriced: int = field(default=0, init=False, repr=False)
    _usd: float = field(default=0.0, init=False, repr=False)
    _unpriced_models: set[str] = field(default_factory=set, init=False, repr=False)

    def record(self, model: str, usage: TokenUsage | None) -> None:
        """记一次模型调用。`usage=None`（供应商没回用量）只增加调用计数。

        为什么不把缺失用量当 0 token：那会让"没拿到账"和"真的没花钱"混为一谈。
        `calls` 与 token 数是两个独立事实，分开记。
        """
        price = price_for(model, self.env)
        prompt = int(usage.prompt_tokens or 0) if usage else 0
        completion = int(usage.completion_tokens or 0) if usage else 0
        with self._lock:
            self._calls += 1
            self._prompt += prompt
            self._completion += completion
            if price is None:
                self._unpriced += 1
                self._unpriced_models.add(model)
                return
            in_price, out_price = price
            self._usd += (prompt * in_price + completion * out_price) / _PER_MILLION

    def snapshot(self) -> UsageTotals:
        with self._lock:
            return UsageTotals(
                prompt_tokens=self._prompt,
                completion_tokens=self._completion,
                calls=self._calls,
                unpriced_calls=self._unpriced,
                usd=self._usd,
            )

    @property
    def usd(self) -> float:
        with self._lock:
            return self._usd

    @property
    def unpriced_models(self) -> tuple[str, ...]:
        """哪些模型没进价格表——用来回答"这个数字为什么偏低"。"""
        with self._lock:
            return tuple(sorted(self._unpriced_models))

    def reset(self) -> None:
        with self._lock:
            self._prompt = self._completion = self._calls = self._unpriced = 0
            self._usd = 0.0
            self._unpriced_models.clear()
