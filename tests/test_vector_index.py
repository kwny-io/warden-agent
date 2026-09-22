"""向量索引与向量库持久化。

两件要盯的事：
  1. **换索引不能换结果**：倒排索引是"精确剪枝"（只跳过必然 0 分的候选），
     所以它的 top_k 必须与暴力扫描**逐位一致**——否则检索质量就悄悄变了。
  2. **落盘后重启不重新嵌入**：真语义嵌入按量计费，重启重算既慢又花钱。
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from warden_agent.rag.knowledge import VectorStore
from warden_agent.rag.vector_index import (
    LinearIndex,
    SparseInvertedIndex,
    build_index,
    decode_sparse,
    encode_sparse,
    sparsity,
)


def _sparse_vectors(n: int = 60, dim: int = 64, nnz: int = 4, seed: int = 7) -> list[list[float]]:
    """造一批**稀疏非负**向量（模拟词频哈希嵌入：维度多、非零少）。"""
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        vec = [0.0] * dim
        for d in rng.sample(range(dim), nnz):
            vec[d] = round(rng.random(), 3)
        out.append(vec)
    return out


# ---------- 索引正确性 ----------


def test_倒排索引与线性扫描结果逐位一致() -> None:
    vectors = _sparse_vectors()
    linear, inv = LinearIndex(), SparseInvertedIndex()
    for v in vectors:
        linear.add(v)
        inv.add(v)

    rng = random.Random(11)
    for _ in range(30):
        query = [0.0] * 64
        for d in rng.sample(range(64), 3):
            query[d] = round(rng.random(), 3)
        for top_k in (1, 3, 10, 100):          # 100 > 候选数，覆盖"候选不足补 0"的分支
            assert inv.search(query, top_k) == linear.search(query, top_k), top_k


def test_倒排索引确实剪枝了() -> None:
    """收益来自"只算共享非零维度的文档"——候选数应远小于全量。"""
    vectors = _sparse_vectors(n=200, dim=256, nnz=3)
    inv = build_index(vectors)
    assert isinstance(inv, SparseInvertedIndex)
    query = vectors[0]                       # 与第 0 条完全相同的查询
    candidates = inv.candidate_ids(query)
    assert inv.size == 200
    assert 0 in candidates
    assert len(candidates) < inv.size / 5, f"剪枝不明显：候选 {len(candidates)}/200"


def test_查询全零时不会丢结果() -> None:
    """全零查询与谁都无共享维度 → 候选为空，但仍要返回 top_k（全 0 分），且与线性一致。"""
    vectors = _sparse_vectors(n=10, dim=32)
    inv = build_index(vectors)
    assert isinstance(inv, SparseInvertedIndex)
    got = inv.search([0.0] * 32, 3)
    assert len(got) == 3
    assert all(score == 0.0 for _idx, score in got)
    # 与线性扫描逐位一致
    linear = LinearIndex()
    for v in vectors:
        linear.add(v)
    assert got == linear.search([0.0] * 32, 3)


def test_按稀疏度自动选索引() -> None:
    assert isinstance(build_index(_sparse_vectors()), SparseInvertedIndex)
    dense = [[0.3, 0.4, 0.5], [0.1, 0.9, 0.2]]          # 无零元素 → 稠密
    assert isinstance(build_index(dense), LinearIndex)
    assert isinstance(build_index([]), LinearIndex)      # 空库
    assert sparsity([0.0, 0.0, 1.0]) == pytest.approx(2 / 3)


def test_稀疏编码往返() -> None:
    vec = [0.0, 1.5, 0.0, 0.25, 0.0]
    payload = encode_sparse(vec)
    assert payload["dim"] == 5 and payload["nz"] == {"1": 1.5, "3": 0.25}
    assert decode_sparse(payload) == vec


# ---------- 向量库：检索行为不变 ----------


def test_检索与全量扫描等价_且来源引用保留() -> None:
    store = VectorStore()
    store.add("年假十五天，需提前申请。", source="员工手册.pdf")
    store.add("报销流程：填表、经理审批、财务打款。", source="员工手册.pdf")
    store.add("完全无关的天气话题。", source="杂记.txt")

    hits = store.search_hits("报销 经理审批", top_k=2)
    assert hits and "报销" in hits[0].text
    assert hits[0].source          # 来源引用还在
    assert hits[0].source_id
    assert store.size == 3


# ---------- 向量库：持久化 ----------


class _CountingEmbedder:
    """计数嵌入器：用来证明"重启后没有重新嵌入"。"""

    def __init__(self) -> None:
        from warden_agent.rag.knowledge import hash_embedder

        self.calls = 0
        self._inner = hash_embedder

    def __call__(self, text: str) -> list[float]:
        self.calls += 1
        return self._inner(text)


def test_落盘后重启_不重新嵌入且结果一致(tmp_path: Path) -> None:
    db = tmp_path / "rag.db"
    first = _CountingEmbedder()
    store = VectorStore(first, persist_path=db)
    store.add("年假十五天，需提前申请。", source="员工手册.pdf")
    store.add("报销流程：填表、经理审批、财务打款。", source="员工手册.pdf")
    before = store.search_hits("报销 经理审批", top_k=2)
    store.close()
    assert first.calls == store.size + 1      # 每块一次 + 查询一次

    # 模拟重启：新实例、新的计数嵌入器（若从盘上读回则不该再嵌入语料）
    second = _CountingEmbedder()
    reopened = VectorStore(second, persist_path=db)
    assert reopened.size == 2
    assert second.calls == 0, "重启后不该重新嵌入已落盘的语料"
    after = reopened.search_hits("报销 经理审批", top_k=2)
    assert second.calls == 1, "只有查询该嵌入一次"

    assert [(h.text, h.score, h.source_id) for h in after] == [
        (h.text, h.score, h.source_id) for h in before
    ]
    reopened.close()


def test_持久化库可继续追加(tmp_path: Path) -> None:
    db = tmp_path / "rag.db"
    store = VectorStore(persist_path=db)
    store.add("第一段：年假十五天。")
    store.close()

    reopened = VectorStore(persist_path=db)
    reopened.add("第二段：报销要经理审批。")
    assert reopened.size == 2
    assert "报销" in reopened.search_hits("报销审批", top_k=1)[0].text
    reopened.close()


def test_未开持久化时不落盘(tmp_path: Path) -> None:
    store = VectorStore()                       # 不传 persist_path
    store.add("只有内存。")
    assert store.size == 1
    assert not list(tmp_path.glob("*.db"))
    store.close()                               # 空操作，不该抛异常
