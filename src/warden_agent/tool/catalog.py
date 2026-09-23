"""类型化 Tool（工具）。



白话解释：
「Tool」就是给 AI 准备的一张『技能卡』。AI 遇到需要"查天气"、"读文件"这类事时，
会在这堆技能卡里挑一张，填好参数，让程序去执行。AI 自己不真的动手，它只是"点单"，
真正干活的代码是这里的 Tool 实现。

一个 Tool 有三样东西：
1. name        —— 技能卡的名字，比如 "weather.get"
2. description —— 给 AI 看的说明，告诉它这卡是干嘛的
3. 参数 schema  —— 这张卡需要填哪些参数（城市名？文件路径？），用 JSON Schema 表达
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError


class Tool(Protocol):
    """一个 Tool 必须能：给出自己的说明书(spec)，并被调用(invoke)。"""

    def spec(self) -> ToolSpec:
        ...

    def invoke(self, arguments: dict[str, Any]) -> Any:
        ...


@dataclass(frozen=True)
class ToolSpec:
    """Tool 的『说明书』：名字、描述、参数格式、有没有副作用。"""

    name: str
    description: str
    parameters_schema: dict[str, Any] = field(default_factory=dict)
    pure: bool = False  # pure=True 表示没有副作用(不会改系统状态)，可以放心重试
    # 明确三观：这个工具"什么时候该被用"的触发词。
    # 手工标注是"可信第一手"；未标注的工具由 intent/skill 路由从 description 自动提取兜底。
    triggers: tuple[str, ...] = ()

    def to_openai_schema(self) -> dict[str, Any]:
        """转成 OpenAI 认识的工具格式（函数调用用）。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema,
            },
        }


@dataclass(frozen=True)
class FunctionToolSpec(ToolSpec):
    """把普通 Python 函数包装成一个 Tool 的说明书。"""

    function: Callable[..., Any] | None = None  # 真正的调用函数


def function_tool(
    name: str,
    description: str,
    parameters_schema: dict[str, Any],
    *,
    pure: bool = False,
    triggers: tuple[str, ...] = (),
) -> Callable[[Callable[..., Any]], FunctionToolSpec]:
    """装饰器：把一个 Python 函数变成一张可注册的『技能卡』。

    用法：
        @function_tool(
            "weather.get", "获取某个城市的天气",
            {"type": "object", "properties": {"city": {"type": "string"}},
             "required": ["city"]},
        )
        def get_weather(city: str) -> str:
            return f"{city} 今天晴，25 度"

    `triggers`：可选，手标这份工具的触发词（"什么时候该被调"）。
    不传时由 intent/skill 路由从 description 自动提取兜底，能力和"描述"长在一起。
    """

    def decorator(fn: Callable[..., Any]) -> FunctionToolSpec:
        return FunctionToolSpec(
            name=name,
            description=description,
            parameters_schema=parameters_schema,
            pure=pure,
            triggers=triggers,
            function=fn,
        )

    return decorator


class ToolArgumentError(Exception):
    """工具参数校验失败（用 Pydantic 校验时抛出）。"""


# JSON Schema 基础类型 → Python 类型（仅覆盖工具参数常用的几种）
_JSON_TYPE_MAP: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _type_matches(expected: object, value: object) -> bool:
    """校验一个值是否命中 JSON Schema 的 type（支持单类型或类型列表）。"""
    if isinstance(expected, list):
        return any(_type_matches(item, value) for item in expected)
    py = _JSON_TYPE_MAP.get(str(expected))
    if py is None:
        return True  # 未知/未声明类型不做强校验（不阻塞）
    # bool 是 int 的子类：integer/number 不应把 True/False 当数字放行。
    if isinstance(value, bool) and str(expected) in ("integer", "number"):
        return False
    return isinstance(value, py)


def _validate_arguments(spec: ToolSpec, arguments: dict[str, Any]) -> None:
    """按工具的 `parameters_schema` 校验参数，不合法抛 `ToolArgumentError`。

    在 dispatch 前拦下"必填缺失 / 类型不符 / 多余参数(additionalProperties=false)"，
    而不是让脏参数进到工具函数里才炸（错误信息也更清楚，便于喂回模型纠正）。
    """
    schema = spec.parameters_schema or {}
    if not isinstance(schema, dict) or not schema:
        return
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    missing = [name for name in required if name not in arguments]
    if missing:
        raise ToolArgumentError(
            f"工具 {spec.name!r} 缺少必填参数: {', '.join(missing)}"
        )
    if schema.get("additionalProperties") is False and isinstance(properties, dict):
        extra = [name for name in arguments if name not in properties]
        if extra:
            raise ToolArgumentError(
                f"工具 {spec.name!r} 收到未声明的参数: {', '.join(extra)}"
            )
    if not isinstance(properties, dict):
        return
    for key, value in arguments.items():
        rule = properties.get(key)
        if not isinstance(rule, dict):
            continue
        expected = rule.get("type")
        if expected is not None and not _type_matches(expected, value):
            raise ToolArgumentError(
                f"工具 {spec.name!r} 参数 {key!r} 类型应为 {expected}，实际为 "
                f"{type(value).__name__}"
            )


def pydantic_tool[M: BaseModel](
    name: str,
    description: str,
    input_model: type[M],
    *,
    pure: bool = False,
    strict: bool = False,
    triggers: tuple[str, ...] = (),
) -> Callable[[Callable[[M], Any]], FunctionToolSpec]:
    """Pydantic 版技能卡：用模型类自动生成 JSON Schema + 自动校验参数。

    对比手写 JSON Schema（function_tool），这一步是主流框架（PydanticAI 等）的做法：
    - 定义 输入模型（class 请求(BaseModel)）→ 自动生成 parameters_schema，不用手写 JSON。
    - 调用时先用 Pydantic 校验 arguments，非法参数直接抛 ToolArgumentError，
      不会把脏数据塞给工具函数（类型安全）。
    - strict=True 时用严格模式校验：禁止隐式类型转换（比如把字符串 "3" 当整数），
      AI 传错类型立刻报错，而不是悄悄帮你转。

    用法：
        from pydantic import BaseModel

        class WeatherReq(BaseModel):
            city: str

        @pydantic_tool("weather.get", "查天气", WeatherReq)
        def get_weather(req: WeatherReq) -> str:
            return f"{req.city}: 晴, 25度"
    """
    schema = input_model.model_json_schema()

    def decorator(fn: Callable[[M], Any]) -> FunctionToolSpec:
        def _call(**kwargs: Any) -> Any:
            try:
                validated = input_model.model_validate(kwargs, strict=strict)
            except ValidationError as e:
                raise ToolArgumentError(f"工具 {name!r} 参数无效: {e}") from e
            return fn(validated)

        return FunctionToolSpec(
            name=name,
            description=description,
            parameters_schema=schema,
            pure=pure,
            triggers=triggers,
            function=_call,
        )

    return decorator


class ToolCatalog:
    """装所有技能卡的『工具箱』，AI 只能从这里面挑工具用。

    设计要点：一旦把工具注册进目录，AI 想用的工具集合就固定了，
    不会运行到一半突然多出一个工具来（这叫"冻结工具集"）。
    """

    def __init__(self) -> None:
        self._by_name: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._by_name[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._by_name[name]
        except KeyError:
            raise KeyError(
                f"目录里没有这个工具: {name!r}，AI 不能调用未注册的工具"
            ) from None

    def all(self) -> list[ToolSpec]:
        return list(self._by_name.values())

    def execute(self, name: str, arguments: dict[str, Any]) -> Any:
        """根据名字找到工具并执行。AI 不能拿着没注册的名字来调用。

        执行前先按工具的 `parameters_schema` 校验参数（必填/类型），
        不合法直接抛 `ToolArgumentError`，不把脏参数交给工具函数。
        """
        spec = self.get(name)
        _validate_arguments(spec, arguments)
        if isinstance(spec, FunctionToolSpec) and spec.function is not None:
            return spec.function(**arguments)
        raise TypeError(f"{spec.name!r} 不是可执行的函数工具")
