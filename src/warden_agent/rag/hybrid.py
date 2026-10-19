"""混合检索：BM25 字面分 + 向量语义分，用 RRF（倒数排名融合）合并。

为什么不是"二选一"：
  · 字面分（BM25）在**标识符**上不可替代——`WARDEN_PLANNER`、`E1024`、"600" 这类
    东西的"语义"很稀薄，向量反而容易把它排到近义但无关的段落上；
  · 向量分在**换说法**上不可替代——"请几天假出去玩" 与 "年假 15 天/年" 字面几乎不重叠。
  两条腿缺一条就会在对应的那类查询上系统性掉分。

为什么用 RRF 而不是"加权求和"：
  余弦相似度和 BM25 分的量纲完全不同（前者 [-1,1] 有界，后者无上界、随语料长度漂），
  加权求和要先做分数归一化，而归一化本身又是一个要调的参数。
  RRF 只看**名次**：score(d) = Σ 1/(k + rank_i(d))，无需归一化、无需调权重，k=60 是
  文献里的常用默认（Cormack et al. 2009）。

诚实边界：RRF 不产生"可解释的相关度"，它只保证"两路都靠前的更靠前"。
对外仍应报 top-1 / MRR（见 `rag/eval.py`），不要报 RRF 分数本身。
"""

from __future__ import annotations

import math
from collections import Counter

from warden_agent.rag.tokens import terms

# BM25 的两个经验参数：k1 控制词频饱和，b 控制文档长度惩罚。
BM25_K1 = 1.5
BM25_B = 0.75
# RRF 的平滑常数：越大越"温和"（名次差异被压平），60 是常用默认。
RRF_K = 60


class LexicalIndex:
    """BM25 倒排：**增量**加入文本，查询时返回 (块序号, BM25 分)。

    为什么自己做而不引 `rank_bm25`：这个仓库的依赖面是要能离线跑的，
    而 BM25 的全部数学就是下面那十几行；多一个依赖换不来任何东西。
    """

    def __init__(self, *, k1: float = BM25_K1, b: float = BM25_B) -> None:
        self._k1 = k1
        self._b = b
        self._tf: list[Counter[str]] = []      # 每块的词频
        self._len: list[int] = []              # 每块的词元数
        self._df: Counter[str] = Counter()     # 文档频率
        self._total_len = 0

    def add(self, text: str) -> int:
        """加入一块文本，返回它的块序号（与 `VectorStore._chunks` 同序）。"""
        toks = terms(text)
        tf = Counter(toks)
        index = len(self._tf)
        self._tf.append(tf)
        self._len.append(len(toks))
        self._total_len += len(toks)
        for term in tf:
            self._df[term] += 1
        return index

    def __len__(self) -> int:
        return len(self._tf)

    def _idf(self, term: str) -> float:
        n = len(self._tf)
        df = self._df.get(term, 0)
        # BM25 的 idf 用 log((N - df + 0.5)/(df + 0.5) + 1)，保证非负（不会因高频词变负分）
        return math.log((n - df + 0.5) / (df + 0.5) + 1.0)

    def scores(self, query: str) -> list[float]:
        """返回与块数等长的 BM25 分（没有命中的块是 0.0）。"""
        n = len(self._tf)
        if n == 0:
            return []
        avgdl = self._total_len / n if n else 0.0
        q_terms = terms(query)
        out = [0.0] * n
        if not q_terms:
            return out
        for term in set(q_terms):
            idf = self._idf(term)
            if idf <= 0:
                continue
            for i, tf in enumerate(self._tf):
                f = tf.get(term, 0)
                if not f:
                    continue
                dl = self._len[i] or 1
                denom = f + self._k1 * (1 - self._b + self._b * dl / (avgdl or 1.0))
                out[i] += idf * (f * (self._k1 + 1)) / denom
        return out

    def top_k(self, query: str, k: int) -> list[tuple[int, float]]:
        """按分排序的前 k 个 (块序号, 分)；分为 0 的块不进结果（没命中就是没命中）。"""
        scored = [(i, s) for i, s in enumerate(self.scores(query)) if s > 0.0]
        scored.sort(key=lambda x: (-x[1], x[0]))
        return scored[:k]


def rrf_fuse(
    ranked: list[list[tuple[int, float]]], *, k: int = RRF_K, top_k: int = 3
) -> list[tuple[int, float]]:
    """把多路"按分排序"的结果按名次融合，返回 (块序号, RRF 分) 的前 top_k。

    同一块在几路里都靠前 → 分数叠加。只出现在一路里的块仍然保留（只是分低），
    因为"只有字面命中"和"只有语义命中"都是有效信号。
    """
    fused: dict[int, float] = {}
    for results in ranked:
        for rank, (idx, _score) in enumerate(results, start=1):
            fused[idx] = fused.get(idx, 0.0) + 1.0 / (k + rank)
    ordered = sorted(fused.items(), key=lambda x: (-x[1], x[0]))
    return ordered[:top_k]
