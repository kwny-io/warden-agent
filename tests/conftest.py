"""pytest 共享 fixture / 工具，供各测试文件复用。"""
from __future__ import annotations

from warden_agent.model.model import AgentChatModel, ChatRequest, ChatResponse
from warden_agent.tool.catalog import ToolCatalog, function_tool


class ScriptedModel(AgentChatModel):
    """每一步都按预写剧本走的模型，用于精确测试循环逻辑。

    真模型/通用假模型的回答不确定，没法精确断言；
    这个模型完全由我们给定的"剧本"(一串 ChatResponse)驱动，
    因此能精确控制"模型每步干嘛"，把循环的各种情况都测到。
    """

    def __init__(self, script: list[ChatResponse]) -> None:
        self.script = list(script)
        self.calls = 0

    def chat(self, request: ChatRequest) -> ChatResponse:
        if self.calls >= len(self.script):
            raise AssertionError("脚本模型被调用次数超过剧本长度")
        resp = self.script[self.calls]
        self.calls += 1
        return resp


def weather_tool() -> ToolCatalog:
    """造一个只含 weather.get 的工具箱，供各测试复用。"""
    @function_tool(
        "weather.get",
        "获取某城市天气",
        {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
        pure=True,
    )
    def get_weather(city: str) -> str:
        return f"{city}: 晴, 25度"

    catalog = ToolCatalog()
    catalog.register(get_weather)
    return catalog
