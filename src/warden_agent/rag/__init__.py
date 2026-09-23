"""RAG 能力：向量检索 + 来源引用，以及"接进产品路径"的装配入口。

  - `knowledge.VectorStore`     —— 极简向量库（chunk + embedding + 余弦检索）
  - `knowledge.make_knowledge_tool` —— 包成 `knowledge.search` 技能卡
  - `knowledge.embedder_from_env`   —— 嵌入器选择（离线词频 / 真语义端点）
  - `loader.build_knowledge`    —— 按"内置语料 / 目录路径 / 自带向量库"造库
  - `corpus.POLICY_CORPUS`      —— 离线演示语料
  - `eval`                      —— 检索质量评测（top-1 / recall@k / MRR）

产品路径接入方式：`build_agent(knowledge=...)` / `build_app(knowledge=...)` /
`run_server` 的 `WARDEN_KNOWLEDGE` 环境变量，最终都由 `agent.augment_catalog` 注册。
"""

from warden_agent.rag.knowledge import (
    Embedder,
    SourceHit,
    VectorStore,
    cosine_similarity,
    embedder_from_env,
    hash_embedder,
    make_knowledge_tool,
    openai_compatible_embedder,
)
from warden_agent.rag.loader import (
    build_knowledge,
    default_persist_path,
    index_corpus,
    index_directory,
)

__all__ = [
    "Embedder",
    "SourceHit",
    "VectorStore",
    "cosine_similarity",
    "embedder_from_env",
    "hash_embedder",
    "make_knowledge_tool",
    "openai_compatible_embedder",
    "build_knowledge",
    "default_persist_path",
    "index_corpus",
    "index_directory",
]
