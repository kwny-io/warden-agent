"""RAG 知识检索测试。"""
from warden_agent.rag.knowledge import (
    VectorStore,
    cosine_similarity,
    hash_embedder,
    make_knowledge_tool,
)
from warden_agent.tool.catalog import ToolCatalog


def test_向量库存取与检索() -> None:
    store = VectorStore()
    store.add("公司的报销流程是：先填表，再找经理审批。")
    store.add("公司的年假政策是每年 15 天。")
    assert len(store) == 2  # 两块都入库存了

    # 检索提到"年假"的问题，应该优先返回年假那条
    results = store.search("我想休年假", top_k=2)
    best = results[0][0]
    assert "年假" in best


def test_相似度_相同文本最像() -> None:
    vec = hash_embedder("报销流程 经理审批 填表")
    same = cosine_similarity(vec, hash_embedder("报销流程 经理审批 填表"))
    diff = cosine_similarity(vec, hash_embedder("天气 篮球 音乐 火山"))
    assert same > diff


def test_空库检索返回空() -> None:
    store = VectorStore()
    assert store.search("任何问题") == []


def test_长文本会自动切块() -> None:
    store = VectorStore()
    long = "内容。" * 500  # 很长的一整段
    store.add(long, chunk_size=200, overlap=30)
    assert len(store) > 1  # 被切成了多块


def test_knowledge工具_通过目录可执行() -> None:
    store = VectorStore()
    store.add("上海是中国的经济中心之一，人口两千多万。")
    tool = make_knowledge_tool(store)
    catalog = ToolCatalog()
    catalog.register(tool)

    # 模型想查"上海"时触发 knowledge.search
    result = catalog.execute("knowledge.search", {"query": "上海在哪里？"})
    assert "上海" in str(result)


def test_search_hits_带来源引用() -> None:
    store = VectorStore()
    store.add("公司的报销流程：先填报销单，再交直属经理审批，最后由财务打款。",
              source="员工手册.pdf")
    hits = store.search_hits("报销流程是怎样的", top_k=1)
    assert len(hits) == 1
    assert "报销" in hits[0].text
    assert "员工手册" in hits[0].source  # 来源被记录


def test_search_hits_无来源则source为空() -> None:
    store = VectorStore()
    store.add("公司的年假是每年 15 天。")
    hits = store.search_hits("年假", top_k=1)
    assert hits[0].source == ""


def test_knowledge工具_默认带引用() -> None:
    store = VectorStore()
    store.add("公司健身房位于 3 楼。", source="行政通知.md")
    tool = make_knowledge_tool(store)  # cite=True 默认
    catalog = ToolCatalog()
    catalog.register(tool)
    result = catalog.execute("knowledge.search", {"query": "健身房在哪"})
    assert "[引用" in str(result)
    assert "行政通知" in str(result)


def test_knowledge工具_cite关闭退化() -> None:
    store = VectorStore()
    store.add("公司健身房位于 3 楼。", source="行政通知.md")
    tool = make_knowledge_tool(store, cite=False)
    catalog = ToolCatalog()
    catalog.register(tool)
    result = catalog.execute("knowledge.search", {"query": "健身房在哪"})
    assert "[引用" not in str(result)  # 不输出引用标记
    assert "3 楼" in str(result)

