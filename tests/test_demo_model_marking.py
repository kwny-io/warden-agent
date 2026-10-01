"""演示模型（假模型）必须在**响应本身**上被标出来，而不是只藏在 /capabilities 里。

为什么值得单独一组测试：FakeModel 会选工具、会答话、SSE 也能逐字打出来，看起来和真模型
一模一样。评审者/集成方/演示观众抓一次包、看一眼响应，就会把"写死脚本的输出"当成系统
真实能力的证据——这类"能力错觉"比直接报错危险得多。三道标记各自负责一段链路：

  1. HTTP 响应头 `X-Warden-Model-Kind: fake` + `Warning: 199 ...`：抓包、网关、
     集成脚本在**数据流里**就能看到（响应头按 latin-1 编码，值必须 ASCII）。
  2. 假模型的每条回复自带 `[演示模型]` 前缀：聊天记录里也没法被看成推理结果。
  3. 对外监听要双重显式声明（见 tests/test_run_server_main.py）。

反向保护同样重要：真模型**不许**被挂上这些标记（否则这标记就没人信了）。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import httpx
import pytest
from tests.conftest import ScriptedModel, weather_tool

from warden_agent.model.fake import DEMO_PREFIX, FakeModel
from warden_agent.model.model import ChatRequest, ChatResponse
from warden_agent.policy.policy import PolicyEngine
from warden_agent.store.sqlite import SqliteStore
from warden_agent.web.server import build_app


def _app(model) -> object:
    store = SqliteStore(Path(tempfile.mkdtemp()) / "t.db")
    app = build_app(model, weather_tool(), PolicyEngine(), store)
    app.state._store = store  # 测试结束前手动 close，别让 sqlite 连接漏着
    return app


async def _client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.anyio
async def test_假模型_非流式响应带标记与告警() -> None:
    app = _app(FakeModel())
    async with await _client(app) as c:
        r = await c.post("/chat/run-1", json={"text": "你好"})
    assert r.status_code == 200
    assert r.headers["X-Warden-Model-Kind"] == "fake"
    assert r.headers["Warning"].startswith("199 warden-agent")
    # 头值必须能 latin-1 编码（中文放头里会直接抛错）——这里顺带守住"别写中文"
    r.headers["Warning"].encode("latin-1")
    app.state._store.close()


@pytest.mark.anyio
async def test_真模型响应不挂演示标记() -> None:
    """反向保护：标记一旦滥挂就没人信了。"""
    model = ScriptedModel([ChatResponse(content="真回答", finish_reason="stop")])
    app = _app(model)
    async with await _client(app) as c:
        r = await c.post("/chat/run-1", json={"text": "你好"})
    assert r.status_code == 200
    assert "X-Warden-Model-Kind" not in r.headers
    assert "Warning" not in r.headers
    app.state._store.close()


@pytest.mark.anyio
async def test_假模型_流式响应同样带标记() -> None:
    """流式是最容易被误认的一种：真的逐字打出来，看的人不会怀疑。"""
    app = _app(FakeModel())
    async with await _client(app) as c, c.stream(
        "POST", "/chat/stream/run-1", json={"text": "你好"}
    ) as r:
        assert r.status_code == 200
        assert r.headers["X-Warden-Model-Kind"] == "fake"
        assert r.headers["Warning"].startswith("199 warden-agent")
        assert "[演示模型]" in "".join([chunk async for chunk in r.aiter_text()])
    app.state._store.close()


def test_假模型每条回复都自带演示前缀() -> None:
    """两条出口路径（有工具结果 / 没匹配到工具）都必须带前缀。"""
    model = FakeModel()

    done = model.chat(ChatRequest(messages=[], tools=[]))
    assert done.content is not None and done.content.startswith(DEMO_PREFIX)

    # 工具调用路径本身没有正文（content=None），把参数猜错这件事写在文档里而不是正文里；
    # 但"工具已跑完"的那一步是用户实际读到的那句话，必须带前缀。
    tool_call = model.chat(
        ChatRequest(
            messages=[_msg("user", "帮我查 weather 上海")],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "weather.get",
                        "description": "查天气",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                        },
                    },
                }
            ],
        )
    )
    assert tool_call.tool_calls and tool_call.tool_calls[0].name == "weather.get"

    after_tool = model.chat(
        ChatRequest(
            messages=[_msg("user", "帮我查 weather 上海"), _msg("tool", "上海: 晴")], tools=[]
        )
    )
    assert after_tool.content is not None and after_tool.content.startswith(DEMO_PREFIX)


def _msg(role: str, content: str):
    from warden_agent.model.model import Message

    return Message(role=role, content=content)
