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
from collections.abc import Callable, Mapping
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


def _term_frequency_embedder(text: str, dim: int = 4096) -> list[float]:
    """词频哈希向量（固定维度）。**词面匹配，不是语义检索。**

    每个词都往它哈希到的"固定维度位置"上加 1（不取反），再归一化。
    - 固定维度 => 任意两段文本的向量长度都一样，余弦相似度直接可比；
    - 用 hashlib 做确定哈希（不是内置 hash()，因为后者每次进程启动会被随机化，
      会导致同一句向量每次都不同，检索不稳定）；
    - 不加负号  => "提到同一个词的文本"必然在同一维度都有分量，相似度会高。

    ⚠️ **dim 不是随便取的**：中文按 2~4 字窗口切，短文档会产生大量词元，
    维度太小会哈希碰撞——碰撞让不相关的词共享维度，相似度就失真了。
    这不是猜的，是拿 `rag/eval.py` 的标注集实测出来的：

        dim=256  → top-1 28.6%   recall@3  57.1%
        dim=512  → top-1 71.4%   recall@3  71.4%
        dim=2048 → top-1 85.7%   recall@3  85.7%
        dim=4096 → top-1 85.7%   recall@3 100.0%   ← 当前默认（MRR 0.905）

    （实测中"查'报销'却检索不到报销那段"就是碰撞导致的，**不是语义问题**——
     定位到这一层，才不会误以为"必须上大模型"。）

    **但它终究是词面匹配**：换个说法问（"请几天假出去玩" vs 文档里的"年假"）
    仍会掉分。要跨过这一步得换真语义嵌入，见 `embedder_from_env()`。
    """
    import hashlib

    vec = [0.0] * dim
    for t in _terms(text):
        # 这里的哈希只用来**分桶**（feature hashing），不承担完整性/抗碰撞职责，
        # 所以用 MD5 也不算漏洞。但仍选 SHA-256：成本可忽略，且不给静态扫描留下
        # "弱加密算法"的告警——安全扫描的误报也是需要人工解释的负担。
        digest = hashlib.sha256(t.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dim
        vec[index] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def hash_embedder(text: str, dim: int = 32, vocab: int = 300) -> list[float]:
    """（保留）紧凑哈希嵌入，兼容旧接口。新代码建议用词频向量。

    dim/vocab 两个参数是历史遗留、当前忽略（统一走 `_term_frequency_embedder`）。
    """
    return _term_frequency_embedder(text)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """向量相似度：输入**已 L2 归一化**时，点积就等于余弦相似度。

    ⚠️ 前提是"已归一化"——这里刻意不除范数：检索是热路径，给每一对都开根号太贵，
    归一化在嵌入阶段（`_term_frequency_embedder` / `openai_compatible_embedder`）就做掉了。
    把未归一化的向量丢进来，拿到的只是点积，**不是**余弦。
    """
    return sum(x * y for x, y in zip(a, b, strict=True))


def openai_compatible_embedder(
    *, base_url: str, api_key: str, model: str, timeout_s: float = 30.0,
) -> Embedder:
    """**真·语义嵌入**：调用 OpenAI 兼容的 `/embeddings` 端点。

    任何兼容端点都行（OpenAI / 智谱 / 百炼 / vLLM / Ollama 的兼容层 / 自建网关）。
    返回的向量做 L2 归一化，以便复用下面的点积当余弦。

    ⚠️ 它需要网络和 key，所以**不在默认路径上**：默认仍是离线词频嵌入，
    保证"不配 key 也能全链路跑通、测试全离线"。
    """
    import httpx

    url = base_url.rstrip("/") + "/embeddings"

    def _embed(text: str) -> list[float]:
        resp = httpx.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "input": text},
            timeout=timeout_s,
        )
        resp.raise_for_status()
        vec = [float(x) for x in resp.json()["data"][0]["embedding"]]
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    return _embed


def embedder_from_env(env: Mapping[str, str]) -> tuple[Embedder, str]:
    """按环境变量选嵌入函数，返回 `(embedder, 名字)`。

    - 配齐 `WARDEN_EMBED_BASE_URL` + `WARDEN_EMBED_API_KEY` + `WARDEN_EMBED_MODEL`
      → 真语义嵌入；
    - 否则 → 离线词频嵌入。

    **为什么要把"名字"返回出去**：因为这两者的检索质量差别很大，报告/日志里必须
    明确写出当前用的是哪一个。在哈希词频嵌入下宣称"语义检索"是过度声称——
    这正是"玩具级 RAG"最容易被面试官戳穿的点。
    """
    base = env.get("WARDEN_EMBED_BASE_URL")
    key = env.get("WARDEN_EMBED_API_KEY")
    model = env.get("WARDEN_EMBED_MODEL")
    if base and key and model:
        return (
            openai_compatible_embedder(base_url=base, api_key=key, model=model),
            f"semantic:{model}",
        )
    return _term_frequency_embedder, "offline-term-frequency"


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
        # 与 _chunks 一一对应的**唯一**来源标识（用于精确引用；同名来源也必须能区分）
        self._source_ids: list[str] = []

    def add(self, text: str, *, chunk_size: int = 400, overlap: int = 50,
            source: str | None = None, source_id: str | None = None) -> None:
        """把一段长文本切块后加入库中（带重叠避免切断语义）。

        - source     整段文本的来源名（如"员工手册.pdf"），切出的每块都继承它。
        - source_id  来源的唯一标识，切块后每块会带上"来源+块序号"，
                     保证同一文档的不同块可被精确区分引用。
        """
        chunks = _chunk_text(text, chunk_size, overlap)
        for i, chunk in enumerate(chunks):
            # 全局块序号：保证 source_id 在"同一文档多块"乃至"多次 add 用同名 source"时仍唯一
            gidx = len(self._chunks)
            self._chunks.append(chunk)
            self._vectors.append(self.embedder(chunk))
            # 来源标注：优先用"来源名+块序号"，否则空白块来源
            if source:
                label = f"{source}（第{i + 1}节）" if len(chunks) > 1 else source
                self._sources.append(label)
                # 调用方给了 source_id 就照用；否则自动生成**唯一** id。
                # 自动生成必须带全局序号：只用 source 名的话，同名的两块 source_id 会重复，
                # 引用就无法区分（demo 里"年假"和"报销"同属《员工手册.pdf》就会撞）。
                self._source_ids.append(source_id or f"{label}#{gidx + 1}")
            else:
                self._sources.append("")
                self._source_ids.append(source_id or "")

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
            sid = self._source_ids[cidx] if cidx < len(self._source_ids) else ""
            hits.append(SourceHit(
                text=self._chunks[cidx],
                score=sim,
                source=src,
                source_id=sid,
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

