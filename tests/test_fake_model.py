"""假模型的**流式**能力：离线也能把流式链路测到。

此前 `FakeModel` 只会一次性返回，没有 stream/deltas，也没有 `chat_stream_iter`，
所以"流式"只能在真模型上验——离线测试根本碰不到这条路径。这里锁住它与
`model/deepseek.py` 对齐的形状：`stream=True` 时 response 带 deltas；
`chat_stream_iter` 先逐段 yield `delta`，最后 yield `done`。
"""

from __future__ import annotations

from warden_agent.model.fake import FakeModel
from warden_agent.model.model import ChatRequest, Message


def _tools() -> list[dict[str, object]]:
    return [{
        "type": "function",
        "function": {
            "name": "weather.get",
            "description": "查天气",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        },
    }]


def test_流式chat的deltas拼接等于全量() -> None:
    model = FakeModel()
    resp = model.chat(ChatRequest(messages=[Message(role="user", content="你好")], stream=True))
    assert resp.content                              # 规则2：无工具 → 固定客套话
    assert resp.deltas
    assert "".join(resp.deltas) == resp.content      # 增量拼起来就是全量（真模型同口径）


def test_非流式chat不带deltas() -> None:
    model = FakeModel()
    resp = model.chat(ChatRequest(messages=[Message(role="user", content="你好")]))
    assert resp.content
    assert resp.deltas == []


def test_stream_iter先增量后done() -> None:
    model = FakeModel()
    events = list(model.chat_stream_iter(
        ChatRequest(messages=[Message(role="user", content="你好")])
    ))
    assert events[-1]["type"] == "done"
    deltas = [e["text"] for e in events if e["type"] == "delta"]
    assert deltas
    assert "".join(deltas) == events[-1]["response"].content


def test_stream_iter工具调用时无增量但有tool_calls() -> None:
    model = FakeModel()
    events = list(model.chat_stream_iter(ChatRequest(
        messages=[Message(role="user", content="上海天气用weather.get查一下")],
        tools=_tools(),
    )))
    assert [e for e in events if e["type"] == "delta"] == []
    done = events[-1]["response"]
    assert done.finish_reason == "tool_calls"
    assert done.tool_calls and done.tool_calls[0].name == "weather.get"
