"""向量索引：把"进程内全量扫描"换成**可剪枝的索引**，并把向量**持久化**。

原来的样子（本轮要解决的）：`VectorStore` 把向量存在两个 list 里，检索时**逐条算余弦**——
语料一大就是 O(N×dim)，而且**不落盘**：进程一重启，所有 chunk 都要重新嵌入（真语义嵌入还要重花钱）。

做法（零新依赖）：
  1. `LinearIndex`：暴力扫描（**保留为参照与兜底**——结果就是"标准答案"）；
  2. `SparseInvertedIndex`：**倒排索引**（维度 → 命中的文档列表）。查询时只对"与查询共享非零维度"
     的文档打分——这是**精确**剪枝（点积在非负向量下，不共享任何非零维就必然是 0 分），不是近似。
     默认的"词频哈希"嵌入是**高度稀疏**的（几千维里只有几十个非零），所以剪枝很有效。
  3. `build_index()` 按**稀疏度**自动选：稀疏 → 倒排；稠密（真语义嵌入）→ 线性扫描。
  4. 稠密的规模化需要真 ANN（FAISS / pgvector）——**如实说**，见 `docs/operations.md` 的已知边界。

⚠️ 关于"ANN"的诚实口径：倒排索引给的是**精确**结果（只是少算了必然为 0 的候选）。
   所以它是"更快的精确检索"，**不是**近似最近邻。真 ANN（HNSW/IVF）要引入外部库，本轮没做。
"""

from __future__ import annotations

from typing import Any, Protocol


def dot_similarity(a: list[float], b: list[float]) -> float:
    """相似度打分：**输入已 L2 归一化**时，点积就等于余弦相似度。

    ⚠️ 沿用本项目原有口径（`rag.knowledge.cosine_similarity` 就是它）——**刻意不除范数**：
    检索是热路径，给每一对都开根号太贵；归一化在嵌入阶段就做完了。
    索引与线性扫描共用这一个函数，保证"换索引"不改变任何一个分数。
    """
    return sum(x * y for x, y in zip(a, b, strict=True))


class VectorIndex(Protocol):
    """向量索引：能增量加、能按相似度取 top_k（返回 `(下标, 分数)`）。"""

    def add(self, vector: list[float]) -> None: ...

    def search(self, query: list[float], top_k: int) -> list[tuple[int, float]]: ...

    @property
    def size(self) -> int: ...


class LinearIndex:
    """暴力扫描：**精确**、结果就是参照答案。语料大时是 O(N) 全量比较。"""

    def __init__(self) -> None:
        self._vectors: list[list[float]] = []

    def add(self, vector: list[float]) -> None:
        self._vectors.append(vector)

    @property
    def size(self) -> int:
        return len(self._vectors)

    def search(self, query: list[float], top_k: int) -> list[tuple[int, float]]:
        scored = [
            (i, dot_similarity(query, v)) for i, v in enumerate(self._vectors)
        ]
        # 排序键带上下标：让"同分"时的顺序**确定**（否则倒排/线性两种实现的顺序可能不同，
        # 测试就没法做严格对照）。
        scored.sort(key=lambda t: (-t[1], t[0]))
        return scored[:top_k]


class SparseInvertedIndex:
    """稀疏向量的**倒排索引**：维度 → 含该维非零的文档下标。

    检索时只对"与查询共享至少一个非零维度"的文档打分。在**非负**向量下，
    不共享任何非零维 ⇒ 点积为 0 ⇒ 余弦为 0——所以这是**精确剪枝**，不是近似。
    （语料小、或候选不足 top_k 时，会把"必然 0 分"的文档按原顺序补上，保证与暴力扫描等价。）
    """

    def __init__(self) -> None:
        self._vectors: list[list[float]] = []
        self._postings: dict[int, list[int]] = {}

    def add(self, vector: list[float]) -> None:
        idx = len(self._vectors)
        self._vectors.append(vector)
        for dim, value in enumerate(vector):
            if value:
                self._postings.setdefault(dim, []).append(idx)

    @property
    def size(self) -> int:
        return len(self._vectors)

    @property
    def postings_count(self) -> int:
        """倒排表里"维度 → 文档"的条目数（用于观察索引规模）。"""
        return sum(len(v) for v in self._postings.values())

    def candidate_ids(self, query: list[float]) -> list[int]:
        """与查询共享非零维度的文档下标（**已排序**，便于观察剪枝效果）。"""
        found: set[int] = set()
        for dim, value in enumerate(query):
            if value:
                found.update(self._postings.get(dim, ()))
        return sorted(found)

    def search(self, query: list[float], top_k: int) -> list[tuple[int, float]]:
        if not self._vectors:
            return []
        candidates = self.candidate_ids(query)
        scored = [(i, dot_similarity(query, self._vectors[i])) for i in candidates]
        if len(scored) < top_k:
            # 候选不足：把剩下的补上（它们点积必然是 0），否则会漏掉"全 0 分"的文档
            have = set(candidates)
            scored.extend((i, 0.0) for i in range(len(self._vectors)) if i not in have)
        else:
            scored = [t for t in scored if t[1] > 0.0]
        scored.sort(key=lambda t: (-t[1], t[0]))
        return scored[:top_k]


def sparsity(vector: list[float]) -> float:
    """零元素占比（0~1）。越高越适合倒排索引。"""
    if not vector:
        return 0.0
    zeros = sum(1 for v in vector if not v)
    return zeros / len(vector)


def build_index(
    vectors: list[list[float]], *, sparse_threshold: float = 0.5
) -> VectorIndex:
    """按稀疏度自动选索引：稀疏（默认阈值 >50% 是零）→ 倒排；否则线性。

    为什么按稀疏度选：倒排索引的收益来自"大部分维度是零"。真语义嵌入通常是**稠密**的
    （几乎没有零），倒排就退化成"几乎全量扫描"——那时线性扫描反而更简单直接。
    """
    if not vectors:
        return LinearIndex()
    # 稀疏 → 倒排（能剪枝）；稠密 → 线性（倒排对稠密向量没有收益）
    index: VectorIndex = (
        SparseInvertedIndex() if sparsity(vectors[0]) >= sparse_threshold else LinearIndex()
    )
    for vec in vectors:
        index.add(vec)
    return index


# ---------------------------------------------------------------------------
# 稀疏编码：持久化时只存非零维度（词频哈希是几千维里几十个非零，全存太浪费）
# ---------------------------------------------------------------------------


def encode_sparse(vector: list[float]) -> dict[str, Any]:
    """把向量编成 `{"dim": N, "nz": {下标: 值}}`——只存非零。"""
    return {
        "dim": len(vector),
        "nz": {str(i): v for i, v in enumerate(vector) if v},
    }


def decode_sparse(payload: dict[str, Any]) -> list[float]:
    """`encode_sparse` 的逆操作（缺失的维度补 0）。"""
    dim = int(payload.get("dim", 0))
    vector = [0.0] * dim
    for key, value in (payload.get("nz") or {}).items():
        i = int(key)
        if 0 <= i < dim:
            vector[i] = float(value)
    return vector
