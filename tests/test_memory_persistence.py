"""记忆持久化测试：`SqliteMemoryStore` 让记忆**重启不丢**。

改之前：`MemoryRepository` 协议只有一个 `InMemoryMemoryStore` 实现，而 `augment_catalog`
固定用它。后果是 `USER` 作用域（"跨会话的用户级记忆"）在语义上成立、在实现上落空——
进程一退记忆就没了。多副本之间也不共享。

这里锁住接上之后的行为：
  - `SqliteMemoryStore` 与内存版语义一致（find / find_ref / latest / search）；
  - **换一个新实例读同一个库，数据还在**（模拟进程重启）；
  - 候选流（propose → approve）落盘；
  - 产品路径（HTTP 的 `/memory/{scope}`）能读到落盘的记忆。
"""

from __future__ import annotations

import datetime as _dt
import tempfile
from pathlib import Path

import httpx
import pytest
from tests.conftest import ScriptedModel, weather_tool

from warden_agent.agent import augment_catalog
from warden_agent.memory import (
    MemoryActor,
    MemoryContent,
    MemoryItem,
    MemoryScope,
    MemoryService,
    MemoryStatus,
    SqliteMemoryStore,
)
from warden_agent.memory.models import new_uid
from warden_agent.model.model import ChatResponse
from warden_agent.policy.policy import PolicyEngine
from warden_agent.store.sqlite import SqliteStore
from warden_agent.tool.catalog import ToolCatalog
from warden_agent.web.server import build_app


def _db() -> str:
    return str(Path(tempfile.mkdtemp()) / "mem.db")


def _item(
    scope: MemoryScope = MemoryScope.USER,
    key: str = "pref",
    text: str = "用户偏好简洁回答",
    status: MemoryStatus = MemoryStatus.ACTIVE,
) -> MemoryItem:
    return MemoryItem(
        uid=new_uid(),
        scope=scope,
        key=key,
        content=MemoryContent(text=text),
        status=status,
    )


# ---------- 存储语义 ----------


def test_保存与读取往返() -> None:
    store = SqliteMemoryStore(_db())
    item = _item()
    store.save(item)

    got = store.find(item.uid)
    assert got is not None
    assert got.uid == item.uid
    assert got.scope == MemoryScope.USER
    assert got.key == "pref"
    assert got.content.text == "用户偏好简洁回答"
    assert got.status == MemoryStatus.ACTIVE
    assert got.version == item.version


def test_find_ref与latest() -> None:
    store = SqliteMemoryStore(_db())
    old = _item(key="pref", text="旧偏好")
    old.updated_at = _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)
    new = _item(key="pref", text="新偏好")
    new.updated_at = _dt.datetime(2026, 6, 1, tzinfo=_dt.UTC)
    other = _item(scope=MemoryScope.SESSION, key="pref", text="别的会话")

    for i in (old, new, other):
        store.save(i)

    ref = store.find_ref(MemoryScope.USER, "pref")
    assert {i.content.text for i in ref} == {"旧偏好", "新偏好"}
    latest = store.latest(MemoryScope.USER, "pref")
    assert latest is not None and latest.content.text == "新偏好"
    # 作用域隔离：SESSION 的 key 相同但互不影响
    assert store.latest(MemoryScope.SESSION, "pref").content.text == "别的会话"  # type: ignore[union-attr]


def test_latest优先有效项() -> None:
    """已软删的比有效的新，也应返回有效的那条（与内存版语义一致）。"""
    store = SqliteMemoryStore(_db())
    live = _item(text="有效")
    live.updated_at = _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)
    dead = _item(text="已删", status=MemoryStatus.TOMBSTONED)
    dead.updated_at = _dt.datetime(2026, 6, 1, tzinfo=_dt.UTC)
    store.save(live)
    store.save(dead)

    latest = store.latest(MemoryScope.USER, "pref")
    assert latest is not None and latest.content.text == "有效"


def test_search按文本与状态过滤() -> None:
    store = SqliteMemoryStore(_db())
    store.save(_item(key="a", text="用户喜欢深色主题"))
    store.save(_item(key="b", text="用户喜欢浅色主题"))
    store.save(_item(key="c", text="已删的深色偏好", status=MemoryStatus.TOMBSTONED))

    hits = store.search(MemoryScope.USER, text_like="深色")
    assert [h.key for h in hits] == ["a"]  # 非 ACTIVE 的排除在外
    assert len(store.search(MemoryScope.USER)) == 2  # 不带过滤只看 ACTIVE


def test_审计轨迹落盘() -> None:
    store = SqliteMemoryStore(_db())
    item = _item()
    item.record("create", MemoryActor(kind="user", id="alice"))
    store.save(item)

    got = store.find(item.uid)
    assert got is not None
    assert got.audit and got.audit[-1].action == "create"
    assert got.audit[-1].actor.id == "alice"


# ---------- 重启不丢 ----------


def test_换新实例读同一库_记忆还在() -> None:
    """模拟进程重启：写一个实例，用**另一个**实例读同一个文件。"""
    db = _db()
    writer = SqliteMemoryStore(db)
    item = _item(text="跨会话也要记得")
    writer.save(item)
    writer.close()

    reader = SqliteMemoryStore(db)  # 新实例 = 重启后的进程
    got = reader.find(item.uid)
    assert got is not None
    assert got.content.text == "跨会话也要记得"


def test_候选流落盘_approve后重启仍有效() -> None:
    db = _db()
    service = MemoryService(SqliteMemoryStore(db))
    proposal = service.propose(
        MemoryScope.USER, "city", MemoryContent(text="用户常驻上海")
    )
    # 候选状态也应落盘（PENDING），重启后仍能继续审批
    assert SqliteMemoryStore(db).find(proposal.item.uid).status == MemoryStatus.PENDING  # type: ignore[union-attr]

    service.approve(proposal)

    fresh = SqliteMemoryStore(db)  # 重启
    assert fresh.latest(MemoryScope.USER, "city").status == MemoryStatus.ACTIVE  # type: ignore[union-attr]
    assert len(MemoryService(fresh).recall(MemoryScope.USER)) == 1


# ---------- 装配与产品路径 ----------


def test_augment_catalog用给定记忆库并标记持久化() -> None:
    repository = SqliteMemoryStore(_db())
    catalog = ToolCatalog()
    extra = augment_catalog(catalog, memory=True, memory_repository=repository)

    assert extra["memory_persistent"] is True
    assert "memory.remember" in [t.name for t in catalog.all()]


def test_augment_catalog不传记忆库时用进程内() -> None:
    catalog = ToolCatalog()
    extra = augment_catalog(catalog, memory=True)
    assert extra["memory_persistent"] is False  # 内存实现，重启即丢


@pytest.mark.asyncio
async def test_http能读到落盘的记忆() -> None:
    """产品路径验证：预先落盘一条 USER 记忆，HTTP 的 /memory/user 应能查到。"""
    repository = SqliteMemoryStore(_db())
    service = MemoryService(repository)
    service.approve(service.propose(
        MemoryScope.USER, "city", MemoryContent(text="用户常驻上海")
    ))

    app = build_app(
        model=ScriptedModel([ChatResponse(content="好", finish_reason="stop")]),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=SqliteStore(Path(tempfile.mkdtemp()) / "t.db"),
        memory=True,
        memory_repository=repository,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        items = (await c.get("/memory/user")).json()
        assert [i["key"] for i in items] == ["city"]
        assert items[0]["text"] == "用户常驻上海"
        assert items[0]["status"] == "ACTIVE"
        # 能力清单要能区分"落盘"与"进程内"——运维/前端据此判断重启会不会丢
        caps = (await c.get("/capabilities")).json()
        assert caps["features"]["memory"] is True
        assert caps["features"]["memory_persistent"] is True
