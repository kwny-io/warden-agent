"""一轮对话失败时，状态码必须说明"是谁的错"——否则两种事故同时发生。

这个判据来自一个被真实缺陷逼出来的场景：修之前 `/chat` 的所有异常一律 `400`，于是

  1. **供应商宕机被报成用户的错**：客户端按 400 处理就不会重试（400 的语义是"重试没用"），
     一次上游抽风 = 用户看到"对话失败"且永远不会自己好；
  2. **告警永不响**：`warden_http_errors_total` 只统计 `>=500`，而告警 `WardenHighErrorRate`
     只读这个指标 ⇒ 模型供应商整体不可用时，错误率面板恒为 0，值班人只能等用户投诉；
  3. 流式路径更糟：200 早已发出去，状态码无从更改，若再不计数，供应商宕机在流式路径上
     完全隐形。

所以这里钉三件事：分类函数对每一类异常的判定；非流式确实把 502 发出去并被计数；
流式虽然只能发 error 事件，但同样会计数。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import httpx
import pytest
from tests.conftest import ScriptedModel, weather_tool

from warden_agent.core.metrics import metrics
from warden_agent.model.deepseek import ModelCallError
from warden_agent.model.model import AgentChatModel, ChatRequest, ChatResponse
from warden_agent.policy.policy import PolicyDenied, PolicyEngine
from warden_agent.store.sqlite import SqliteStore
from warden_agent.tool.catalog import ToolArgumentError
from warden_agent.web.routes.chat import _failure_status
from warden_agent.web.server import build_app


class _RaisingModel(AgentChatModel):
    """每次都抛同一个异常的模型——用来模拟"上游挂了"这类轮内失败。"""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def chat(self, request: ChatRequest) -> ChatResponse:
        raise self.exc


def _app(model) -> object:
    store = SqliteStore(Path(tempfile.mkdtemp()) / "t.db")
    app = build_app(model, weather_tool(), PolicyEngine(), store)
    app.state._store = store  # 测试结束前手动 close，别让 sqlite 连接漏着
    return app


async def _client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _counted(status: str) -> bool:
    """指标里是否出现过这个状态桶。"""
    return f'warden_chat_failures_total{{status="{status}"}}' in metrics().render()


def _total() -> float:
    """所有桶的合计——用来做"绿的一轮不加数"的反向对照。"""
    return sum(
        float(line.rsplit(" ", 1)[1])
        for line in metrics().render().splitlines()
        if line.startswith("warden_chat_failures_total{")
    )


def test_分类函数_按谁的错分桶() -> None:
    """表驱动钉死每一类映射；改错了这里先红。"""
    assert _failure_status(ModelCallError("upstream 503")) == 502
    assert _failure_status(TimeoutError("超时")) == 504
    assert _failure_status(RuntimeError("租约丢了 / 没料到")) == 500
    assert _failure_status(PolicyDenied("该工具被永久禁止")) == 400
    assert _failure_status(ToolArgumentError("参数不合法")) == 400
    assert _failure_status(ValueError("没料到")) == 500


@pytest.mark.anyio
async def test_供应商宕机报_502_不是_400_且进错误率指标() -> None:
    app = _app(_RaisingModel(ModelCallError("upstream 503")))
    async with await _client(app) as c:
        r = await c.post("/chat/run-1", json={"text": "你好"})
    assert r.status_code == 502, "供应商挂了必须让客户端知道'可以重试'，不能报成 400"
    assert "upstream 503" in r.json()["detail"]
    rendered = metrics().render()
    assert _counted("502")
    # 告警 `WardenHighErrorRate` 只读这个指标；5xx 不进它，供应商宕机就永远不告警。
    assert "warden_http_errors_total" in rendered
    app.state._store.close()


@pytest.mark.anyio
async def test_策略拒绝仍然是_400() -> None:
    """反向保护：把用户错也改成 5xx，就等于教客户端"什么都重试"。"""
    app = _app(_RaisingModel(PolicyDenied("该工具被永久禁止")))
    async with await _client(app) as c:
        r = await c.post("/chat/run-1", json={"text": "你好"})
    assert r.status_code == 400
    app.state._store.close()


@pytest.mark.anyio
async def test_流式路径虽只能发_error_事件_但同样计数() -> None:
    """200 已经发出去了，改不了状态码；那就至少别让这次失败在指标里消失。"""
    app = _app(_RaisingModel(ModelCallError("upstream 503")))
    async with (
        await _client(app) as c,
        c.stream("POST", "/chat/stream/run-1", json={"text": "你好"}) as r,
    ):
        assert r.status_code == 200
        body = "".join([chunk async for chunk in r.aiter_text()])
    assert "upstream 503" in body
    assert _counted("502")
    app.state._store.close()


@pytest.mark.anyio
async def test_正常轮次不计失败() -> None:
    """反向对照：绿的一轮不许往失败桶里加数（否则面板就成了噪声）。"""
    before = _total()
    app = _app(ScriptedModel([ChatResponse(content="好的", finish_reason="stop")]))
    async with await _client(app) as c:
        r = await c.post("/chat/run-2", json={"text": "你好"})
    assert r.status_code == 200
    assert _total() == before
    app.state._store.close()
