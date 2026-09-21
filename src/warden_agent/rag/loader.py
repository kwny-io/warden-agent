"""把知识索引进向量库 —— RAG 接进产品路径的入口。

**为什么需要这个文件**：在此之前 `rag/` 只被 `demo_e2e.py` 与 `rag/eval.py` 引用，
`build_agent` / `build_app` / `run_server` **没有任何地方构造 `VectorStore`**。
结果是：RAG 的代码、评测、来源引用全都在，但模型手里的工具清单里根本没有
`knowledge.search` —— 典型的"实现了没接线"。

这里补上那段缺失的装配：给定"要索引什么"，造出向量库并交给上层注册成工具。

支持三种来源（对应 `build_knowledge` 的 `source` 参数）：
  - `VectorStore` 实例 → 原样使用（调用方自带库；测试与自定义嵌入走这条）
  - `True`            → 索引内置离线语料（零配置即可让控制台"有知识"）
  - 目录路径           → 索引该目录下的 `.md` / `.markdown` / `.txt`
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from warden_agent.rag.knowledge import Embedder, VectorStore, embedder_from_env

_TEXT_SUFFIXES = (".md", ".markdown", ".txt")


def index_directory(
    root: str | Path, *, embedder: Embedder | None = None
) -> tuple[VectorStore, list[str]]:
    """把目录下的文本文件索引进向量库，返回 `(store, 已索引文件名)`。

    每个文件作为独立来源（`source=相对路径`），这样检索结果能精确引用到文件——
    来源引用是 RAG 可溯源的前提，丢了来源这一层就退化成了"不知道从哪抄的"。
    空文件跳过；读取失败按替换字符处理（不让一个坏文件毁掉整次索引）。
    """
    base = Path(root)
    if not base.is_dir():
        raise NotADirectoryError(f"知识目录不存在或不是目录: {root}")
    store = VectorStore(embedder=embedder)
    indexed: list[str] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            continue
        rel = path.relative_to(base).as_posix()
        store.add(text, source=rel)
        indexed.append(rel)
    return store, indexed


def index_corpus(
    pairs: tuple[tuple[str, str], ...] | list[tuple[str, str]],
    *,
    embedder: Embedder | None = None,
) -> tuple[VectorStore, list[str]]:
    """把 `(来源名, 文本)` 列表索引进库（内置离线语料走这条）。"""
    store = VectorStore(embedder=embedder)
    names: list[str] = []
    for source, text in pairs:
        store.add(text, source=source)
        names.append(source)
    return store, names


def build_knowledge(
    source: VectorStore | str | Path | bool,
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[VectorStore, str, list[str]]:
    """按 `source` 造向量库，返回 `(store, 嵌入器名, 已索引来源列表)`。

    **为什么把"嵌入器名"一起返回**：离线词频嵌入与真语义嵌入的检索质量差别很大，
    日志/报告里必须写清楚当前用的是哪一个——在词频嵌入下宣称"语义检索"是过度声称。
    调用方（`run_server` / `augment_catalog`）据此把它记进 `extra`，启动日志会打出来。
    """
    src_env: Mapping[str, str] = env if env is not None else os.environ
    embedder, embedder_name = embedder_from_env(src_env)

    if isinstance(source, VectorStore):
        # 调用方自带库：嵌入器以库自身的为准，这里只报告我们无从得知其实现
        return source, "provided", [f"<注入的向量库，共 {len(source)} 块>"]

    if source is True:
        from warden_agent.rag.corpus import POLICY_CORPUS

        store, names = index_corpus(POLICY_CORPUS, embedder=embedder)
        return store, embedder_name, names

    if source is False:
        # False 是"不启用"，调用方应该用 None 表达；走到这里说明参数用错了，明确报错
        raise ValueError(
            "build_knowledge(source=False) 无意义：不启用请传 None，"
            "或传 True（内置语料）/ 目录路径 / VectorStore 实例"
        )

    store, names = index_directory(source, embedder=embedder)
    return store, embedder_name, names
