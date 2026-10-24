"""阶段规划的**默认口径**测试（第115 批：规划从"默认关"改成"默认开"）。

为什么值得单独一个文件：把默认值翻过来，风险不在代码而在**成本**——
如果"开"意味着每个简单任务都多花一次模型调用，那这个默认值就是在偷偷放血。
所以这里钉的不是"能开"，而是两件更硬的事：

1. **简单任务零成本**：`plan_with_model` 必须先跑离线复杂度判定，命中"不复杂"就直接返回，
   **一次模型调用都不发**（用计数桩模型数调用次数）。
2. **复杂任务只多一次**：多出来的那一次就是规划本身，且失败必须降级成模板而不是崩。
"""
from tests.conftest import ScriptedModel

from warden_agent.loop.planner import ModelPlanner, plan_with_model
from warden_agent.model.model import ChatResponse

COMPLEX_TASK = "调研三家公司并写对比报告，给出建议"
SIMPLE_TASK = "上海天气怎么样"


class CountingModel(ScriptedModel):
    """在剧本模型上记下**收到的请求**，用来断言"到底发没发模型调用"。"""

    def __init__(self) -> None:
        super().__init__([ChatResponse(content='{"steps":[{"title":"收集","goal":"收三家资料"}]}')])
        self.requests: list[object] = []

    def chat(self, request):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        return super().chat(request)


def test_简单任务不花模型调用() -> None:
    model = CountingModel()
    plan = plan_with_model(SIMPLE_TASK, model)
    assert plan.is_complex is False
    assert model.requests == []          # 关键断言：一次都没发


def test_复杂任务只多一次调用() -> None:
    model = CountingModel()
    plan = plan_with_model(COMPLEX_TASK, model)
    assert len(model.requests) == 1
    assert plan.is_complex is True


def test_规划调用失败时降级成模板而不是崩() -> None:
    class Exploding(ScriptedModel):
        def __init__(self) -> None:
            super().__init__([])

        def chat(self, request):  # type: ignore[no-untyped-def]
            raise RuntimeError("模型不可用")

    plan = plan_with_model(COMPLEX_TASK, Exploding())
    assert plan.is_complex is True
    assert plan.steps                # 模板兜底仍有阶段


def test_模型返回非法JSON时降级() -> None:
    model = ScriptedModel([ChatResponse(content="这不是 JSON")])
    plan = plan_with_model(COMPLEX_TASK, model)
    assert plan.is_complex is True
    assert plan.steps


def test_ModelPlanner转发build与context() -> None:
    model = CountingModel()
    planner = ModelPlanner(model)
    plan = planner.build(COMPLEX_TASK)
    assert plan.is_complex is True
    ctx = planner.context(plan, 0)
    assert "[任务规划]" in ctx
