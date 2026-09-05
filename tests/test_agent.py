"""build_agent()：一键装配门面测试。"""
from __future__ import annotations

import pytest
from pydantic import BaseModel
from tests.conftest import weather_tool

from warden_agent.agent import Agent, InMemoryRunStore, build_agent
from warden_agent.model.fake import FakeModel
from warden_agent.policy.policy import Decision, PolicyEngine, PolicyResult
from warden_agent.store.sqlite import SqliteStore


def test_build_agent_返回Agent对象() -> None:
    agent = build_agent(tools=weather_tool().all())
    assert isinstance(agent, Agent)


def test_build_agent_离线假模型能聊天() -> None:
    agent = build_agent(provider=None, tools=weather_tool().all())
    reply = agent.chat("上海天气用weather.get查一下")
    assert isinstance(reply, str)


def test_build_agent_无工具也能聊() -> None:
    agent = build_agent()
    reply = agent.chat("你好")
    assert isinstance(reply, str)
    assert reply  # 非空


def test_build_agent_typed_reply_还原对象() -> None:
    from warden_agent.model.model import AgentChatModel, ChatRequest, ChatResponse

    class Report(BaseModel):
        city: str
        temp: int

    class JsonModel(AgentChatModel):
        def chat(self, request: ChatRequest) -> ChatResponse:
            return ChatResponse(
                content='{"city": "上海", "temp": 25}', finish_reason="stop"
            )

    agent = build_agent(provider=JsonModel())
    result = agent.typed_reply(Report, "上海天气？")
    assert isinstance(result, Report)
    assert result.city == "上海"
    assert result.temp == 25


def test_build_agent_自定义策略生效() -> None:
    """给 build_agent 传入的 PolicyEngine 应真正参与工具门禁。"""
    from warden_agent.runtime.session import PolicyDenied

    engine = PolicyEngine()
    engine.add(lambda name, args: PolicyResult(Decision.DENY, "全禁"))
    agent = build_agent(provider=FakeModel(), tools=weather_tool().all(),
                        policy_engine=engine)

    with pytest.raises(PolicyDenied):
        agent.chat("上海天气用weather.get")


def test_build_agent_可传SqliteStore() -> None:
    """显式传 SqliteStore 时能正常建会话（持久化可用）。"""
    import tempfile
    from pathlib import Path

    store = SqliteStore(Path(tempfile.mkdtemp()) / "agent.db")
    agent = build_agent(tools=weather_tool().all(), store=store)
    reply = agent.chat("你好")
    assert isinstance(reply, str)
    store.close()


def test_InMemoryRunStore_实现RunStore接口() -> None:
    from warden_agent.core.run.status import AgentRun, RunStatus
    from warden_agent.model.model import Message
    from warden_agent.store.base import RunStore

    store: RunStore = InMemoryRunStore()
    run = AgentRun("r1")
    run.mark_queued()
    store.save_run(run)
    assert store.load_run("r1").status == RunStatus.QUEUED
    store.append_message("r1", Message(role="user", content="hi"))
    assert store.load_messages("r1")[0].content == "hi"
    store.save_pending_approval("r1", "a", "fs.delete", {"p": "/x"}, "需批准")
    assert store.load_pending_approval("r1")[1] == "fs.delete"
