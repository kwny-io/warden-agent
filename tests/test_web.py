"""HTTP/SSE 服务测试：用 httpx 异步 ASGI transport 走一遍真实 API。

FastAPI 应用是标准的，用 httpx.AsyncClient + ASGITransport 驱动（测试专用，
不真开端口）。用 pytest-asyncio 跑异步测试。
"""
import tempfile
from pathlib import Path

import httpx
import pytest
from tests.conftest import ScriptedModel, weather_tool

from warden_agent.model.model import ChatResponse, ToolCall
from warden_agent.policy.policy import Decision, PolicyEngine, PolicyResult
from warden_agent.store.sqlite import SqliteStore
from warden_agent.web.server import build_app


def _policy_ask() -> PolicyEngine:
    engine = PolicyEngine()
    engine.add(lambda name, args: PolicyResult(Decision.ASK, "需要人工批准"))
    return engine


async def _client_with(script, policy_engine, db_path=None):
    store = SqliteStore(db_path or Path(tempfile.mkdtemp()) / "t.db")
    app = build_app(model=ScriptedModel(script), catalog=weather_tool(),
                    policy=policy_engine, store=store)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_chat_返回最终回答() -> None:
    client = await _client_with(
        [ChatResponse(content="你好！", finish_reason="stop")], PolicyEngine())
    resp = await client.post("/chat/run-1", json={"text": "hi"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "final"
    assert body["text"] == "你好！"
    assert body["status"] == "COMPLETED"
    await client.aclose()


@pytest.mark.asyncio
async def test_chat_遇到审批_返回needs_approval并列出() -> None:
    script = [
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="c1", name="weather.get", arguments={"city": "上海"})],
            finish_reason="tool_calls"),
        ChatResponse(content="天晴。", finish_reason="stop"),
    ]
    client = await _client_with(script, _policy_ask())

    resp = await client.post("/chat/run-2", json={"text": "查天气"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "needs_approval"
    assert body["approval"]["tool_name"] == "weather.get"
    assert body["status"] == "WAITING_APPROVAL"

    approvals = (await client.get("/approvals")).json()
    assert any(a["run_id"] == "run-2" for a in approvals)

    final = await client.post("/approve/run-2")
    assert final.status_code == 200
    assert final.json()["kind"] == "final"
    assert final.json()["text"] == "天晴。"
    assert (await client.get("/approvals")).json() == []
    await client.aclose()


@pytest.mark.asyncio
async def test_拒绝工具_返回最终回答() -> None:
    script = [
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="c1", name="weather.get", arguments={"city": "上海"})],
            finish_reason="tool_calls"),
        ChatResponse(content="已跳过。", finish_reason="stop"),
    ]
    client = await _client_with(script, _policy_ask())
    await client.post("/chat/run-3", json={"text": "hi"})
    final = await client.post("/reject/run-3")
    assert final.json()["kind"] == "final"
    assert final.json()["text"] == "已跳过。"
    await client.aclose()


@pytest.mark.asyncio
async def test_对没有审批的run批准_返回409() -> None:
    client = await _client_with(
        [ChatResponse(content="ok", finish_reason="stop")], PolicyEngine())
    await client.post("/chat/run-4", json={"text": "hi"})
    resp = await client.post("/approve/run-4")
    assert resp.status_code == 409
    await client.aclose()


@pytest.mark.asyncio
async def test_状态查询() -> None:
    client = await _client_with(
        [ChatResponse(content="ok", finish_reason="stop")], PolicyEngine())
    await client.post("/chat/run-5", json={"text": "hi"})
    status = (await client.get("/status/run-5")).json()
    assert status["status"] == "COMPLETED"
    await client.aclose()


@pytest.mark.asyncio
async def test_首页返回演示控制台() -> None:
    """GET / 应返回可视化演示网页（HTML）。

    T10 起首页优先返回 React 构建产物（web/dist 的 SPA）；若未构建则回退到旧版
    演示控制台。无论哪种都要是合法 HTML 且带 "Warden Agent" 标题。
    """
    client = await _client_with(
        [ChatResponse(content="ok", finish_reason="stop")], PolicyEngine())
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    assert "Warden Agent" in body
    assert "@vite/client" not in body  # 不能是 Vite 开发页，必须是构建/回退产物
    await client.aclose()


@pytest.mark.asyncio
async def test_chat_stream_返回SSE事件流() -> None:
    """/chat/stream 应返回 text/event-stream，且包含 start 和 final 事件（打字机数据源）。"""
    client = await _client_with(
        [ChatResponse(content="你好！", finish_reason="stop")], PolicyEngine())
    content_type = ""
    body = ""
    async with client.stream("POST", "/chat/stream/run-s", json={"text": "hi"}) as resp:
        content_type = resp.headers["content-type"]
        async for line in resp.aiter_lines():
            body += line + "\n"
    assert content_type.startswith("text/event-stream")
    assert '"start"' in body
    assert '"final"' in body
    assert "你好" in body
    await client.aclose()


@pytest.mark.asyncio
async def test_t10_react_spa_服务和资源() -> None:
    """/ 应返回 React 构建产物 index.html（含 root 挂载点 + 绝对路径 /assets），
    且 /assets 下的静态资源能被同源加载（单端口部署）。"""
    client = await _client_with(
        [ChatResponse(content="ok", finish_reason="stop")], PolicyEngine())
    resp = await client.get("/")
    assert resp.status_code == 200
    body = resp.text
    # React 构建产物的标志：一个 <div id="root"> 挂载点 + 引用 /assets/* 的打包资源
    assert '<div id="root">' in body
    assert "/assets/" in body
    # 取其中的一个资源路径（JS/CSS），请求它应能拿到 200（说明 FastAPI 挂载了 SPA 静态文件）
    import re

    m = re.search(r'(?:src|href)="(/assets/[^"]+)"', body)
    assert m, "构建产物中应引用 /assets 下的资源"
    asset_resp = await client.get(m.group(1).split("?")[0])
    assert asset_resp.status_code == 200
    await client.aclose()
