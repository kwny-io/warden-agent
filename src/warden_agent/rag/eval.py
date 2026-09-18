"""检索质量评测：把 RAG 从"看起来能用"变成"有数字"。

为什么需要它：RAG 特别容易自我欺骗 —— "我做了向量检索"和"检索真的准"是两件事。
没有指标，就只能拿"demo 里那条刚好命中了"来自我安慰，而 demo 的词往往是自己挑的。
这里用一套**标注过的问答对**算出三个业界通用指标：

  · top-1 准确率 —— 第一条命中是不是期望来源
  · recall@k    —— 期望来源是否出现在前 k 条（默认 k=3）
  · MRR         —— 期望来源排名倒数的均值（越靠前越高）

用法：
    python -m warden_agent.rag.eval                 # 默认离线词频嵌入
    # 换真语义嵌入、看数字变化（任何 OpenAI 兼容 /embeddings 端点）：
    WARDEN_EMBED_BASE_URL=https://api.example.com/v1 \\
    WARDEN_EMBED_API_KEY=sk-xxx WARDEN_EMBED_MODEL=text-embedding-3-small \\
        python -m warden_agent.rag.eval

⚠️ 报告里会写明**当前用的是哪个嵌入器**。离线词频嵌入 ≠ 语义检索；
   对外引用数字时必须连嵌入器一起说明，否则就是过度声称。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from warden_agent.rag.corpus import POLICY_CORPUS
from warden_agent.rag.knowledge import VectorStore, embedder_from_env

# ---- 标注问答对：(问题, 期望命中的正文片段) ----
#
# 特意混入"改了说法、关键词重合低"的问法（如第 3、7 条）：
# 这类问题才真正区分"词面匹配"和"语义检索"。词频嵌入在这里会掉分，
# 而这个掉分是**真实的、值得被看见的**——它正是"要不要上语义嵌入"的依据。
RETRIEVAL_CASES: tuple[tuple[str, str], ...] = (
    ("报销要走什么流程", "财务"),
    ("出差住酒店每晚能报多少钱", "600"),
    ("我想请几天假出去玩，有什么规定", "15 天"),
    ("晚上加班了能不能换成休息", "调休"),
    ("跨城出差坐高铁还是飞机", "高铁"),
    ("几点上下班", "9:00"),
    ("能不能把客户的合同金额告诉媒体", "不得对外披露"),
)


def build_corpus_store(embedder_name_hint: str = "") -> tuple[VectorStore, str]:
    """按环境变量建带标注来源的知识库，返回 (store, 嵌入器名)。"""
    embedder, name = embedder_from_env(os.environ)
    store = VectorStore(embedder=embedder)
    for source, text in POLICY_CORPUS:
        store.add(text, source=source)
    return store, (embedder_name_hint or name)


@dataclass(frozen=True)
class CaseOutcome:
    """一条问法的检索结果判定。"""

    query: str
    expected: str
    rank: int | None    # 期望片段首次出现的排名（1-based）；None = 前 k 条都没命中


@dataclass(frozen=True)
class RetrievalReport:
    """一次检索质量评测的报告。"""

    embedder_name: str
    k: int
    outcomes: tuple[CaseOutcome, ...]

    @property
    def top1(self) -> float:
        """第一条就命中的比例。"""
        if not self.outcomes:
            return 0.0
        return sum(1 for o in self.outcomes if o.rank == 1) / len(self.outcomes)

    @property
    def recall_at_k(self) -> float:
        """前 k 条内命中的比例。"""
        if not self.outcomes:
            return 0.0
        return sum(1 for o in self.outcomes if o.rank is not None) / len(self.outcomes)

    @property
    def mrr(self) -> float:
        """平均倒数排名（命中的越靠前越接近 1）。"""
        if not self.outcomes:
            return 0.0
        return sum(1 / o.rank for o in self.outcomes if o.rank) / len(self.outcomes)

    def misses(self) -> tuple[CaseOutcome, ...]:
        """前 k 条都没命中的问法 —— 这些就是当前的失败样例。"""
        return tuple(o for o in self.outcomes if o.rank is None)


def evaluate(store: VectorStore, k: int = 3,
             cases: tuple[tuple[str, str], ...] = RETRIEVAL_CASES,
             embedder_name: str = "") -> RetrievalReport:
    """跑一遍标注问答对，算 top-1 / recall@k / MRR。"""
    outcomes: list[CaseOutcome] = []
    for query, expected in cases:
        hits = store.search_hits(query, top_k=k)
        rank: int | None = None
        for i, hit in enumerate(hits, start=1):
            if expected in hit.text:
                rank = i
                break
        outcomes.append(CaseOutcome(query=query, expected=expected, rank=rank))
    return RetrievalReport(embedder_name=embedder_name, k=k, outcomes=tuple(outcomes))


def format_report(report: RetrievalReport) -> str:
    lines = [
        f"嵌入器：{report.embedder_name}",
        f"语料块数：{len(POLICY_CORPUS)}   问法数：{len(report.outcomes)}   k={report.k}",
        "-" * 46,
        f"top-1 准确率   {report.top1:>6.1%}",
        f"recall@{report.k}      {report.recall_at_k:>6.1%}",
        f"MRR            {report.mrr:>6.3f}",
    ]
    if report.misses():
        lines.append("-" * 46)
        lines.append(f"未命中（前 {report.k} 条都没有期望片段）：")
        for o in report.misses():
            lines.append(f"  · {o.query!r} —— 期望片段 {o.expected!r}")
    return "\n".join(lines)


def main() -> None:
    store, name = build_corpus_store()
    report = evaluate(store, embedder_name=name)
    print(format_report(report))


if __name__ == "__main__":
    main()
