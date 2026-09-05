"""RAG（检索增强生成）—— 给 Agent 增加"记忆/知识库"能力。

我们把"知识检索"做成一个工具 knowledge.search，模型遇到不懂的问题时会自己去查。

RAG 的完整流程（三句话）：
  1. 把文档切成小块（chunk），每块算一个"向量"（embedding）存进向量库。
  2. 用户提问时，把问题也变成一个向量，跟库里所有块比"相似度"。
  3. 把最相似的几块取出来，连同问题一起塞给模型，模型就能"带着资料回答"。

本实现的设计要点：
  - 向量存储：VectorStore，存 chunk 文本 + embedding。
  - 嵌入函数可替换：默认用纯 Python 的哈希嵌入（零重依赖、离线可测、不花钱）；
    以后想接入 FastEmbed / 真嵌入 API，只需传一个函数进来，其他地方不用改。
  - 检索用余弦相似度，纯 numpy 实现。
  - 通过 function_tool 暴露成 knowledge.search 技能卡，模型自动会用。
"""
from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass

from warden_agent.tool.catalog import ToolSpec, function_tool

# 嵌入函数：输入一段文本，返回一个浮点向量（list[float]）
Embedder = Callable[[str], list[float]]


# ---- 默认嵌入：词袋（term-frequency）向量。可靠、离线、零依赖 ----
def _terms(text: str) -> list[str]:
    """把文本切成词/词组。中文按字粒度切会太碎，这里按 2~4 字窗口做词元。"""
    text = text.lower()
    # 英文词 + 中文的 2,3,4 字窗口，模拟"词"
    tokens: list[str] = re.findall(r"[a-z0-9]+", text)
    cjk = re.findall(r"[\u4e00-\u9fff]+", text)
    for seg in cjk:
        for n in (2, 3, 4):
            if len(seg) >= n:
                tokens.extend(seg[i:i + n] for i in range(len(seg) - n + 1))
    return tokens


def _term_frequency_embedder(text: str, dim: int = 256) -> list[float]:
    """词频哈希向量（固定维度）。

    每个词都往它哈希到的"固定维度位置"上加 1（不取反），再归一化。
    - 固定维度 => 任意两段文本的向量长度都一样，余弦相似度直接可比；
    - 用 hashlib 做确定哈希（不是内置 hash()，因为后者每次进程启动会被随机化，
      会导致同一句向量每次都不同，检索不稳定）；
    - 不加负号  => "提到同一个词的文本"必然在同一维度都有分量，相似度会高。
    想升级成语义向量（理解近义词），把本函数换成 FastEmbed 即可。
    """
    import hashlib

    vec = [0.0] * dim
    for t in _terms(text):
        digest = hashlib.md5(t.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dim
        vec[index] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def hash_embedder(text: str, dim: int = 32, vocab: int = 300) -> list[float]:
    """（保留）紧凑哈希嵌入，兼容旧接口。新代码建议用词频向量。"""
    return _term_frequency_embedder(text)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """计算两个向量的余弦相似度（-1~1，越高越像）。两个向量维度固定且相等。"""
    return sum(x * y for x, y in zip(a, b, strict=True))


@dataclass
class SourceHit:
    """一条带来源引用的检索命中。

    - text     命中的文本块（chunk）
    - score    相似度（越高越相关）
    - source   来源（文档标题/文件名/章节，用于引用溯源）；可能为空
    - source_id 来源的唯一标识（如文档名+序号），便于精确引用；可能为空
    """

    text: str
    score: float
    source: str = ""
    source_id: str = ""


class VectorStore:
    """一个极简的向量库：存 chunk + 向量，支持按相似度检索（可带来源引用）。"""

    def __init__(self, embedder: Embedder | None = None) -> None:
        self.embedder: Embedder = embedder or hash_embedder
        self._chunks: list[str] = []
        self._vectors: list[list[float]] = []
        # 与 _chunks 一一对应的来源标注（为空表示该块无来源）
        self._sources: list[str] = []

    def add(self, text: str, *, chunk_size: int = 400, overlap: int = 50,
            source: str | None = None, source_id: str | None = None) -> None:
        """把一段长文本切块后加入库中（带重叠避免切断语义）。

        - source     整段文本的来源名（如"员工手册.pdf"），切出的每块都继承它。
        - source_id  来源的唯一标识，切块后每块会带上"来源+块序号"，
                     保证同一文档的不同块可被精确区分引用。
        """
        chunks = _chunk_text(text, chunk_size, overlap)
        for i, chunk in enumerate(chunks):
            self._chunks.append(chunk)
            self._vectors.append(self.embedder(chunk))
            # 来源标注：优先用"来源名+块序号"，否则空白块来源
            if source:
                self._sources.append(f"{source}（第{i + 1}节）" if len(chunks) > 1 else source)
            else:
                self._sources.append("")

    def search(self, query: str, top_k: int = 3) -> list[tuple[str, float]]:
        """给定问题，返回最相关的 top_k 个文本块（带相似度分数）。

        兼容旧接口：仍返回 (chunk, score) 元组；需要来源引用请用 search_hits()。
        """
        return [(h.text, h.score) for h in self.search_hits(query, top_k)]

    def search_hits(self, query: str, top_k: int = 3) -> list[SourceHit]:
        """给定问题，返回最相关的 top_k 个命中，**带来源引用**（企业级可溯源）。"""
        if not self._chunks:
            return []
        qvec = self.embedder(query)
        scored = [
            (cidx, cosine_similarity(qvec, v))
            for cidx, v in enumerate(self._vectors)
        ]
        scored.sort(key=lambda t: t[1], reverse=True)
        hits: list[SourceHit] = []
        for cidx, sim in scored[:top_k]:
            src = self._sources[cidx] if cidx < len(self._sources) else ""
            source_id = src if src else ""
            hits.append(SourceHit(
                text=self._chunks[cidx],
                score=sim,
                source=src,
                source_id=source_id,
            ))
        return hits

    def __len__(self) -> int:
        return len(self._chunks)


def _chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    """把长文本按字符切成有重叠的小块。"""
    if len(text) <= chunk_size:
        return [text] if text else []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        chunks.append(text[start:start + chunk_size])
        start += chunk_size - overlap
    return chunks


# ---- 把检索暴露成 Agent 能用的技能卡 ----
_KNOWLEDGE_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string", "description": "要查的问题"}},
    "required": ["query"],
}


def make_knowledge_tool(store: VectorStore, *, cite: bool = True) -> ToolSpec:
    """把一个向量库包装成 knowledge.search 技能卡（Agent 需要时会自动查）。

    `cite=True`（默认）时，检索结果会带**来源引用**（[来源: xxx]），模型可以根据
    引用在回答里指出"据《某文档》"——这是企业级可信、可溯源的关键。
    `cite=False` 时退化为只返回文本块（兼容旧行为）。
    """

    @function_tool(
        "knowledge.search",
        (
            "在知识库里检索与问题相关的内容，返回带来源引用的资料。"
            "当用户问的知识你记不准确，或需要参考资料时，调用它获取；"
            "回答时请引用来源（如『据《xxx》』），保证答案可溯源。"
        ),
        _KNOWLEDGE_SCHEMA,
    )
    def search(query: str) -> str:
        if not cite:
            results = store.search(query, top_k=3)
            if not results:
                return "知识库为空或没有相关结果。"
            return "\n\n".join(
                f"[相关度 {score:.2f}] {chunk}" for chunk, score in results
            )
        hits = store.search_hits(query, top_k=3)
        if not hits:
            return "知识库为空或没有相关结果。"
        parts = []
        for i, hit in enumerate(hits, start=1):
            src = f"来源：{hit.source}" if hit.source else "来源：未标注"
            parts.append(f"[引用{i}|{src}｜相关度 {hit.score:.2f}]\n{hit.text}")
        return "\n\n".join(parts)

    return search

