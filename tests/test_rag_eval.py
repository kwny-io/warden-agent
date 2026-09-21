"""检索质量评测与来源标识的测试。

三件事要钉死：
1. **指标算法本身对**（否则数字好看也没意义）；
2. **source_id 唯一**——同一份文档下的多个条款必须能区分，否则"引用"是假的；
3. **检索质量有下界**——这是给 `_term_frequency_embedder` 的 `dim` 加护栏：
   当初 dim=256 时 recall@3 只有 57%，把 dim 调到 4096 才到 100%。
   谁要是把 dim 改回去，这条会红。
"""

from __future__ import annotations

import pytest

from warden_agent.rag.corpus import POLICY_CORPUS
from warden_agent.rag.eval import RETRIEVAL_CASES, CaseOutcome, RetrievalReport, evaluate
from warden_agent.rag.knowledge import (
    VectorStore,
    cosine_similarity,
    embedder_from_env,
    hash_embedder,
    make_knowledge_tool,
    openai_compatible_embedder,
)
from warden_agent.tool.catalog import ToolCatalog


def _policy_store() -> VectorStore:
    store = VectorStore()
    for source, text in POLICY_CORPUS:
        store.add(text, source=source)
    return store


# ---------------- 指标算法 ----------------


def test_指标算法_全部命中且都排第一() -> None:
    report = RetrievalReport(
        embedder_name="t", k=3,
        outcomes=(CaseOutcome("q1", "n", 1), CaseOutcome("q2", "n", 1)),
    )
    assert report.top1 == 1.0
    assert report.recall_at_k == 1.0
    assert report.mrr == 1.0


def test_指标算法_一条没命中() -> None:
    report = RetrievalReport(
        embedder_name="t", k=3,
        outcomes=(CaseOutcome("q1", "n", 2), CaseOutcome("q2", "n", None)),
    )
    assert report.recall_at_k == 0.5          # 2 条里中 1 条
    assert report.top1 == 0.0                 # 没有一条排第一
    assert abs(report.mrr - 0.25) < 1e-9      # (1/2 + 0) / 2
    assert len(report.misses()) == 1


def test_空用例集不除以零() -> None:
    report = RetrievalReport(embedder_name="t", k=3, outcomes=())
    assert (report.top1, report.recall_at_k, report.mrr) == (0.0, 0.0, 0.0)


def test_k覆盖率反映小库抬高() -> None:
    """k/语料块数——越高说明 recall@k 越可能是"库小"而非"检索器强"。"""
    small = RetrievalReport(embedder_name="t", k=3, outcomes=(), corpus_size=7)
    assert abs(small.k_coverage - 3 / 7) < 1e-9
    big = RetrievalReport(embedder_name="t", k=3, outcomes=(), corpus_size=1000)
    assert big.k_coverage < 0.01


def test_报告逐条排名在前且标注口径() -> None:
    """逐条排名要打印出来——聚合数字会掩盖"垫底进榜"这种命中。"""
    from warden_agent.rag.eval import format_report

    report = RetrievalReport(
        embedder_name="offline", k=3, corpus_size=7,
        outcomes=(
            CaseOutcome("报销要走什么流程", "财务", 1),
            CaseOutcome("我想请几天假出去玩，有什么规定", "15 天", 3),
        ),
    )
    out = format_report(report)
    assert "逐条命中排名" in out
    assert "第 3" in out                       # 垫底命中也要能看见
    assert "recall@3 的口径" in out             # 小库抬高指标，必须提示
    assert "43%" in out                        # 3/7 的覆盖率


def test_大语料不打印小库口径提示() -> None:
    """库足够大时 k 覆盖率很低，不该刷无关提示。"""
    from warden_agent.rag.eval import format_report

    report = RetrievalReport(
        embedder_name="offline", k=3, corpus_size=1000,
        outcomes=(CaseOutcome("报销要走什么流程", "财务", 2),),
    )
    assert "recall@3 的口径" not in format_report(report)


def test_评测能跑通且指标在合理区间() -> None:
    report = evaluate(_policy_store(), embedder_name="offline")
    for value in (report.top1, report.recall_at_k, report.mrr):
        assert 0.0 <= value <= 1.0
    assert len(report.outcomes) == len(RETRIEVAL_CASES)


# ---------------- 质量下界（dim 的护栏） ----------------


def test_离线检索的recall有下界_防止dim被改小() -> None:
    """dim=256 时 recall@3 只有 57%；当前 dim=4096 实测 100%。

    留 0.8 的余量：低于它说明嵌入维度/分词被改回了碰撞严重的配置。
    """
    report = evaluate(_policy_store(), embedder_name="offline")
    assert report.recall_at_k >= 0.8, (
        f"recall@3 掉到 {report.recall_at_k:.0%}，检查 _term_frequency_embedder 的 dim"
    )


# ---------------- source_id 唯一性 ----------------


def test_同源不同条款的source_id不重复() -> None:
    """《员工手册.pdf》下有报销/年假/调休三条 —— 引用必须能区分它们。"""
    store = _policy_store()
    hits = store.search_hits("报销", top_k=len(POLICY_CORPUS))
    ids = [h.source_id for h in hits]
    assert len(ids) == len(set(ids)), f"source_id 有重复：{ids}"


def test_source_id非空且带来源名() -> None:
    store = _policy_store()
    hit = store.search_hits("报销要走什么流程", top_k=1)[0]
    assert hit.source_id
    assert "员工手册" in hit.source_id


def test_未标来源时source_id为空() -> None:
    store = VectorStore()
    store.add("没有来源的一段话。")
    assert store.search_hits("没有来源", top_k=1)[0].source_id == ""


def test_调用方可显式指定source_id() -> None:
    store = VectorStore()
    store.add("一段话。", source="某文档.md", source_id="DOC-42")
    assert store.search_hits("一段话", top_k=1)[0].source_id == "DOC-42"


def test_引用标记里带得出source_id对应的来源() -> None:
    store = _policy_store()
    catalog = ToolCatalog()
    catalog.register(make_knowledge_tool(store))
    out = str(catalog.execute("knowledge.search", {"query": "出差住宿标准"}))
    assert "[引用" in out and "差旅制度" in out


# ---------------- 嵌入器选择：不许把词频当语义 ----------------


def test_默认是离线词频嵌入() -> None:
    _embedder, name = embedder_from_env({})
    assert name == "offline-term-frequency"


def test_配齐环境变量才切语义嵌入(monkeypatch: pytest.MonkeyPatch) -> None:
    # 构造时就要求"公网地址"，所以这里注入一个公网解析结果（保持测试离线）
    monkeypatch.setattr(
        "warden_agent.rag.knowledge.socket.getaddrinfo",
        _fake_resolution("93.184.216.34"),
    )
    _embedder, name = embedder_from_env({
        "WARDEN_EMBED_BASE_URL": "https://embed.example.com/v1",
        "WARDEN_EMBED_API_KEY": "sk-x",
        "WARDEN_EMBED_MODEL": "text-embedding-3-small",
    })
    assert name.startswith("semantic:")


def test_嵌入端点解析不了时构造即失败_不拖到第一次检索(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """域名解析不了 → 构造就报错（fail fast），而不是检索时才炸。

    这里**注入"解析失败"**而不是依赖某个域名真的解析不了：有些运行环境（企业代理、
    沙箱）会做 DNS 通配劫持，把任何域名都解析到保留段——那样"解析失败"的前提就不成立，
    测试会随环境飘。要测的是"解析失败时怎么处理"，所以把失败条件直接造出来。
    """
    def _no_dns(_host: str, _port: object) -> list[object]:
        raise OSError("DNS 不可用（测试注入）")

    monkeypatch.setattr("warden_agent.rag.knowledge.socket.getaddrinfo", _no_dns)
    with pytest.raises(ValueError) as ei:
        openai_compatible_embedder(
            base_url="https://embed.example.com/v1", api_key="k", model="m",
        )
    assert "无法解析" in str(ei.value)


def test_只配一部分仍走离线_不半吊子切换() -> None:
    _embedder, name = embedder_from_env({"WARDEN_EMBED_BASE_URL": "https://example.invalid/v1"})
    assert name == "offline-term-frequency"


def test_离线嵌入是确定性的() -> None:
    """必须是确定性哈希：不能用内置 hash()，否则每进程启动结果都不同。"""
    store_a = _policy_store()
    store_b = _policy_store()
    q = "出差住酒店能报多少钱"
    assert [h.text for h in store_a.search_hits(q)] == [h.text for h in store_b.search_hits(q)]


def test_嵌入向量是归一化的_所以点积即余弦() -> None:
    """真实契约：嵌入函数输出已 L2 归一化，因此检索里直接用点积当余弦。"""
    v = hash_embedder("报销流程：先填报销单并附上发票")
    assert abs(cosine_similarity(v, v) - 1.0) < 1e-9


def test_未归一化输入只是点积_记录这个前提() -> None:
    """[0.3,0.4] 的模是 0.5，所以自比是 0.25 而不是 1 —— 记录前提，防误用。"""
    assert abs(cosine_similarity([0.3, 0.4], [0.3, 0.4]) - 0.25) < 1e-9


# ---------------- 嵌入端点的 URL 守卫（服务端出站请求的安全底线） ----------------
#
# 这些用例全部**离线确定性**：只解析字面量 IP（走 syscall，不需要 DNS），
# 或者用 monkeypatch 注入解析结果，不去真的查域名。


def _fake_resolution(ip: str):
    """伪造 getaddrinfo 返回值（AF_INET 的 sockaddr 形状）。"""
    return lambda _host, _port: [(2, 1, 6, "", (ip, 0))]  # type: ignore[return-value]


def test_嵌入端点只允许http和https() -> None:
    with pytest.raises(ValueError):
        openai_compatible_embedder(base_url="file:///etc/passwd", api_key="k", model="m")


def test_嵌入端点缺少主机名被拒() -> None:
    with pytest.raises(ValueError):
        openai_compatible_embedder(base_url="http:///v1", api_key="k", model="m")


@pytest.mark.parametrize("bad", [
    "http://127.0.0.1:8080/v1",      # 环回
    "http://[::1]:8080/v1",          # IPv6 环回
    "http://10.0.0.5/v1",            # 私有
    "http://192.168.1.10/v1",        # 私有
    "http://169.254.169.254/v1",     # 云元数据端点（链路本地）
    "http://0.0.0.0/v1",             # 未指定
])
def test_嵌入端点拒绝本机与内网地址(bad: str) -> None:
    with pytest.raises(ValueError):
        openai_compatible_embedder(base_url=bad, api_key="k", model="m")


def test_公网域名放行(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "warden_agent.rag.knowledge.socket.getaddrinfo",
        _fake_resolution("93.184.216.34"),
    )
    openai_compatible_embedder(
        base_url="https://embed.example.com/v1", api_key="k", model="m",
    )


def test_域名解析到内网时拒绝_防DNS重绑定(monkeypatch: pytest.MonkeyPatch) -> None:
    """域名看着完全正常，却解析到内网 —— 只校验域名的实现会在这里被绕过。"""
    monkeypatch.setattr(
        "warden_agent.rag.knowledge.socket.getaddrinfo",
        _fake_resolution("10.0.0.9"),
    )
    with pytest.raises(ValueError) as ei:
        openai_compatible_embedder(
            base_url="https://looks-public.example.com/v1", api_key="k", model="m",
        )
    assert "非公网地址" in str(ei.value)


def test_多解析结果里有一个内网就整体拒绝(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "warden_agent.rag.knowledge.socket.getaddrinfo",
        lambda _h, _p: [
            (2, 1, 6, "", ("93.184.216.34", 0)),
            (2, 1, 6, "", ("192.168.0.7", 0)),
        ],
    )
    with pytest.raises(ValueError):
        openai_compatible_embedder(
            base_url="https://mixed.example.com/v1", api_key="k", model="m",
        )
