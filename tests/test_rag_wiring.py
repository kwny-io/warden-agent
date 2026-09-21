"""RAG 接线测试：`knowledge.search` 真的挂到产品路径上了。

改之前：`rag/` 只被 `demo_e2e.py` 与 `rag/eval.py` 引用，`build_agent` / `build_app` /
`run_server` 从不构造 `VectorStore` —— 代码、评测、来源引用都在，但模型手里的工具清单里
**没有** `knowledge.search`。这就是"实现了没接线"。

这里锁住接上之后的行为：
  - `augment_catalog(knowledge=...)` 把工具注册进目录（三种来源都支持）；
  - HTTP 服务的 `/capabilities` 能看到它，且**模型真的能调**、结果带来源引用；
  - `run_server` 的环境开关解析正确。

测试一律传**显式 VectorStore**（用 `env={}` 造离线嵌入），不依赖机器上的
`WARDEN_EMBED_*` 环境变量，避免测试联网、保证确定性。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import httpx
import pytest
from tests.conftest import ScriptedModel, weather_tool

from warden_agent.agent import augment_catalog, build_agent
from warden_agent.model.model import ChatResponse, ToolCall
from warden_agent.policy.policy import PolicyEngine
from warden_agent.rag import VectorStore, build_knowledge
from warden_agent.store.sqlite import SqliteStore
from warden_agent.tool.catalog import ToolCatalog
from warden_agent.web.run_server import _knowledge_from_env
from warden_agent.web.server import build_app


def _offline_store() -> VectorStore:
    """内置离线语料的向量库（强制离线嵌入，不碰网络与环境变量）。"""
    store, _name, _sources = build_knowledge(True, env={})
    return store


# ---------- build_knowledge ----------


def test_内置语料可建库() -> None:
    store, embedder_name, sources = build_knowledge(True, env={})
    assert len(store) > 0
    assert embedder_name == "offline-term-frequency"
    assert "员工手册.pdf" in sources


def test_目录可建库() -> None:
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        (base / "制度.md").write_text("年假制度：正式员工每年 15 天带薪年假。", encoding="utf-8")
        (base / "notes.txt").write_text("报销流程：填单后交经理审批。", encoding="utf-8")
        (base / "ignore.json").write_text("{}", encoding="utf-8")  # 非文本后缀应跳过
        (base / "empty.md").write_text("   \n", encoding="utf-8")   # 空文件应跳过

        store, _name, sources = build_knowledge(base, env={})
        assert sorted(sources) == ["notes.txt", "制度.md"]
        assert len(store) == 2


def test_目录不存在时报错() -> None:
    with pytest.raises(NotADirectoryError):
        build_knowledge("no/such/dir", env={})


def test_传False无意义要报错() -> None:
    """不启用应该传 None；传 False 属于用错参数，明确报错而不是静默。"""
    with pytest.raises(ValueError):
        build_knowledge(False, env={})


def test_注入向量库原样使用() -> None:
    store = _offline_store()
    got, name, sources = build_knowledge(store, env={})
    assert got is store  # 原样使用，不重新索引
    assert name == "provided"
    assert sources and "注入" in sources[0]


# ---------- augment_catalog ----------


def test_注册knowledge_search工具() -> None:
    catalog = ToolCatalog()
    extra = augment_catalog(catalog, knowledge=_offline_store())

    assert "knowledge.search" in [t.name for t in catalog.all()]
    assert extra["knowledge_embedder"] == "provided"
    # 工具真的能查出内容，且带来源引用（可溯源是 RAG 的要点）
    out = str(catalog.execute("knowledge.search", {"query": "年假怎么休"}))
    assert "来源：" in out
    assert "年假" in out


def test_不传knowledge就不注册工具() -> None:
    catalog = ToolCatalog()
    extra = augment_catalog(catalog)
    assert "knowledge.search" not in [t.name for t in catalog.all()]
    assert "knowledge_store" not in extra


def test_目录路径直接可用() -> None:
    with tempfile.TemporaryDirectory() as d:
        Path(d, "a.md").write_text("差旅住宿：一线城市每晚不超过 600 元。", encoding="utf-8")
        catalog = ToolCatalog()
        augment_catalog(catalog, knowledge=d)  # 传路径，内部自动索引
        out = str(catalog.execute("knowledge.search", {"query": "住宿标准"}))
        assert "600" in out
        assert "a.md" in out


def test_build_agent也能接knowledge() -> None:
    """SDK 面同样接上（不只是 HTTP 面）。"""
    agent = build_agent(knowledge=_offline_store())
    assert agent is not None  # 装配不报错即说明参数被接受并走到 augment_catalog


# ---------- HTTP 产品路径 ----------


def _client(app):  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


@pytest.mark.asyncio
async def test_http能力清单含knowledge_search() -> None:
    app = build_app(
        model=ScriptedModel([ChatResponse(content="好", finish_reason="stop")]),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=SqliteStore(Path(tempfile.mkdtemp()) / "t.db"),
        knowledge=_offline_store(),
    )
    async with _client(app) as c:
        caps = (await c.get("/capabilities")).json()
        assert "knowledge.search" in caps["tools"]
        # 报出嵌入器名，避免"词频嵌入被当成语义检索"；并给出已索引来源数
        # （注入的向量库走 "provided" 分支，来源列表是它自身的占位项，所以是 1）
        assert caps["features"]["knowledge_embedder"] == "provided"
        assert caps["features"]["knowledge_sources"] == 1


@pytest.mark.asyncio
async def test_未开RAG时能力清单不谎报() -> None:
    app = build_app(
        model=ScriptedModel([ChatResponse(content="好", finish_reason="stop")]),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=SqliteStore(Path(tempfile.mkdtemp()) / "t.db"),
    )
    async with _client(app) as c:
        caps = (await c.get("/capabilities")).json()
        assert "knowledge.search" not in caps["tools"]
        assert caps["features"]["knowledge_embedder"] is None
        assert caps["features"]["knowledge_sources"] == 0


@pytest.mark.asyncio
async def test_http里模型真的能调knowledge_search并拿到来源() -> None:
    """端到端：模型发起调用 → 循环执行 → 观察里带回带来源的资料。"""
    store = SqliteStore(Path(tempfile.mkdtemp()) / "t.db")
    script = [
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="c1", name="knowledge.search", arguments={"query": "年假"})],
            finish_reason="tool_calls"),
        ChatResponse(content="据《员工手册.pdf》，年假 15 天。", finish_reason="stop"),
    ]
    app = build_app(
        model=ScriptedModel(script),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=store,
        knowledge=_offline_store(),
    )
    async with _client(app) as c:
        r = await c.post("/chat/run-rag", json={"text": "年假怎么休"})
        assert r.status_code == 200
        body = r.json()
        assert body["kind"] == "final"
        # 工具确实执行了：观察消息里有检索结果和来源引用
        tool_msgs = [m for m in (body.get("messages") or []) if m["role"] == "tool"]
        assert tool_msgs, "knowledge.search 没有被执行"
        assert "员工手册.pdf" in tool_msgs[0]["content"]
        assert "来源：" in tool_msgs[0]["content"]


# ---------- 环境开关 ----------


def test_环境开关解析() -> None:
    assert _knowledge_from_env({}) is None
    assert _knowledge_from_env({"WARDEN_KNOWLEDGE": ""}) is None
    assert _knowledge_from_env({"WARDEN_KNOWLEDGE": "0"}) is None
    assert _knowledge_from_env({"WARDEN_KNOWLEDGE": "off"}) is None
    assert _knowledge_from_env({"WARDEN_KNOWLEDGE": "1"}) is True
    assert _knowledge_from_env({"WARDEN_KNOWLEDGE": "true"}) is True
    assert _knowledge_from_env({"WARDEN_KNOWLEDGE": "D:/docs"}) == "D:/docs"
