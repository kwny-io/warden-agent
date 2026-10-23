"""成本账（第116 批）：把"这次跑了多少钱"变成可断言的事实。

这一批要钉住的不只是算术，而是三条**语义**：
  1. 未知模型必须记 `unpriced_calls`，**不能**当 0 元——否则换模型后成本账静默失真；
  2. 预算越界要 fail-closed（抛异常停住），且**已经花掉的那一笔仍要记账**；
  3. 两次 run 的账互不污染（同一个 meter 可被复用，但不该被别人偷偷加钱）。
"""

from __future__ import annotations

import pytest

from warden_agent.core.usage import (
    PRICES,
    BudgetExceeded,
    CostMeter,
    UsageTotals,
    price_for,
)
from warden_agent.loop.loop import AgentLoop
from warden_agent.model.model import ChatResponse, ModelUsage
from warden_agent.tool.catalog import ToolCatalog


class _MeteredModel:
    """每次回答都带用量的假模型（真模型的行为，FakeModel 不带 usage）。"""

    def __init__(self, usage: ModelUsage | None, name: str = "deepseek-chat") -> None:
        self.usage = usage
        self.model = name
        self.calls = 0

    def chat(self, request):  # noqa: ANN001 - 协议签名
        self.calls += 1
        return ChatResponse(content=f"第{self.calls}次回答", usage=self.usage)


def test_按价格表折算美元() -> None:
    meter = CostMeter(env={})
    meter.record("deepseek-chat", ModelUsage(prompt_tokens=1000, completion_tokens=500))
    totals = meter.snapshot()
    # 1000*0.27/1e6 + 500*1.10/1e6 = 0.00027 + 0.00055
    assert totals.usd == pytest.approx(0.00082)
    assert totals.calls == 1
    assert totals.total_tokens == 1500
    assert totals.unpriced_calls == 0


def test_未知模型记未定价而不是零元() -> None:
    meter = CostMeter(env={})
    meter.record("some-unlisted-model", ModelUsage(prompt_tokens=1_000_000, completion_tokens=0))
    totals = meter.snapshot()
    assert totals.unpriced_calls == 1
    assert totals.usd == 0.0          # 没定价 → 不加钱
    assert totals.calls == 1          # 但调用确实发生过
    assert totals.prompt_tokens == 1_000_000
    assert meter.unpriced_models == ("some-unlisted-model",)


def test_环境变量覆盖单价() -> None:
    env = {"WARDEN_PRICE_INPUT_PER_M": "1.0", "WARDEN_PRICE_OUTPUT_PER_M": "2.0"}
    assert price_for("deepseek-chat", env) == (1.0, 2.0)
    meter = CostMeter(env=env)
    meter.record("deepseek-chat", ModelUsage(prompt_tokens=1_000_000, completion_tokens=1_000_000))
    assert meter.snapshot().usd == pytest.approx(3.0)


def test_只给一个覆盖变量时不生效() -> None:
    env = {"WARDEN_PRICE_INPUT_PER_M": "1.0"}
    assert price_for("deepseek-chat", env) == PRICES["deepseek-chat"]


def test_坏单价退回常量表() -> None:
    env = {"WARDEN_PRICE_INPUT_PER_M": "不是数字", "WARDEN_PRICE_OUTPUT_PER_M": "2.0"}
    assert price_for("deepseek-chat", env) == PRICES["deepseek-chat"]


def test_缺用量仍计一次调用() -> None:
    meter = CostMeter(env={})
    meter.record("deepseek-chat", None)
    totals = meter.snapshot()
    assert totals.calls == 1
    assert totals.total_tokens == 0
    assert totals.usd == 0.0


def test_reset_清空累计() -> None:
    meter = CostMeter(env={})
    meter.record("deepseek-chat", ModelUsage(prompt_tokens=1000, completion_tokens=500))
    meter.reset()
    assert meter.snapshot() == UsageTotals()


def test_超预算抛出并停止调用模型() -> None:
    model = _MeteredModel(ModelUsage(prompt_tokens=1_000_000, completion_tokens=0))
    loop = AgentLoop(model, ToolCatalog(), max_cost_usd=0.1)
    with pytest.raises(BudgetExceeded) as err:
        loop.run("随便问一句")
    # 1M prompt token * 0.27 = 0.27 美元，第一次调用就越界
    assert err.value.spent_usd > 0.1
    assert model.calls == 1           # 关键：没有再调第二次（fail-closed）
    # 越界那一笔仍然记在账上（对账不能缺最后一笔）
    assert loop.cost_meter.snapshot().calls == 1


def test_环境变量预算生效() -> None:
    model = _MeteredModel(ModelUsage(prompt_tokens=1_000_000, completion_tokens=0))
    loop = AgentLoop(model, ToolCatalog())  # 不传 max_cost_usd
    loop.max_cost_usd = None
    assert loop.max_cost_usd is None
    # 走构造期读环境变量那条路径
    import os

    os.environ["WARDEN_RUN_BUDGET_USD"] = "0.05"
    try:
        loop2 = AgentLoop(model, ToolCatalog())
        assert loop2.max_cost_usd == pytest.approx(0.05)
        with pytest.raises(BudgetExceeded):
            loop2.run("随便问一句")
    finally:
        os.environ.pop("WARDEN_RUN_BUDGET_USD", None)


def test_不设预算就不拦() -> None:
    model = _MeteredModel(ModelUsage(prompt_tokens=1_000_000, completion_tokens=0))
    loop = AgentLoop(model, ToolCatalog())
    assert loop.max_cost_usd is None
    reply = loop.run("随便问一句")
    assert reply.cost_usd > 0.2        # 花了钱但没拦


def test_收敛时回复带用量与花费() -> None:
    model = _MeteredModel(ModelUsage(prompt_tokens=1000, completion_tokens=500))
    loop = AgentLoop(model, ToolCatalog())
    reply = loop.run("随便问一句")
    assert reply.usage is not None
    assert reply.usage.calls == 1
    assert reply.usage.total_tokens == 1500
    assert reply.cost_usd == pytest.approx(0.00082)
    assert reply.budget_hit is False


def test_两个_run_各用各的账() -> None:
    model_a = _MeteredModel(ModelUsage(prompt_tokens=1000, completion_tokens=500))
    model_b = _MeteredModel(ModelUsage(prompt_tokens=2000, completion_tokens=0))
    a = AgentLoop(model_a, ToolCatalog())
    b = AgentLoop(model_b, ToolCatalog())
    a.run("问一句")
    b.run("问一句")
    assert a.cost_meter.snapshot().total_tokens == 1500
    assert b.cost_meter.snapshot().total_tokens == 2000


def test_模型名探测_退化到类名() -> None:
    meter = CostMeter(env={})
    meter.record("_MeteredModel", ModelUsage(prompt_tokens=10, completion_tokens=0))
    assert meter.snapshot().unpriced_calls == 1
