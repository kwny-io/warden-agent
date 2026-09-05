"""Memory 记忆能力测试：候选流、作用域、冲突消解、过期清理、审计、抽取、工具。"""
from __future__ import annotations

import datetime as dt

from warden_agent.memory import (
    InMemoryMemoryStore,
    MemoryActor,
    MemoryContent,
    MemoryKind,
    MemoryScope,
    MemoryService,
    extract_facts,
    make_memory_tools,
    propose_from_text,
)


def _service(now=None) -> MemoryService:
    return MemoryService(InMemoryMemoryStore())


# ---- 候选流：propose → approve / reject ----
def test_propose_后待确认_approve_后生效() -> None:
    svc = _service()
    proposal = svc.propose(
        MemoryScope.USER, "preference",
        MemoryContent(kind=MemoryKind.TEXT, text="用户喜欢简洁"),
    )
    # 刚提议是候选（PENDING），recall 还查不到
    assert proposal.item.status.name == "PENDING"
    assert svc.recall(MemoryScope.USER, key="preference") == []

    svc.approve(proposal)
    found = svc.recall(MemoryScope.USER, key="preference")
    assert len(found) == 1
    assert found[0].content.text == "用户喜欢简洁"


def test_reject_后不入库() -> None:
    svc = _service()
    proposal = svc.propose(MemoryScope.USER, "k", MemoryContent(kind=MemoryKind.TEXT, text="x"))
    svc.reject(proposal)
    assert svc.recall(MemoryScope.USER, key="k") == []


# ---- 冲突消解 ----
def test_冲突_approve替代旧记忆() -> None:
    svc = _service()
    p1 = svc.propose(MemoryScope.SESSION, "conclusion", MemoryContent(text="结论A"))
    svc.approve(p1, replacing=False)

    p2 = svc.propose(MemoryScope.SESSION, "conclusion", MemoryContent(text="结论B"))
    assert p2.item.status.name == "CONFLICTED"  # 检测到冲突

    svc.approve(p2, replacing=True)
    latest = svc.recall(MemoryScope.SESSION, key="conclusion")[0]
    assert latest.content.text == "结论B"
    # 旧记忆被软删除
    assert p1.item.status.name == "TOMBSTONED"


def test_冲突_保留旧放弃新() -> None:
    svc = _service()
    p1 = svc.propose(MemoryScope.USER, "k", MemoryContent(text="旧"))
    svc.approve(p1, replacing=False)
    p2 = svc.propose(MemoryScope.USER, "k", MemoryContent(text="新"))
    svc.resolve_conflict(p2, keep_new=False)
    assert svc.recall(MemoryScope.USER, key="k")[0].content.text == "旧"


# ---- 作用域隔离 ----
def test_作用域隔离() -> None:
    svc = _service()
    svc.propose(MemoryScope.USER, "k", MemoryContent(text="用户级")).item
    # RUN 作用域查不到 USER 的（权限过滤）
    assert svc.recall(MemoryScope.RUN, key="k") == []
    svc.recall(MemoryScope.USER, key="k")  # 能从正确作用域查到


# ---- 过期清理 ----
def test_过期后不可检索_purge物理清除() -> None:
    svc = _service()
    past = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
    p = svc.propose(
        MemoryScope.USER, "k", MemoryContent(text="会过期的记忆"),
        expires_at=past,
    )
    svc.approve(p, replacing=False)
    # 已过期 → 检索不到
    assert svc.recall(MemoryScope.USER, key="k") == []
    # request_purge 标记过期，execute_purge 清除
    assert svc.request_purge() >= 1
    svc.execute_purge()


# ---- 审计轨迹 ----
def test_审计记录() -> None:
    svc = _service()
    p = svc.propose(MemoryScope.USER, "k", MemoryContent(text="x"),
                    actor=MemoryActor("human", "user-1"))
    svc.approve(p)
    actions = [e.action for e in p.item.audit]
    assert "propose" in actions
    assert "approve" in actions


# ---- 确定性事实抽取 ----
def test_extract_facts() -> None:
    facts = dict(extract_facts("用户叫小明，喜欢简洁回答"))
    assert "identity" in facts
    assert "偏好" in facts.get("preference", "")
    assert "小明" in facts.get("identity", "")


def test_propose_from_text() -> None:
    svc = _service()
    ids = propose_from_text(svc, MemoryScope.SESSION, "用户叫小红，喜欢详细回答")
    assert len(ids) >= 2  # identity + preference 两条候选


# ---- 工具 ----
def test_memory_tools_remember_recall() -> None:
    from warden_agent.tool.catalog import ToolCatalog

    svc = _service()
    catalog = ToolCatalog()
    for spec in make_memory_tools(svc, MemoryScope.SESSION):
        catalog.register(spec)

    catalog.execute("memory.remember", {"key": "preference", "text": "用户喜欢图表"})
    out = catalog.execute("memory.recall", {"key": "preference"})
    assert "用户喜欢图表" in str(out)


# ---- WORKSPACE 工作区作用域（多 Agent 共享工作记忆）----
def test_workspace_作用域_propose_recall() -> None:
    svc = _service()
    prop = svc.propose(MemoryScope.WORKSPACE, "调研发现",
                       MemoryContent(kind=MemoryKind.TEXT, text="年假是15天"))
    svc.approve(prop)
    got = svc.recall_text(MemoryScope.WORKSPACE, key="调研发现")
    assert "年假是15天" in got


def test_workspace_与session隔离() -> None:
    svc = _service()
    prop = svc.propose(MemoryScope.WORKSPACE, "k", MemoryContent(text="工作区的"))
    svc.approve(prop)
    # SESSION 域看不到 WORKSPACE 的条目（scope 硬分区）
    assert svc.recall(MemoryScope.SESSION, key="k") == []


def test_workspace_工具可写读() -> None:
    from warden_agent.tool.catalog import ToolCatalog

    svc = _service()
    catalog = ToolCatalog()
    for spec in make_memory_tools(svc, MemoryScope.WORKSPACE):
        catalog.register(spec)
    catalog.execute("memory.remember", {"key": "结论", "text": "建议采用方案B"})
    out = catalog.execute("memory.recall", {"key": "结论"})
    assert "方案B" in str(out)
