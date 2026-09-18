"""检索质量评测与来源标识的测试。

三件事要钉死：
1. **指标算法本身对**（否则数字好看也没意义）；
2. **source_id 唯一**——同一份文档下的多个条款必须能区分，否则"引用"是假的；
3. **检索质量有下界**——这是给 `_term_frequency_embedder` 的 `dim` 加护栏：
   当初 dim=256 时 recall@3 只有 57%，把 dim 调到 4096 才到 100%。
   谁要是把 dim 改回去，这条会红。
"""

from __future__ import annotations

from warden_agent.rag.corpus import POLICY_CORPUS
from warden_agent.rag.eval import RETRIEVAL_CASES, CaseOutcome, RetrievalReport, evaluate
from warden_agent.rag.knowledge import (
    VectorStore,
    cosine_similarity,
    embedder_from_env,
    hash_embedder,
    make_knowledge_tool,
)
from warden_agent.tool.catalog import ToolCatalog


def _policy_store() -> VectorStore:
    store = VectorStore()
    for source, text in POLICY_CORPUS:
        store.add(text, source=source)
    return store


# ---------------- 指标算法 ----------------


def test_指标算法_全部命中且都排第一() -> None:
    report = RetrievalReport(
        embedder_name="t", k=3,
        outcomes=(CaseOutcome("q1", "n", 1), CaseOutcome("q2", "n", 1)),
    )
    assert report.top1 == 1.0
    assert report.recall_at_k == 1.0
    assert report.mrr == 1.0


def test_指标算法_一条没命中() -> None:
    report = RetrievalReport(
        embedder_name="t", k=3,
        outcomes=(CaseOutcome("q1", "n", 2), CaseOutcome("q2", "n", None)),
    )
    assert report.recall_at_k == 0.5          # 2 条里中 1 条
    assert report.top1 == 0.0                 # 没有一条排第一
    assert abs(report.mrr - 0.25) < 1e-9      # (1/2 + 0) / 2
    assert len(report.misses()) == 1


def test_空用例集不除以零() -> None:
    report = RetrievalReport(embedder_name="t", k=3, outcomes=())
    assert (report.top1, report.recall_at_k, report.mrr) == (0.0, 0.0, 0.0)


def test_评测能跑通且指标在合理区间() -> None:
    report = evaluate(_policy_store(), embedder_name="offline")
    for value in (report.top1, report.recall_at_k, report.mrr):
        assert 0.0 <= value <= 1.0
    assert len(report.outcomes) == len(RETRIEVAL_CASES)


# ---------------- 质量下界（dim 的护栏） ----------------


def test_离线检索的recall有下界_防止dim被改小() -> None:
    """dim=256 时 recall@3 只有 57%；当前 dim=4096 实测 100%。

    留 0.8 的余量：低于它说明嵌入维度/分词被改回了碰撞严重的配置。
    """
    report = evaluate(_policy_store(), embedder_name="offline")
    assert report.recall_at_k >= 0.8, (
        f"recall@3 掉到 {report.recall_at_k:.0%}，检查 _term_frequency_embedder 的 dim"
    )


# ---------------- source_id 唯一性 ----------------


def test_同源不同条款的source_id不重复() -> None:
    """《员工手册.pdf》下有报销/年假/调休三条 —— 引用必须能区分它们。"""
    store = _policy_store()
    hits = store.search_hits("报销", top_k=len(POLICY_CORPUS))
    ids = [h.source_id for h in hits]
    assert len(ids) == len(set(ids)), f"source_id 有重复：{ids}"


def test_source_id非空且带来源名() -> None:
    store = _policy_store()
    hit = store.search_hits("报销要走什么流程", top_k=1)[0]
    assert hit.source_id
    assert "员工手册" in hit.source_id


def test_未标来源时source_id为空() -> None:
    store = VectorStore()
    store.add("没有来源的一段话。")
    assert store.search_hits("没有来源", top_k=1)[0].source_id == ""


def test_调用方可显式指定source_id() -> None:
    store = VectorStore()
    store.add("一段话。", source="某文档.md", source_id="DOC-42")
    assert store.search_hits("一段话", top_k=1)[0].source_id == "DOC-42"


def test_引用标记里带得出source_id对应的来源() -> None:
    store = _policy_store()
    catalog = ToolCatalog()
    catalog.register(make_knowledge_tool(store))
    out = str(catalog.execute("knowledge.search", {"query": "出差住宿标准"}))
    assert "[引用" in out and "差旅制度" in out


# ---------------- 嵌入器选择：不许把词频当语义 ----------------


def test_默认是离线词频嵌入() -> None:
    _embedder, name = embedder_from_env({})
    assert name == "offline-term-frequency"


def test_配齐环境变量才切语义嵌入() -> None:
    _embedder, name = embedder_from_env({
        "WARDEN_EMBED_BASE_URL": "https://example.invalid/v1",
        "WARDEN_EMBED_API_KEY": "sk-x",
        "WARDEN_EMBED_MODEL": "text-embedding-3-small",
    })
    assert name.startswith("semantic:")


def test_只配一部分仍走离线_不半吊子切换() -> None:
    _embedder, name = embedder_from_env({"WARDEN_EMBED_BASE_URL": "https://example.invalid/v1"})
    assert name == "offline-term-frequency"


def test_离线嵌入是确定性的() -> None:
    """必须是确定性哈希：不能用内置 hash()，否则每进程启动结果都不同。"""
    store_a = _policy_store()
    store_b = _policy_store()
    q = "出差住酒店能报多少钱"
    assert [h.text for h in store_a.search_hits(q)] == [h.text for h in store_b.search_hits(q)]


def test_嵌入向量是归一化的_所以点积即余弦() -> None:
    """真实契约：嵌入函数输出已 L2 归一化，因此检索里直接用点积当余弦。"""
    v = hash_embedder("报销流程：先填报销单并附上发票")
    assert abs(cosine_similarity(v, v) - 1.0) < 1e-9


def test_未归一化输入只是点积_记录这个前提() -> None:
    """[0.3,0.4] 的模是 0.5，所以自比是 0.25 而不是 1 —— 记录前提，防误用。"""
    assert abs(cosine_similarity([0.3, 0.4], [0.3, 0.4]) - 0.25) < 1e-9
