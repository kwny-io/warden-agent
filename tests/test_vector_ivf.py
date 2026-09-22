"""IVF 近似最近邻（真 ANN）测试。

两条硬性质：
  1. `nprobe >= nlist` 时**必须与暴力扫描逐位一致**（扫全部簇 = 精确）；
  2. `nprobe` 小时召回率下降但仍有意义（用召回换速度）。
数据用**确定性伪随机**生成（不用 RNG，避免安全扫描器噪声）。
"""

from __future__ import annotations

import math

from warden_agent.rag.vector_index import (
    IVFIndex,
    LinearIndex,
    build_index,
)


def _seq(seed: int):
    """确定性伪随机序列（线性同余），返回 [0,1) 生成器。"""
    state = seed & 0x7FFFFFFF

    def nxt() -> float:
        nonlocal state
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        return state / 0x7FFFFFFF

    return nxt


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def _clustered(n_clusters: int = 4, per_cluster: int = 25, dim: int = 16, seed: int = 1):
    """造 K 个明显分开的簇（每簇中心不同，点在其附近）。返回 (vectors, query)。"""
    nxt = _seq(seed)
    centers = [_normalize([nxt() + (1 if d == c else 0) for d in range(dim)])
               for c in range(n_clusters)]
    vectors: list[list[float]] = []
    for c in range(n_clusters):
        for _ in range(per_cluster):
            jittered = [centers[c][d] + (nxt() - 0.5) * 0.1 for d in range(dim)]
            vectors.append(_normalize(jittered))
    queries = [vectors[i % len(vectors)] for i in range(20)]
    return vectors, queries


def test_nprobe等于nlist时与暴力扫描完全一致() -> None:
    vectors, queries = _clustered()
    linear = LinearIndex()
    ivf = IVFIndex(nlist=4, nprobe=4, iters=10)  # nprobe = nlist → 扫全部簇
    for v in vectors:
        linear.add(v)
        ivf.add(v)
    for q in queries:
        for k in (1, 5, 10):
            assert ivf.search(q, k) == linear.search(q, k), (k,)


def test_nprobe小时用召回换速度_但仍有意义() -> None:
    vectors, queries = _clustered()
    linear = LinearIndex()
    ivf = IVFIndex(nlist=4, nprobe=1, iters=10)
    for v in vectors:
        linear.add(v)
        ivf.add(v)

    hits = total = 0
    for q in queries:
        top_linear = {i for i, _ in linear.search(q, 10)}
        top_ivf = {i for i, _ in ivf.search(q, 10)}
        hits += len(top_linear & top_ivf)
        total += len(top_linear)
    recall = hits / total
    assert 0.75 <= recall <= 1.0, f"nprobe=1 的 recall@10={recall:.2f} 不在合理区间"


def test_空索引与增量add后重建() -> None:
    ivf = IVFIndex(nlist=4, nprobe=2)
    assert ivf.search([1.0, 0.0], 3) == []
    ivf.add([1.0, 0.0])
    ivf.add([0.0, 1.0])
    # add 之后 search 会惰性重建聚类，不应报错
    got = ivf.search([1.0, 0.0], 2)
    assert len(got) == 2 and got[0][0] == 0
    assert sum(ivf.list_sizes) == ivf.size == 2


def test_build_index支持ivf() -> None:
    vectors, queries = _clustered()
    idx = build_index(vectors, kind="ivf", nlist=4, nprobe=4)
    assert isinstance(idx, IVFIndex)
    linear = build_index(vectors, kind="linear")
    assert idx.search(queries[0], 5) == linear.search(queries[0], 5)


def test_build_index拒绝未知类型() -> None:
    import pytest

    with pytest.raises(ValueError):
        build_index([[1.0, 0.0]], kind="magic")
