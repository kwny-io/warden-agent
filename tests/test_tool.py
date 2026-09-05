"""Tool 管线测试。"""
from warden_agent.tool.catalog import ToolCatalog, function_tool


def test_函数工具_被注册和调用() -> None:
    @function_tool(
        "weather.get",
        "获取某城市的天气",
        {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
        pure=True,
    )
    def get_weather(city: str) -> str:
        return f"{city} 今天晴，25 度"

    catalog = ToolCatalog()
    catalog.register(get_weather)
    assert "weather.get" in [s.name for s in catalog.all()]
    result = catalog.execute("weather.get", {"city": "上海"})
    assert result == "上海 今天晴，25 度"


def test_目录里没有的工具_调用会被拒绝() -> None:
    """AI 不能调用没注册的工具（冻结工具集之外的不放行）。"""
    catalog = ToolCatalog()
    try:
        catalog.execute("not.registered", {})
        assert False, "应该拒绝未注册工具"
    except KeyError as e:
        assert "not.registered" in str(e)


def test_工具转openai格式() -> None:
    @function_tool("math.add", "加法", {
        "type": "object",
        "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
        "required": ["a", "b"],
    })
    def add(a: float, b: float) -> float:
        return a + b

    schema = add.to_openai_schema()
    assert schema["function"]["name"] == "math.add"
    assert schema["function"]["parameters"]["properties"]["a"]["type"] == "number"
