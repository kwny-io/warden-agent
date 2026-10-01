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
from warden_agent.memory.models import MemoryStatus


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
    repo = InMemoryMemoryStore()
    svc = MemoryService(repo)
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
    assert svc.execute_purge() >= 1
    # 真的清掉了：find_ref 不再返回该行（回归：以前 search 只找 ACTIVE，purge 是空操作）
    assert repo.find_ref(MemoryScope.USER, "k") == []


def test_pending真的列出候选() -> None:
    """回归：pending() 曾用 search()（只返回 ACTIVE）枚举 → 永远为空。"""
    repo = InMemoryMemoryStore()
    svc = MemoryService(repo)
    svc.propose(MemoryScope.USER, "k1", MemoryContent(text="候选一"))
    svc.propose(MemoryScope.SESSION, "k2", MemoryContent(text="候选二"))

    assert {p.item.key for p in svc.pending()} == {"k1", "k2"}
    assert {p.item.key for p in svc.pending(MemoryScope.USER)} == {"k1"}


def test_墓碑也被execute_purge真删() -> None:
    repo = InMemoryMemoryStore()
    svc = MemoryService(repo)
    p = svc.propose(MemoryScope.USER, "k", MemoryContent(text="x"))
    svc.reject(p)  # TOMBSTONED
    assert repo.find_ref(MemoryScope.USER, "k")  # 软删后仍在库里
    assert svc.execute_purge() == 1
    assert repo.find_ref(MemoryScope.USER, "k") == []  # 物理清除


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

    # ---- 两阶段：remember 只入候选（写的是"先进入待确认候选"，就得真的是候选）----
    queued = catalog.execute("memory.remember", {"key": "preference", "text": "用户喜欢图表"})
    assert "待确认" in str(queued)
    # 还没确认 ⇒ recall 取不到（生效前不可见）
    assert "用户喜欢图表" not in str(catalog.execute("memory.recall", {"key": "preference"}))
    assert len(svc.pending(MemoryScope.SESSION)) == 1

    confirmed = catalog.execute("memory.confirm", {"key": "preference"})
    assert "已确认" in str(confirmed)
    out = catalog.execute("memory.recall", {"key": "preference"})
    assert "用户喜欢图表" in str(out)
    # 确认后候选区清空（不留下永远没人处理的脏候选）
    assert svc.pending(MemoryScope.SESSION) == []


def test_memory_confirm_无候选时给明确提示() -> None:
    from warden_agent.tool.catalog import ToolCatalog

    svc = _service()
    catalog = ToolCatalog()
    for spec in make_memory_tools(svc, MemoryScope.SESSION):
        catalog.register(spec)
    out = catalog.execute("memory.confirm", {"key": "never-saved"})
    assert "没有待确认的候选" in str(out)


def test_memory_冲突候选_确认后新值替掉旧值() -> None:
    from warden_agent.tool.catalog import ToolCatalog

    svc = _service()
    catalog = ToolCatalog()
    for spec in make_memory_tools(svc, MemoryScope.SESSION):
        catalog.register(spec)
    catalog.execute("memory.remember", {"key": "preference", "text": "旧偏好：表格"})
    catalog.execute("memory.confirm", {"key": "preference"})

    conflict = catalog.execute("memory.remember", {"key": "preference", "text": "新偏好：图表"})
    assert "冲突" in str(conflict)
    catalog.execute("memory.confirm", {"key": "preference"})
    out = str(catalog.execute("memory.recall", {"key": "preference"}))
    assert "新偏好：图表" in out
    assert "旧偏好：表格" not in out


# ---- WORKSPACE 工作区作用域（多 Agent 共享工作记忆）----
def test_候选枚举_含冲突项且按归属隔离() -> None:
    """`candidates()` 的语义：既要看得到冲突项，又不能把别人的候选露出去。

    - 含 CONFLICTED：冲突项才是最需要人看一眼的；只列 PENDING 会让
      "和已有记忆冲突"的候选从界面上消失，等于冲突检测白做。
    - 按 owner 过滤：候选区是人的待办，不过滤就是跨人泄露。加 owner 参数
      而不是让调用方自己过滤，是因为这一层筛错了没人看得出来。
    """
    svc = _service()
    svc.approve(svc.propose(
        MemoryScope.USER, "k", MemoryContent(text="旧值"), owner="alice"))
    svc.propose(MemoryScope.USER, "k", MemoryContent(text="冲突的新值"), owner="alice")
    svc.propose(MemoryScope.USER, "k", MemoryContent(text="别人的候选"), owner="bob")

    mine = svc.candidates(MemoryScope.USER, owner="alice")
    assert len(mine) == 1
    assert mine[0].item.status == MemoryStatus.CONFLICTED
    assert svc.conflict_preview(mine[0]) == "旧值"   # 要替掉什么，得给人看清楚
    assert svc.conflict_preview(svc.candidates(MemoryScope.USER, owner="bob")[0]) == ""
    assert svc.candidates(MemoryScope.SESSION, owner="alice") == []  # 作用域硬分区


def test_越权确认与拒绝都无效() -> None:
    """服务层就把归属卡死：三个入口（工具卡/HTTP/以后的 CLI）共用这一层，不会各漏一个。"""
    svc = _service()
    prop = svc.propose(MemoryScope.USER, "k", MemoryContent(text="alice 的"), owner="alice")
    uid = prop.item.uid

    assert svc.approve_candidate(uid, scope=MemoryScope.SESSION, owner="alice") is None
    assert svc.approve_candidate(uid, scope=MemoryScope.USER, owner="bob") is None
    assert svc.reject_candidate(uid, scope=MemoryScope.USER, owner="bob") is False
    assert svc.reject_candidate("不存在的-uid", scope=MemoryScope.USER, owner="alice") is False
    # 上面四次越权/错参都不能把候选弄坏
    assert [c.item.uid for c in svc.candidates(MemoryScope.USER, owner="alice")] == [uid]

    item = svc.approve_candidate(uid, scope=MemoryScope.USER, owner="alice")
    assert item is not None and item.status == MemoryStatus.ACTIVE
    # 已生效的不能再确认一次（不是候选了）
    assert svc.approve_candidate(uid, scope=MemoryScope.USER, owner="alice") is None


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
    catalog.execute("memory.confirm", {"key": "结论"})
    out = catalog.execute("memory.recall", {"key": "结论"})
    assert "方案B" in str(out)


# ---- 归属隔离（安全回归）----


def test_记忆按归属隔离_互不可见() -> None:
    """scope 只区分"哪一类"，owner 才区分"谁的"——没有 owner，记忆就是全部署共享的。"""
    svc = _service()
    for owner, text in (("alice", "A 的偏好"), ("bob", "B 的偏好")):
        svc.approve(svc.propose(
            MemoryScope.USER, "preference", MemoryContent(text=text), owner=owner))

    assert [i.content.text for i in
            svc.recall(MemoryScope.USER, key="preference", owner="alice")] == ["A 的偏好"]
    assert [i.content.text for i in
            svc.recall(MemoryScope.USER, key="preference", owner="bob")] == ["B 的偏好"]
    # 不过滤（owner=None）才看得到全部——那是管理路径，不能用在面向用户的读路径上
    # （注意按 key 查走的是"取该键最新一条"的语义，所以要按文本搜才能数出两条）
    assert len(svc.recall(MemoryScope.USER, text_like="偏好")) == 2


def test_记忆工具按运行期上下文归属_不来自入参() -> None:
    """memory.remember/recall 是装配期建好、全局共享的；归属来自会话上下文，模型改不了。"""
    from warden_agent.memory import owner_scope
    from warden_agent.tool.catalog import ToolCatalog

    svc = _service()
    catalog = ToolCatalog()
    for spec in make_memory_tools(svc, MemoryScope.SESSION):
        catalog.register(spec)

    with owner_scope("alice"):
        catalog.execute("memory.remember", {"key": "city", "text": "常驻上海"})
        catalog.execute("memory.confirm", {"key": "city"})
        assert "上海" in str(catalog.execute("memory.recall", {"key": "city"}))

    with owner_scope("bob"):
        # bob 在同一作用域下也查不到 alice 的记忆（否则就是跨租户投毒/泄露）
        assert "上海" not in str(catalog.execute("memory.recall", {"key": "city"}))
        # 也不能替 alice 确认候选（越权确认 = 替别人把内容写进记忆）
        assert "已确认" not in str(catalog.execute("memory.confirm", {"key": "city"}))



# ---- extract_facts / propose_from_text：接到产品路径上，并且归属不丢 ----
def test_remember_text从用户原话抽候选() -> None:
    """改之前：`extract_facts` / `propose_from_text` 只有单测在调，产品里没有任何入口。

    一个模块"有实现、有测试、没人用"就是形同虚设——测试只证明它自己是对的，
    不证明产品里有这条路。这张卡把它接上：用户说了一大句，模型不必自己拆句子。
    """
    from warden_agent.tool.catalog import ToolCatalog

    svc = _service()
    catalog = ToolCatalog()
    for spec in make_memory_tools(svc, MemoryScope.USER, owner="alice"):
        catalog.register(spec)

    out = str(catalog.execute("memory.remember_text",
                              {"text": "我喜欢简洁回答，记住我住在杭州"}))
    assert "候选" in out and "待确认" in out

    # 抽出来的候选真的进了候选区，而且**仍待确认**（不是直接生效）
    queued = svc.candidates(MemoryScope.USER, owner="alice")
    assert queued, "抽出来的候选必须进候选区"
    # 生效前 recall 取不到
    assert "杭州" not in str(catalog.execute("memory.recall", {"key": "fact"}))


def test_remember_text候选归属当前用户而不是空归属() -> None:
    """归属是这张卡最容易错的地方：漏传 owner ⇒ 候选落进"公共"桶，多用户下互相看见。

    `propose_from_text` 以前不接收 owner（默认空串），所以它一旦被产品调用，
    抽出来的候选就全归"部署级共享"——A 的偏好会出现在 B 的候选区里。
    """
    from warden_agent.tool.catalog import ToolCatalog

    svc = _service()
    catalog = ToolCatalog()
    for spec in make_memory_tools(svc, MemoryScope.USER, owner="alice"):
        catalog.register(spec)
    catalog.execute("memory.remember_text", {"text": "我喜欢简洁回答"})

    mine = svc.candidates(MemoryScope.USER, owner="alice")
    assert [c.item.owner for c in mine] == ["alice"]
    assert svc.candidates(MemoryScope.USER, owner="bob") == []  # 不是公共桶


def test_remember_text抽不到时如实说抽不到() -> None:
    """抽不到就说抽不到，别返回一句含糊的"已记录"。"""
    from warden_agent.tool.catalog import ToolCatalog

    svc = _service()
    catalog = ToolCatalog()
    for spec in make_memory_tools(svc, MemoryScope.USER, owner="alice"):
        catalog.register(spec)
    out = str(catalog.execute("memory.remember_text", {"text": "今天天气不错"}))
    assert "没抽出" in out
    assert svc.candidates(MemoryScope.USER, owner="alice") == []
