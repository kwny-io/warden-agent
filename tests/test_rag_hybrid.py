"""混合检索（BM25 字面 + 向量 RRF）的机制测试。

为什么要有这个文件：`python -m warden_agent.rag.eval` 在离线词频嵌入下只能证明
"hybrid ≈ lexical"——两条腿都是字面匹配，融合有没有真的发生看不出来。这里用一个
**确定性假语义嵌入器**替代真嵌入：它把"同义说法"映射到同一个方向，于是"字面查不到、
语义查得到"的块必然出现，RRF 的行为才可被钉住。

这些测试证明的是**机制**，不是**质量**：真嵌入下的 top-1 提升必须配 WARDEN_EMBED_* 实测，
不能拿这里的绿当"检索变强了"对外说。
"""
import pytest

from warden_agent.rag.hybrid import BM25_B, BM25_K1, LexicalIndex, rrf_fuse
from warden_agent.rag.knowledge import VectorStore

# 假语义嵌入器：按"说法类别"给方向，不认识的文本退成同一个小分量。
# 三个维度就够——测试要的是"同一类说法相似、不同类不相似"，不是嵌入质量。
_SYNONYM_AXES: dict[str, int] = {
    "假": 0, "休": 0, "请假": 0, "出去玩": 0, "年假": 0,
    "报销": 1, "报账": 1, "核销": 1, "发票": 1,
}


def fake_semantic_embedder(text: str) -> list[float]:
    vec = [0.0, 0.0, 0.01]
    for word, axis in _SYNONYM_AXES.items():
        if word in text:
            vec[axis] += 1.0
    return vec


CORPUS = (
    "年假政策：每年 15 天，提前一周申请。",
    "报销流程：先填表，再找财务审批。",
    "公司楼下有一台咖啡机。",
)


def _store(mode: str) -> VectorStore:
    store = VectorStore(embedder=fake_semantic_embedder, mode=mode)
    for chunk in CORPUS:
        store.add(chunk)
    return store


def test_默认模式是hybrid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WARDEN_RAG_MODE", raising=False)
    assert VectorStore(embedder=fake_semantic_embedder).mode == "hybrid"


def test_模式取值非法时构造即失败() -> None:
    # 静默退回默认会让"我设了 lexical 却在跑 hybrid"这种事故无人发现
    with pytest.raises(ValueError, match="WARDEN_RAG_MODE"):
        VectorStore(embedder=fake_semantic_embedder, mode="keyword")


def test_BM25命中标识符() -> None:
    index = LexicalIndex()
    index.add("WARDEN_PLANNER 开关控制阶段计划注入，默认关。")
    index.add("公司楼下有一台咖啡机。")
    hits = index.top_k("planner 怎么打开", 3)
    assert hits and hits[0][0] == 0


def test_BM25参数是常规取值() -> None:
    # 1.5/0.75 是 BM25 的常识默认；改了就是新实验，得在 CHANGELOG 里说明
    assert (BM25_K1, BM25_B) == (1.5, 0.75)


def test_字面这一路查不到换说法() -> None:
    store = _store("lexical")
    # "我想请几天假出去玩" 与 "年假政策：每年 15 天，提前一周申请" 没有共同词元
    assert store.search_hits("我想请几天假出去玩", top_k=1) == []


def test_语义这一路能命中换说法() -> None:
    store = _store("semantic")
    hits = store.search_hits("我想请几天假出去玩", top_k=1)
    assert hits and "年假" in hits[0].text


def test_RRF把语义命中顶到第一且不丢字面命中() -> None:
    store = _store("hybrid")
    assert "年假" in store.search_hits("我想请几天假出去玩", top_k=1)[0].text
    # 另一路的事实不能被融合吃掉：报销类问法仍排第一
    assert "报销" in store.search_hits("报账要走什么流程", top_k=1)[0].text


def test_三模式在同一库上可切换() -> None:
    store = _store("lexical")
    assert store.search_hits("我想请几天假出去玩", top_k=1) == []
    store.mode = "semantic"
    assert store.search_hits("我想请几天假出去玩", top_k=1)
    store.mode = "hybrid"
    assert store.search_hits("我想请几天假出去玩", top_k=1)


def test_空库任何模式都返回空() -> None:
    for mode in ("lexical", "semantic", "hybrid"):
        assert VectorStore(embedder=fake_semantic_embedder, mode=mode).search_hits("年假") == []


def test_rrf融合分只表示名次() -> None:
    # 两路都排第一的块，分数必须高于"只有一路排第一"的块
    fused = dict(rrf_fuse([[(0, 9.0), (1, 8.0)], [(0, 5.0), (2, 4.0)]], top_k=3))
    assert fused[0] > fused[1] and fused[1] == fused[2]
    # 分数量级只与名次和 k 有关：1/(60+1) 的两倍
    assert abs(fused[0] - 2 / 61) < 1e-9


def test_reset后字面索引一起清空() -> None:
    store = _store("lexical")
    assert store.search_hits("年假", top_k=1)
    store.reset()
    assert len(store) == 0
    assert store.search_hits("年假", top_k=1) == []
