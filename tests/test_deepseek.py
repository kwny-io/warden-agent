"""模型层测试（官方 openai SDK 实现）。

我们不用真 key、不碰网。做法：构造模型后，把真实的 openai 客户端替换成一个
"假客户端"（stub），它的 chat.completions.create 返回我们写好的形状，从而
验证我们的翻译逻辑（消息映射 / 工具解析 / 流式参数分片累加 / 结构化输出 / usage）。
"""
from types import SimpleNamespace

import pytest

from warden_agent.model.deepseek import (
    BailianModel,
    DeepSeekError,
    DeepSeekModel,
    OpenAIModel,
    ZhipuModel,
    _safe_json,
    create_model,
)
from warden_agent.model.model import ChatRequest, Message, ToolCall


# ---------- 构造假客户端的小工具 ----------
def _msg(content="hi", tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def _choice(message, finish_reason="stop"):
    return SimpleNamespace(message=message, finish_reason=finish_reason)


def _tool_call(index, name=None, arguments=None, id=None):
    return SimpleNamespace(
        index=index,
        id=id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


class FakeCompletions:
    """模拟 openai 客户端的 chat.completions.create。"""

    def __init__(self, result):
        self.result = result
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return self.result


class FakeClient:
    def __init__(self, result):
        self.chat = SimpleNamespace(completions=FakeCompletions(result))

    def close(self):
        pass


def _model_with(result):
    """造一个 DeepSeekModel，但把真实客户端换成假客户端。"""
    m = DeepSeekModel(api_key="sk-test")  # 用假 key 构造（不联网）
    m._client = FakeClient(result)
    return m


def test_普通对话_返回内容() -> None:
    m = _model_with(SimpleNamespace(
        choices=[_choice(_msg("你好！"))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)))
    resp = m.chat(ChatRequest(messages=[Message(role="user", content="你好")]))
    assert resp.content == "你好！"
    assert resp.tool_calls is None
    assert resp.usage.total_tokens == 15


def test_工具调用被正确解析() -> None:
    tc = _tool_call(0, name="weather.get", arguments='{"city":"上海"}', id="c1")
    m = _model_with(SimpleNamespace(
        choices=[_choice(_msg(None, [tc]), "tool_calls")], usage=None))
    resp = m.chat(ChatRequest(messages=[Message(role="user", content="上海天气")]))
    assert resp.tool_calls is not None
    assert resp.tool_calls[0].name == "weather.get"
    assert resp.tool_calls[0].arguments == {"city": "上海"}


def test_消息映射_tool角色正确() -> None:
    """发给模型的 tool 消息 role 应为 'tool'。"""
    m = _model_with(SimpleNamespace(choices=[_choice(_msg("ok"))], usage=None))
    m.chat(ChatRequest(messages=[Message(role="tool", content="结果")]))
    sent = m._client.chat.completions.last_kwargs["messages"]
    assert sent[0]["role"] == "tool"


def _stream_choice(delta, finish_reason=None):
    """构造流式 chunk 的 choice：流式用的是 .delta，不是 .message。"""
    return SimpleNamespace(delta=delta, finish_reason=finish_reason)


def test_流式_内容增量累加() -> None:
    """流式下 content 逐段来，最终 content 拼起所有片段，deltas 记录增量。"""

    def gen():
        for piece in ["你", "好", "！"]:
            yield SimpleNamespace(
                choices=[_stream_choice(SimpleNamespace(content=piece, tool_calls=None))],
                usage=None,
            )

    m = _model_with(gen())
    resp = m.chat(ChatRequest(messages=[Message(role="user", content="hi")], stream=True))
    assert resp.content == "你好！"
    assert resp.deltas == ["你", "好", "！"]


def test_流式_工具参数分片累加() -> None:
    """流式下工具参数被拆多片，必须按 index 拼好再解析——真实 Agent 的关键点。"""

    def gen():
        yield SimpleNamespace(choices=[_stream_choice(SimpleNamespace(
            content=None, tool_calls=[_tool_call(0, name="weather.get", arguments='{"ci')]))],
            usage=None)
        yield SimpleNamespace(choices=[_stream_choice(SimpleNamespace(
            content=None, tool_calls=[_tool_call(0, arguments='ty":"上海"}', id="c1")]))],
            usage=None)
        yield SimpleNamespace(choices=[_stream_choice(
            SimpleNamespace(content=None, tool_calls=None), "tool_calls")], usage=None)

    m = _model_with(gen())
    sent_tools = [{"type": "function", "function": {"name": "weather.get"}}]
    resp = m.chat(ChatRequest(messages=[Message(role="user", content="上海")],
                              tools=sent_tools, stream=True))
    assert resp.finish_reason == "tool_calls"
    assert resp.tool_calls is not None
    assert resp.tool_calls[0].name == "weather.get"
    assert resp.tool_calls[0].arguments == {"city": "上海"}  # 分片拼好再解析


def test_结构化输出_请求带response_format() -> None:
    schema = {"type": "object",
              "properties": {"city": {"type": "string"}}, "required": ["city"]}
    m = _model_with(SimpleNamespace(choices=[_choice(_msg('{"city":"上海"}'))], usage=None))
    m.chat(ChatRequest(messages=[Message(role="user", content="x")], structured_output=schema))
    fmt = m._client.chat.completions.last_kwargs["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["schema"] == schema


def test_工具名带点_自动合法化并还原() -> None:
    """框架内允许 weather.get，但协议只允许 weather_get：发送前转，收到后还原。"""
    tc = _tool_call(0, name="weather_get", arguments='{"city":"上海"}', id="c1")
    m = _model_with(SimpleNamespace(
        choices=[_choice(_msg(None, [tc]), "tool_calls")], usage=None))
    # 传入带点的工具 schema
    tools = [{"type": "function",
              "function": {"name": "weather.get", "description": "天气"}}]
    resp = m.chat(ChatRequest(messages=[Message(role="user", content="上海天气")], tools=tools))
    # 发送给模型的工具名应该是合法化的 weather_get
    sent = m._client.chat.completions.last_kwargs["tools"]
    assert sent[0]["function"]["name"] == "weather_get"
    # 收到的 tool_call 名被还原成框架里的 weather.get
    assert resp.tool_calls is not None
    assert resp.tool_calls[0].name == "weather.get"


def test_缺key时报错() -> None:
    with pytest.raises(DeepSeekError):
        DeepSeekModel(api_key="")  # 空 key 且无环境变量 -> 报错


def test_openai_model_同样构造() -> None:
    m = OpenAIModel(api_key="sk-test")
    assert "gpt" in m.model  # 默认是 gpt-4o-mini


def test_多厂商子类_各自默认端点() -> None:
    z = ZhipuModel(api_key="z-test")
    assert "glm" in z.model
    b = BailianModel(api_key="b-test")
    assert "qwen" in b.model


def test_create_model_按名选择() -> None:
    m = create_model("zhipu", api_key="z-test")
    assert isinstance(m, ZhipuModel)
    m2 = create_model("deepseek", api_key="d-test")
    assert isinstance(m2, DeepSeekModel)


def test_create_model_未知厂商报错() -> None:
    with pytest.raises(DeepSeekError):
        create_model("nope", api_key="x")


def test_安全解析arguments() -> None:
    assert _safe_json("{}") == {}
    assert _safe_json('{"a":1}') == {"a": 1}
    assert _safe_json("not-json") == {}


def test_assistant_tool_call_映射为完整tool_calls() -> None:
    """真实 API 回归测试：带 tool_call 的 assistant 消息必须映射成完整 tool_calls
    结构（content 显式 null + 合法化工具名 + JSON 字符串参数），否则 API 报 400。"""
    m = _model_with(SimpleNamespace(choices=[_choice(_msg("ok"))], usage=None))
    msg = Message(role="assistant", content="[调用工具 weather.get]",
                  tool_call=ToolCall(id="c9", name="weather.get",
                                     arguments={"city": "上海"}))
    email = m._messages_to_openai([msg])[0]
    assert email["role"] == "assistant"
    assert email["content"] is None  # 显式 null
    assert email["tool_calls"][0]["id"] == "c9"
    assert email["tool_calls"][0]["function"]["name"] == "weather_get"  # 合法化
    assert '"city": "上海"' in email["tool_calls"][0]["function"]["arguments"]
