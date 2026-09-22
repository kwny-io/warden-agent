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

⚠️ 关于"ANN"的诚实口径：
  · `LinearIndex` 是暴力扫描（精确，参照答案）；
  · `SparseInvertedIndex` 给的是**精确**结果（只是少算了必然为 0 的候选）——是"更快的精确检索"；
  · `IVFIndex` 才是**真 ANN**（近似最近邻）：k-means 聚类 + 只扫最近 `nprobe` 个簇，
    用少量召回换速度。**零新依赖**（纯 Python），但对超大语料仍不如 FAISS/pgvector 这类
    原生实现——需要真正上规模时再换外部向量库。
"""

from __future__ import annotations

import math
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


class IVFIndex:
    """倒排文件索引（IVF）——**真 ANN**（近似最近邻），纯 Python、零依赖。

    做法：先用 k-means 把向量聚成 `nlist` 个簇（每簇一个质心），检索时只扫
    "与查询最接近的 `nprobe` 个簇"。这是**近似**：真近邻若落在未探测的簇里就会被漏掉，
    召回率随 `nprobe` 上升；`nprobe >= nlist` 时扫全部簇 ⇒ 退化为精确（等价于暴力扫描）。

    与 `SparseInvertedIndex` 的区别要分清：
      · 倒排（稀疏）是**精确**剪枝（不共享非零维必然 0 分，不丢结果）；
      · IVF 是**近似**（用召回换速度）——这才是"ANN"。

    聚类初始化用**确定性最远点**（不是随机）：先取第一个向量，之后每次取"与已有质心最不相似"
    的向量。既确定可复现，也避免引入 RNG（索引构建不该有随机性）。

    ⚠️ 诚实边界：纯 Python 实现适合"几十万级以内、且要零依赖"的场景；再往上（百万级/高维）
    应换 FAISS / pgvector 这类原生库。
    """

    def __init__(self, *, nlist: int = 8, nprobe: int = 2, iters: int = 10) -> None:
        if nlist < 1:
            raise ValueError("nlist 必须 >= 1")
        if nprobe < 1:
            raise ValueError("nprobe 必须 >= 1")
        self.nlist = nlist
        self.nprobe = nprobe
        self.iters = iters
        self._vectors: list[list[float]] = []
        self._centroids: list[list[float]] = []
        self._lists: list[list[int]] = []
        self._built = False

    def add(self, vector: list[float]) -> None:
        self._vectors.append(vector)
        self._built = False  # 新向量进来 → 需要重新聚类

    @property
    def size(self) -> int:
        return len(self._vectors)

    @property
    def list_sizes(self) -> list[int]:
        """每个簇里的向量数（观察聚类是否均衡）。"""
        return [len(members) for members in self._lists]

    @staticmethod
    def _normalize(vector: list[float]) -> list[float]:
        norm = math.sqrt(sum(x * x for x in vector))
        return [x / norm for x in vector] if norm else list(vector)

    def _init_centroids(self, k: int) -> list[list[float]]:
        """确定性最远点初始化：每次取与已有质心最不相似的向量作新质心。"""
        centroids = [self._normalize(self._vectors[0])]
        while len(centroids) < k:
            worst_idx, worst_sim = 0, None
            for i, vec in enumerate(self._vectors):
                sim = max(dot_similarity(vec, c) for c in centroids)
                if worst_sim is None or sim < worst_sim:
                    worst_idx, worst_sim = i, sim
            centroids.append(self._normalize(self._vectors[worst_idx]))
        return centroids

    def _assign(self, centroids: list[list[float]]) -> list[list[int]]:
        lists: list[list[int]] = [[] for _ in centroids]
        for i, vec in enumerate(self._vectors):
            # 取相似度最大的质心；同分时取下标小的（确定性）
            best = max(range(len(centroids)), key=lambda j: (dot_similarity(vec, centroids[j]), -j))
            lists[best].append(i)
        return lists

    def _fit(self) -> None:
        n = len(self._vectors)
        if n == 0:
            self._centroids, self._lists, self._built = [], [], True
            return
        k = min(self.nlist, n)
        centroids = self._init_centroids(k)
        for _ in range(self.iters):
            lists = self._assign(centroids)
            dim = len(self._vectors[0])
            new_centroids: list[list[float]] = []
            for j, members in enumerate(lists):
                if not members:
                    new_centroids.append(centroids[j])  # 空簇保留原质心
                    continue
                acc = [0.0] * dim
                for i in members:
                    for d, value in enumerate(self._vectors[i]):
                        acc[d] += value
                new_centroids.append(self._normalize(acc))
            centroids = new_centroids
        self._centroids = centroids
        self._lists = self._assign(centroids)
        self._built = True

    def search(self, query: list[float], top_k: int) -> list[tuple[int, float]]:
        if not self._vectors:
            return []
        if not self._built:
            self._fit()
        probe = min(self.nprobe, len(self._centroids))
        # 选最近的 nprobe 个簇
        order = sorted(
            range(len(self._centroids)),
            key=lambda j: (-dot_similarity(query, self._centroids[j]), j),
        )[:probe]
        candidates: list[int] = []
        for j in order:
            candidates.extend(self._lists[j])
        scored = [(i, dot_similarity(query, self._vectors[i])) for i in candidates]
        scored.sort(key=lambda t: (-t[1], t[0]))
        return scored[:top_k]


def sparsity(vector: list[float]) -> float:
    """零元素占比（0~1）。越高越适合倒排索引。"""
    if not vector:
        return 0.0
    zeros = sum(1 for v in vector if not v)
    return zeros / len(vector)


def build_index(
    vectors: list[list[float]],
    *,
    sparse_threshold: float = 0.5,
    kind: str = "auto",
    nlist: int = 8,
    nprobe: int = 2,
) -> VectorIndex:
    """构造向量索引。

    `kind`：
      - `"auto"`（默认）：按稀疏度自动选——稀疏（>50% 是零）→ 倒排；稠密 → 线性。
        为什么按稀疏度：倒排的收益来自"大部分维度是零"；真语义嵌入通常稠密，
        倒排会退化成"几乎全量扫描"，那时线性更简单直接。
      - `"linear"`：暴力扫描（精确参照）。
      - `"inverted"`：稀疏倒排（精确剪枝）。
      - `"ivf"`：**真 ANN**（k-means + nprobe 探测；`nprobe>=nlist` 即精确）。
        稠密大规模语料想要"用召回换速度"时用它。
    """
    if not vectors:
        return LinearIndex()
    if kind == "linear":
        index: VectorIndex = LinearIndex()
    elif kind == "inverted":
        index = SparseInvertedIndex()
    elif kind == "ivf":
        index = IVFIndex(nlist=nlist, nprobe=nprobe)
    elif kind == "auto":
        # 稀疏 → 倒排（能剪枝）；稠密 → 线性（倒排对稠密向量没有收益）
        index = (
            SparseInvertedIndex()
            if sparsity(vectors[0]) >= sparse_threshold
            else LinearIndex()
        )
    else:
        raise ValueError(f"未知的索引类型: {kind!r}")
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
