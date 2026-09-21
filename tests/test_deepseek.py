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
    OpenAiCompatibleModel,
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


# ---------- custom 通用接入（傻瓜式接任意 OpenAI 兼容 API） ----------

def test_create_model_custom_读环境变量(monkeypatch) -> None:
    """custom: 从 WARDEN_MODEL_API_KEY / WARDEN_BASE_URL / WARDEN_MODEL 零改源码接入。"""
    monkeypatch.setenv("WARDEN_MODEL_API_KEY", "k-c")
    monkeypatch.setenv("WARDEN_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("WARDEN_MODEL", "my-llama")
    m = create_model("custom")
    assert isinstance(m, OpenAiCompatibleModel)
    assert m.model == "my-llama"
    assert "11434" in str(m._client.base_url)


def test_create_model_custom_不读服务端鉴权密钥(monkeypatch) -> None:
    """回归：`WARDEN_API_KEY` 是 HTTP 服务的鉴权密钥，**不能**被 custom 当模型 key 用。

    两者曾经同名 → 用 WARDEN_API_KEY 开鉴权、又用 custom 接自建网关时，
    模型会把**服务端鉴权密钥**发给那个网关。现在 custom 只认 WARDEN_MODEL_API_KEY。
    """
    monkeypatch.delenv("WARDEN_MODEL_API_KEY", raising=False)
    monkeypatch.setenv("WARDEN_API_KEY", "服务端鉴权密钥-不该被模型用")
    monkeypatch.setenv("WARDEN_BASE_URL", "https://gw.example.com/v1")
    with pytest.raises(DeepSeekError) as exc:
        create_model("custom")
    assert "WARDEN_MODEL_API_KEY" in str(exc.value)


def test_create_model_custom_不回落到其他厂商密钥(monkeypatch) -> None:
    """回归：custom 若借用别的厂商密钥，等于**把那个密钥发给第三方** → 必须拒绝。"""
    monkeypatch.delenv("WARDEN_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("WARDEN_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-真实厂商密钥")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-另一个厂商密钥")
    monkeypatch.setenv("WARDEN_BASE_URL", "https://third-party-gw.example.com/v1")
    with pytest.raises(DeepSeekError) as exc:
        create_model("custom")
    assert "WARDEN_MODEL_API_KEY" in str(exc.value)


def test_create_model_custom_本机网关可填占位串(monkeypatch) -> None:
    """Ollama 这类不校验密钥的端点：填任意非空占位串即可。"""
    monkeypatch.setenv("WARDEN_MODEL_API_KEY", "not-needed")
    monkeypatch.setenv("WARDEN_BASE_URL", "http://localhost:11434/v1")
    m = create_model("custom")
    assert "11434" in str(m._client.base_url)


def test_create_model_custom_显式传参覆盖() -> None:
    """custom: 也能直接传 base_url/model/api_key,不依赖环境变量。"""
    m = create_model("custom", api_key="k",
                     base_url="https://my-gateway.example.com/v1", model="gpt-x")
    assert m.model == "gpt-x"
    assert "my-gateway" in str(m._client.base_url)


def test_create_model_custom_缺base_url报错(monkeypatch) -> None:
    """custom: 没给 base_url 且没设 WARDEN_BASE_URL => 清晰报错,提示怎么配。"""
    monkeypatch.delenv("WARDEN_BASE_URL", raising=False)
    with pytest.raises(DeepSeekError):
        create_model("custom", api_key="k")


def test_create_model_custom_未知厂商仍报错() -> None:
    with pytest.raises(DeepSeekError):
        create_model("still-unknown", api_key="k")


class FakeStreamCompletions:
    """模拟流式 create：返回逐段产出的 chunk 迭代器，并记录调用参数。"""

    def __init__(self, chunks):
        self.chunks = chunks
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return iter(self.chunks)


def _stream_chunk(content=None, tool_calls=None, finish_reason=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)])


def test_chat_stream_iter_逐段实时产出增量并汇总done():
    """真流式生成器：delta 逐段产出（边生成边吐，不是攒一坨），
    流结束时产出 done 事件，携带完整 content / finish_reason。"""
    chunks = [
        _stream_chunk(content="你好"),
        _stream_chunk(content="，世界"),
        _stream_chunk(finish_reason="stop"),
    ]
    m = create_model("custom", api_key="k", base_url="http://x/v1", model="m")
    fake = FakeStreamCompletions(chunks)
    m._client = SimpleNamespace(chat=SimpleNamespace(completions=fake))

    events = list(m.chat_stream_iter(ChatRequest(messages=[Message(role="user", content="hi")])))
    assert [e["type"] for e in events] == ["delta", "delta", "done"]
    assert events[0]["text"] == "你好"
    done = events[-1]["response"]
    assert done.content == "你好，世界"
    assert done.finish_reason == "stop"
    # 流式开关确实打开了
    assert fake.last_kwargs.get("stream") is True
