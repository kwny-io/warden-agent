"""typed_reply：类型化结果交付测试（结构化输出 → 校验 → 还原成对象）。"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from pydantic import BaseModel, Field
from tests.conftest import weather_tool

from warden_agent.model.model import AgentChatModel, ChatRequest, ChatResponse
from warden_agent.policy.policy import PolicyEngine
from warden_agent.runtime.session import AgentSession, TypedOutputError
from warden_agent.store.sqlite import SqliteStore


class WeatherReport(BaseModel):
    """期望模型按这个 schema 返回的"类型化最终结果"。"""

    city: str
    temperature_c: int = Field(description="摄氏温度")
    condition: str


class JsonModel(AgentChatModel):
    """固定返回一段 JSON 字符串的假模型（模拟模型走了结构化输出）。"""

    def __init__(self, json_text: str) -> None:
        self._json = json_text
        self.calls = 0
        self.last_request: ChatRequest | None = None

    def chat(self, request: ChatRequest) -> ChatResponse:
        self.calls += 1
        self.last_request = request
        return ChatResponse(content=self._json, finish_reason="stop")


def _policy() -> PolicyEngine:
    return PolicyEngine()  # 空策略 = 全部放行


def _store() -> SqliteStore:
    return SqliteStore(Path(tempfile.mkdtemp()) / "t.db")


def test_typed_reply_返回类型化对象() -> None:
    """模型返回符合 schema 的 JSON → 还原成 WeatherReport 实例，且字段正确。"""
    store = _store()
    model = JsonModel('{"city": "上海", "temperature_c": 25, "condition": "晴"}')
    sess = AgentSession(run_id="r-typed1", model=model, catalog=weather_tool(),
                        policy_engine=_policy(), store=store)

    result = sess.run_typed(WeatherReport, "上海天气怎么样？")

    assert isinstance(result, WeatherReport)
    assert result.city == "上海"
    assert result.temperature_c == 25
    assert result.condition == "晴"


def test_typed_reply_请求带了结构化输出schema() -> None:
    """会话应把 reply_type 的 JSON Schema 作为 structured_output 传给模型。"""
    store = _store()
    model = JsonModel('{"city": "北京", "temperature_c": 18, "condition": "多云"}')
    sess = AgentSession(run_id="r-typed2", model=model, catalog=weather_tool(),
                        policy_engine=_policy(), store=store)

    sess.run_typed(WeatherReport, "北京天气？")

    assert model.last_request is not None
    schema = model.last_request.structured_output
    assert schema is not None
    assert "city" in schema["properties"]
    assert schema["properties"]["temperature_c"]["type"] == "integer"


def test_typed_reply_校验失败抛TypedOutputError() -> None:
    """模型返回缺字段/类型错的 JSON → 抛 TypedOutputError，不透传坏数据。"""
    store = _store()
    # temperature_c 用了字符串，且缺 condition —— 不符合 schema
    model = JsonModel('{"city": "上海", "temperature_c": "25"}')
    sess = AgentSession(run_id="r-typed3", model=model, catalog=weather_tool(),
                        policy_engine=_policy(), store=store)

    with pytest.raises(TypedOutputError):
        sess.run_typed(WeatherReport, "上海天气？")


def test_typed_reply_非JSON内容也报错() -> None:
    """模型返回的不是合法 JSON → 同样抛 TypedOutputError。"""
    store = _store()
    model = JsonModel("我今天心情不错")  # 不是结构化 JSON
    sess = AgentSession(run_id="r-typed4", model=model, catalog=weather_tool(),
                        policy_engine=_policy(), store=store)

    with pytest.raises(TypedOutputError):
        sess.run_typed(WeatherReport, "上海天气？")
