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

**落盘（persist）**：`VectorStore` 支持把向量存进 SQLite（重启不重新嵌入——真语义嵌入
按量计费，重算既慢又花钱）。此前 loader 从不传 `persist_path`，"落盘"能力等于没接线，
这里把它接上：
  - 默认在**主库同目录**下放一个 `<主库名>-rag.db`（目录来源再追加路径哈希，见
    `default_persist_path`），所以重启就复用，不需要运维额外配置；
  - 复用的前提是**来源指纹一致**（内置语料内容 / 目录内文件的相对路径+大小+mtime）。
    来源变了就丢弃旧索引重建，避免"旧文档混进新语料"；
  - 想临时关闭落盘（测试/一次性索引）传 `persist=False`。
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from pathlib import Path

from warden_agent.core.settings import env_opt
from warden_agent.rag.knowledge import Embedder, VectorStore, embedder_from_env

_TEXT_SUFFIXES = (".md", ".markdown", ".txt")


def default_persist_path(
    source: VectorStore | str | Path | bool, env: Mapping[str, str] | None = None
) -> Path:
    """落盘默认位置：主库（`WARDEN_DB_PATH`）同目录下的 `<主库名>-rag*.db`。

    为什么放**主库同目录**：那是"本部署的可写数据目录"（容器里 rootfs 只读时
    `WARDEN_DB_PATH` 会指向挂载卷）。放这里，重启复用不需要任何额外配置。

    目录来源再追加**路径哈希**：不同知识目录各自一份库，避免把 A 目录的索引
    误当成 B 目录的（照抄主库名会让它们撞在同一个文件里）。
    """
    src_env: Mapping[str, str] = env if env is not None else os.environ
    db = env_opt("WARDEN_DB_PATH", src_env) or "warden-agent-local.db"
    base = Path(db)
    if isinstance(source, (str, Path)):
        key = hashlib.sha1(str(Path(source)).encode("utf-8")).hexdigest()[:12]
        return base.parent / f"{base.stem}-rag-{key}.db"
    return base.parent / f"{base.stem}-rag.db"


def _corpus_fingerprint(pairs: tuple[tuple[str, str], ...] | list[tuple[str, str]]) -> str:
    """内置语料的内容指纹：语料改了就得重建索引。"""
    h = hashlib.sha256()
    for source, text in pairs:
        h.update(source.encode("utf-8"))
        h.update(b"\x00")
        h.update(text.encode("utf-8"))
        h.update(b"\x01")
    return "corpus:" + h.hexdigest()


def _dir_fingerprint(base: Path, files: list[Path]) -> str:
    """目录指纹：相对路径 + 文件大小 + mtime。

    用 mtime 而不是全文哈希：算指纹不该把整目录再读一遍（真正的开销在嵌入，不在 stat）。
    文件被改写会更新 mtime → 指纹变化 → 重建。
    """
    h = hashlib.sha256()
    for path in files:
        st = path.stat()
        rel = path.relative_to(base).as_posix()
        h.update(f"{rel}|{st.st_size}|{st.st_mtime_ns}\n".encode())
    return "dir:" + h.hexdigest()


def _reuse_persisted(store: VectorStore, fingerprint: str) -> bool:
    """落盘库已有内容且来源指纹一致 → 直接复用（**不重新嵌入**）。

    指纹不一致时清空重建：否则新语料会与旧索引混在一起（检索出已经删掉的内容）。
    """
    if store.size == 0:
        return False
    if store.get_meta("fingerprint", "") == fingerprint:
        return True
    store.reset()
    return False


def index_directory(
    root: str | Path,
    *,
    embedder: Embedder | None = None,
    persist_path: str | Path | None = None,
) -> tuple[VectorStore, list[str]]:
    """把目录下的文本文件索引进向量库，返回 `(store, 已索引文件名)`。

    每个文件作为独立来源（`source=相对路径`），这样检索结果能精确引用到文件——
    来源引用是 RAG 可溯源的前提，丢了来源这一层就退化成了"不知道从哪抄的"。
    空文件跳过；读取失败按替换字符处理（不让一个坏文件毁掉整次索引）。
    """
    base = Path(root)
    if not base.is_dir():
        raise NotADirectoryError(f"知识目录不存在或不是目录: {root}")
    files = [
        path
        for path in sorted(base.rglob("*"))
        if path.is_file() and path.suffix.lower() in _TEXT_SUFFIXES
    ]
    store = VectorStore(embedder=embedder, persist_path=persist_path)
    fingerprint = _dir_fingerprint(base, files)
    if _reuse_persisted(store, fingerprint):
        return store, [f"<复用已落盘的索引，共 {len(store)} 块>"]
    indexed: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            continue
        rel = path.relative_to(base).as_posix()
        store.add(text, source=rel)
        indexed.append(rel)
    store.set_meta("fingerprint", fingerprint)
    return store, indexed


def index_corpus(
    pairs: tuple[tuple[str, str], ...] | list[tuple[str, str]],
    *,
    embedder: Embedder | None = None,
    persist_path: str | Path | None = None,
) -> tuple[VectorStore, list[str]]:
    """把 `(来源名, 文本)` 列表索引进库（内置离线语料走这条）。"""
    store = VectorStore(embedder=embedder, persist_path=persist_path)
    fingerprint = _corpus_fingerprint(pairs)
    if _reuse_persisted(store, fingerprint):
        return store, [f"<复用已落盘的索引，共 {len(store)} 块>"]
    names: list[str] = []
    for source, text in pairs:
        store.add(text, source=source)
        names.append(source)
    store.set_meta("fingerprint", fingerprint)
    return store, names


def build_knowledge(
    source: VectorStore | str | Path | bool,
    *,
    env: Mapping[str, str] | None = None,
    persist: bool = True,
    persist_path: str | Path | None = None,
) -> tuple[VectorStore, str, list[str]]:
    """按 `source` 造向量库，返回 `(store, 嵌入器名, 已索引来源列表)`。

    **为什么把"嵌入器名"一起返回**：离线词频嵌入与真语义嵌入的检索质量差别很大，
    日志/报告里必须写清楚当前用的是哪一个——在词频嵌入下宣称"语义检索"是过度声称。
    调用方（`run_server` / `augment_catalog`）据此把它记进 `extra`，启动日志会打出来。

    **落盘语义**（见模块 docstring）：
      - `persist_path` 显式给出 → 用它；
      - 否则 `persist=True`（默认）→ `default_persist_path(source, env)`；
      - `persist=False` → 纯进程内、不落盘（测试/一次性索引用）。
    """
    src_env: Mapping[str, str] = env if env is not None else os.environ
    embedder, embedder_name = embedder_from_env(src_env)
    target = persist_path if persist_path is not None else (
        default_persist_path(source, src_env) if persist else None
    )

    if isinstance(source, VectorStore):
        # 调用方自带库：嵌入器以库自身的为准，这里只报告我们无从得知其实现
        return source, "provided", [f"<注入的向量库，共 {len(source)} 块>"]

    if source is True:
        from warden_agent.rag.corpus import POLICY_CORPUS

        store, names = index_corpus(POLICY_CORPUS, embedder=embedder, persist_path=target)
        return store, embedder_name, names

    if source is False:
        # False 是"不启用"，调用方应该用 None 表达；走到这里说明参数用错了，明确报错
        raise ValueError(
            "build_knowledge(source=False) 无意义：不启用请传 None，"
            "或传 True（内置语料）/ 目录路径 / VectorStore 实例"
        )

    store, names = index_directory(source, embedder=embedder, persist_path=target)
    return store, embedder_name, names
