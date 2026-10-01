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
from warden_agent.model.model import ChatResponse, ToolCall
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


def test_list_by_status_不套用ACTIVE过滤() -> None:
    """按状态枚举：PENDING/TOMBSTONED 不能被 `search()` 的 ACTIVE 过滤漏掉。"""
    store = SqliteMemoryStore(_db())
    store.save(_item(key="p", status=MemoryStatus.PENDING))
    store.save(_item(key="g", status=MemoryStatus.TOMBSTONED))

    assert store.search(MemoryScope.USER) == []  # search 只看 ACTIVE
    assert {i.key for i in store.list_by_status(MemoryStatus.PENDING)} == {"p"}
    assert {i.key for i in store.list_by_status()} == {"p", "g"}


def test_sqlite_purge经list_by_status真删行() -> None:
    store = SqliteMemoryStore(_db())
    svc = MemoryService(store)
    past = _dt.datetime(2020, 1, 1, tzinfo=_dt.UTC)
    p = svc.propose(MemoryScope.USER, "k", MemoryContent(text="过期"), expires_at=past)
    svc.approve(p, replacing=False)

    assert store.find_ref(MemoryScope.USER, "k")  # 软删前还在
    assert svc.request_purge() == 1
    assert svc.execute_purge() == 1
    assert store.find_ref(MemoryScope.USER, "k") == []  # 真删了（不再是空操作）


def test_带偏移的过期时间按UTC比较() -> None:
    """回归：expires_at 以 ISO 字符串落库，字符串比较在非 UTC 偏移下会得出错误结论。"""
    store = SqliteMemoryStore(_db())
    svc = MemoryService(store)
    # +08:00 的“过去”时间；归一为 UTC 后应与 now 正确比较
    past_cn = _dt.datetime(
        2020, 1, 1, tzinfo=_dt.timezone(_dt.timedelta(hours=8))
    )
    p = svc.propose(MemoryScope.USER, "k", MemoryContent(text="x"), expires_at=past_cn)
    svc.approve(p, replacing=False)

    assert svc.recall(MemoryScope.USER, key="k") == []
    assert svc.request_purge() == 1
    # store 的 purge_expired 也按 UTC-aware 比较后物理删除
    assert store.purge_expired(_dt.datetime.now(_dt.UTC)) == 1
    assert store.find_ref(MemoryScope.USER, "k") == []


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
    """产品路径验证：预先落盘一条**属于调用者**的 USER 记忆，HTTP 的 /memory/user 应能查到。

    匿名开发模式下调用者身份是 `demo-user`（`_identity` 的缺省），所以记忆必须带这个归属。
    """
    repository = SqliteMemoryStore(_db())
    service = MemoryService(repository)
    service.approve(service.propose(
        MemoryScope.USER, "city", MemoryContent(text="用户常驻上海"),
        owner="demo-user",
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


@pytest.mark.asyncio
async def test_http读不到别人的记忆_跨租户隔离() -> None:
    """安全回归：`/memory/{scope}` 只返回**调用者自己**的记忆。

    此前 endpoint 不做任何归属过滤，任一用户就能读到全部署所有人的记忆
    （以及被投毒的记忆）——scope 只区分"哪一类"、不区分"谁的"。
    """
    repository = SqliteMemoryStore(_db())
    service = MemoryService(repository)
    service.approve(service.propose(
        MemoryScope.USER, "secret", MemoryContent(text="别人的私事"),
        owner="someone-else",
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
        assert items == [], f"不应看到他人记忆，实际：{items}"


@pytest.mark.asyncio
async def test_http候选流_可确认可拒绝_并拒绝越权() -> None:
    """产品路径的两阶段记忆：候选可看、可确认、可拒绝；别人的候选既看不到也动不了。

    这条锁住的是"`memory.remember` 写的是待确认候选"这句承诺：候选必须在 HTTP 面上
    真的看得见（否则确认环节只是文案），且确认/拒绝必须按归属隔离
    （否则任一用户就能替别人把内容写进记忆，或删掉别人的候选）。
    """
    repository = SqliteMemoryStore(_db())
    service = MemoryService(repository)
    # 一条待确认候选（属于调用者）+ 一条别人的候选（不得出现）
    mine = service.propose(
        MemoryScope.USER, "city", MemoryContent(text="用户常驻上海"), owner="demo-user",
    )
    service.propose(
        MemoryScope.USER, "secret", MemoryContent(text="别人的草稿"), owner="someone-else",
    )

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
        pending = (await c.get("/memory/user/candidates")).json()
        assert [p["key"] for p in pending] == ["city"], f"不应看到他人候选：{pending}"
        assert pending[0]["status"] == "PENDING"

        # 待确认的内容不能提前生效
        assert (await c.get("/memory/user")).json() == []

        # 越权确认：uid 存在但不归调用者 ⇒ 404（不分化"存在"与"不归你"）
        other = service.candidates(MemoryScope.USER, owner="someone-else")[0]
        r = await c.post(f"/memory/user/candidates/{other.item.uid}/confirm")
        assert r.status_code == 404

        # 自己的可以确认，确认后候选区清空、/memory/{scope} 看得到
        r = await c.post(f"/memory/user/candidates/{mine.item.uid}/confirm")
        assert r.status_code == 200, r.text
        assert (await c.get("/memory/user/candidates")).json() == []
        active = (await c.get("/memory/user")).json()
        assert [i["text"] for i in active] == ["用户常驻上海"]

        # 拒绝：软删（候选消失，且不会出现在已生效列表里）
        again = service.propose(
            MemoryScope.USER, "noise", MemoryContent(text="不该留下的"), owner="demo-user",
        )
        r = await c.post(f"/memory/user/candidates/{again.item.uid}/reject")
        assert r.status_code == 200
        assert (await c.get("/memory/user/candidates")).json() == []
        assert [i["key"] for i in (await c.get("/memory/user")).json()] == ["city"]

        # 已确认/已拒绝的 uid 再确认一次 ⇒ 404（不能重放）
        r = await c.post(f"/memory/user/candidates/{mine.item.uid}/confirm")
        assert r.status_code == 404


def test_老库没有owner列_按迁移路径仍可读(tmp_path: Path) -> None:
    """升级兼容：`owner` 列是后加的，老库（没有该列）必须能被自动迁移且数据不丢。

    迁移口径：老行的 owner 视为空串（部署级共享）。所以按 owner='' 查得到、
    按具体用户查不到（这是有意的隔离行为，不是什么 bug）。
    """
    import sqlite3

    from warden_agent.memory import MemoryScope
    from warden_agent.memory.store import SqliteMemoryStore

    db_path = tmp_path / "old-memory.db"
    conn = sqlite3.connect(str(db_path))
    # 旧 schema：没有 owner 列
    conn.executescript(
        """
        CREATE TABLE memories (
            uid            TEXT PRIMARY KEY,
            scope          TEXT NOT NULL,
            key            TEXT NOT NULL,
            content_kind   TEXT NOT NULL,
            content_text   TEXT NOT NULL,
            content_data   TEXT NOT NULL,
            status         TEXT NOT NULL,
            actor_kind     TEXT NOT NULL,
            actor_id       TEXT NOT NULL,
            version        INTEGER NOT NULL,
            created_at     TEXT NOT NULL,
            updated_at     TEXT NOT NULL,
            expires_at     TEXT,
            conflicts_with TEXT,
            supersedes     TEXT,
            audit          TEXT NOT NULL
        );
        """
    )
    now = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("old-1", "USER", "city", "TEXT", "老记忆", "{}", "ACTIVE",
         "service", "default", 1, now, now, None, None, None, "[]"),
    )
    conn.commit()
    conn.close()

    repo = SqliteMemoryStore(db_path=db_path)   # 打开时自动补 owner 列
    item = repo.latest(MemoryScope.USER, "city", owner="")
    assert item is not None and item.content.text == "老记忆"
    assert item.owner == "", "老行的 owner 迁移后应为空串（部署级共享）"
    # 隔离口径：具体用户查不到这条（老数据不属于任何用户）
    assert repo.latest(MemoryScope.USER, "city", owner="alice") is None


@pytest.mark.asyncio
async def test_会话里调memory_remember_归属自动落到调用者() -> None:
    """产品路径回归：`memory.remember` 的归属来自**运行期上下文**，不是工具入参。

    `make_memory_tools(owner=None)` 是产品路径的用法（固定 owner 只给 SDK/测试）：
    归属从 `current_owner()` 读，而它是 AgentSession 在**执行工具那一刻**设置的
    （runtime/session.py 的 `_execute` → owner_scope）。

    这条测试是配合那次修复的回归件：owner_scope 原先包住整个流式生成器，在真实服务器上
    （uvicorn/Starlette 把生成器丢线程池逐块 next()）会跨 Context 报错——
        ValueError: <Token ... was created in a different Context>
    表现为浏览器连发第二条消息就 500。改成只包 `_execute` 后，模型在会话里提的候选
    必须带上调用者身份（匿名开发模式是 demo-user），否则 `/memory/user/candidates`
    查不到东西，用户永远没得确认，记忆功能就是半残。
    """
    repository = SqliteMemoryStore(_db())
    service = MemoryService(repository)
    app = build_app(
        model=ScriptedModel([
            ChatResponse(content=None, tool_calls=[
                ToolCall(id="c1", name="memory.remember",
                         arguments={"key": "city", "text": "用户常驻上海"})],
                finish_reason="tool_calls"),
            ChatResponse(content="记住了。", finish_reason="stop"),
        ]),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=SqliteStore(Path(tempfile.mkdtemp()) / "t.db"),
        memory=True,
        memory_repository=repository,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/chat/run-mem-owner", json={"text": "记住我常驻上海"})
        assert r.status_code == 200, r.text
        assert r.json()["kind"] == "final", r.json()   # 工具真被执行了，不是挂起等审批
        # memory.remember 的默认范围是 SESSION（工具入参只能给 key/text，模型选不了范围）
        candidates = (await c.get("/memory/session/candidates")).json()
        # 归属只能来自凭证/服务端身份：候选面**故意忽略** ?user_id=（_identity(request, None)），
        # 否则任一匿名调用者就能用查询参数把自己的候选挂在别人名下、或看到别人的候选。
        spoof = (await c.get("/memory/session/candidates?user_id=alice")).json()

    assert [x["key"] for x in candidates] == ["city"], (
        f"会话里提的候选没按调用者归属落库，实际：{candidates}"
    )
    assert candidates[0]["owner"] == "demo-user"
    assert candidates[0]["status"] == "PENDING", "remember 写的是待确认候选，不是直接生效"
    assert spoof == candidates, (
        f"候选面不能被 ?user_id= 冒充身份改写（应当原样忽略），实际：{spoof}"
    )
    # 归属不能来自工具入参：模型没有（也不该有）办法自报身份
    assert [c.item.owner for c in service.candidates(MemoryScope.SESSION, owner="demo-user")] \
        == ["demo-user"]
