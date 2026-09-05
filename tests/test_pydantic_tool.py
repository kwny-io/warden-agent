"""Pydantic 工具测试：自动生成 schema + 参数校验。"""
from pydantic import BaseModel

from warden_agent.tool.catalog import ToolArgumentError, ToolCatalog, pydantic_tool


class WeatherReq(BaseModel):
    city: str


def _catalog() -> ToolCatalog:
    @pydantic_tool("weather.get", "查城市天气", WeatherReq)
    def get_weather(req: WeatherReq) -> str:
        return f"{req.city}: 晴, 25度"

    catalog = ToolCatalog()
    catalog.register(get_weather)
    return catalog


def test_pydantic工具_自动生成schema() -> None:
    """schema 应从 Pydantic 模型自动生成，不需要手写 JSON。"""
    catalog = _catalog()
    schema = catalog.all()[0].to_openai_schema()
    params = schema["function"]["parameters"]
    assert params["properties"]["city"]["type"] == "string"
    assert "city" in params["required"]


def test_pydantic工具_合法参数被解析为模型() -> None:
    """合法参数应被 Pydantic 解析成模型实例再交给函数。"""
    catalog = _catalog()
    result = catalog.execute("weather.get", {"city": "上海"})
    assert result == "上海: 晴, 25度"


def test_pydantic工具_非法参数抛ToolArgumentError() -> None:
    """缺必填/类型错 → 抛 ToolArgumentError，不把脏数据喂给函数。"""
    catalog = _catalog()
    try:
        catalog.execute("weather.get", {})  # 缺 city
        assert False, "应抛 ToolArgumentError"
    except ToolArgumentError:
        pass


class CountReq(BaseModel):
    n: int


def test_pydantic工具_strict模式拒绝隐式转换() -> None:
    """strict=True 时，字符串 "3" 不会被隐式转成整数，抛 ToolArgumentError。"""
    @pydantic_tool("count.get", "取个数", CountReq, strict=True)
    def get_count(req: CountReq) -> int:
        return req.n

    catalog = ToolCatalog()
    catalog.register(get_count)

    # 正常整数：通过
    assert catalog.execute("count.get", {"n": 3}) == 3
    # 字符串 "3"：strict 模式拒绝（默认模式会偷偷转）
    try:
        catalog.execute("count.get", {"n": "3"})
        assert False, "strict 模式应拒绝字符串当整数"
    except ToolArgumentError:
        pass
