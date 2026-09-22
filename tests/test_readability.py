"""正文抽取（Readability 轻量版）：只留主要内容，把导航/侧边栏/页脚排掉。

这里用**内联 fixture HTML**（不联网、不下载真实网页），因为要测的是"取舍逻辑"：
  - 有 `<article>` 时优先它；
  - 导航/侧边栏/页脚/相关推荐（链接密集 + 命中负面词）要被排掉；
  - 抽不出正文时**退回整页粗提取**（宁可多点噪音，别丢内容）；
  - 解析异常/空输入不能抛异常（抓取路径不能因为一个畸形页面就失败）。
"""

from __future__ import annotations

from warden_agent.web.readability import MIN_CHARS, extract_article, extract_main_text

# 一个"典型博客页"：导航 + 侧边栏 + 正文 + 相关推荐 + 页脚
PAGE = """
<html><head>
  <title>如何做正文抽取 - 示例博客</title>
  <meta property="og:title" content="如何做正文抽取（OG 标题）" />
  <style>body{color:red}</style>
</head><body>
  <header class="site-header"><nav class="main-nav">
    <a href="/">首页</a><a href="/about">关于</a><a href="/tags">标签</a>
  </nav></header>
  <div id="sidebar" class="sidebar widget">
    <a href="/hot/1">热文一</a><a href="/hot/2">热文二</a><a href="/hot/3">热文三</a>
  </div>
  <article class="post-content">
    <h1>如何做正文抽取</h1>
    <p>正文抽取的目标是只留下页面的主要内容，把导航、侧边栏、页脚这类噪音去掉。</p>
    <p>为什么重要：把整个页面塞给模型，既费 token，又会让模型把菜单当成正文内容的一部分，
       从而给出跑偏的答案。这是抓取工具必须解决的一步。</p>
    <p>常见做法是给候选块打分：文本越长越像正文，链接密度越高越像导航，
       命中 nav/sidebar/footer 这类标识的直接降权。</p>
  </article>
  <div class="related-posts">
    <a href="/r/1">相关推荐一</a><a href="/r/2">相关推荐二</a><a href="/r/3">相关推荐三</a>
  </div>
  <footer class="site-footer">版权所有 © 示例博客
    <a href="/privacy">隐私</a><a href="/terms">条款</a></footer>
</body></html>
"""


def test_正文被留下_导航侧边栏页脚被排掉() -> None:
    text = extract_main_text(PAGE)
    assert "正文抽取的目标是只留下页面的主要内容" in text
    assert "链接密度越高越像导航" in text
    # 噪音：导航 / 侧边栏 / 相关推荐 / 页脚 都不该出现
    assert "关于" not in text
    assert "标签" not in text
    assert "热文一" not in text
    assert "相关推荐一" not in text
    assert "版权所有" not in text
    assert "隐私" not in text
    # 样式与脚本内容也不该混进来
    assert "color:red" not in text


def test_标题优先取og_title() -> None:
    title, _text = extract_article(PAGE)
    assert title == "如何做正文抽取（OG 标题）"


def test_没有og时退回title标签() -> None:
    html = "<html><head><title>标题甲</title></head><body><p>" + "内容" * 200 + "</p></body></html>"
    assert extract_article(html)[0] == "标题甲"


def test_有main标签时优先main() -> None:
    html = f"""<html><body>
      <nav><a href="/a">导航链接一大堆重复文本</a></nav>
      <main><p>{'这是正文段落。' * 40}</p></main>
      <div class="footer">{'页脚文字。' * 40}</div>
    </body></html>"""
    text = extract_main_text(html)
    assert "这是正文段落。" in text
    assert "页脚文字。" not in text


def test_链接列表不会当选正文() -> None:
    """"全是链接"的块链接密度高 → 即使文本量大也不该压过正文。"""
    links = "".join(f'<a href="/p/{i}">这是第 {i} 条链接的标题文字</a>' for i in range(60))
    html = f"""<html><body>
      <div class="index-list">{links}</div>
      <article><p>{'真正的正文内容。' * 30}</p></article>
    </body></html>"""
    text = extract_main_text(html)
    assert "真正的正文内容。" in text
    assert "第 59 条链接" not in text


def test_抽不出正文时退回整页粗提取() -> None:
    """页面很短或结构怪异 → 宁可用整页文本（可能带噪音），也不要丢内容。"""
    html = "<html><body><div>很短的一段话。</div></body></html>"
    text = extract_main_text(html)
    assert "很短的一段话。" in text
    # 短页的产物必然短于 MIN_CHARS，说明走的是兜底路径
    assert len(text) < MIN_CHARS


def test_畸形HTML不抛异常() -> None:
    """真实网页的标签经常没闭合；解析异常不能让抓取失败。"""
    for bad in (
        "<html><body><div><p>没闭合<p>第二段</body>",
        "<html><body><article>未闭合的 article 里" + "有内容" * 200,
        "",
        "纯文本，没有任何标签",
        "<html><body>" + "<div>" * 200 + "深层嵌套" + "</div>" * 50,
    ):
        text = extract_main_text(bad)          # 不抛异常即通过
        assert isinstance(text, str)


def test_脚本与样式内容不混入正文() -> None:
    html = f"""<html><head><script>var secret = '不该出现';</script>
      <style>.x{{color:red}}</style></head>
      <body><article><p>{'正文句子。' * 40}</p></article></body></html>"""
    text = extract_main_text(html)
    assert "正文句子。" in text
    assert "不该出现" not in text
    assert "color:red" not in text


def test_抓取路径确实用了正文抽取() -> None:
    """回归：`HttpFetchProvider` 抓 HTML 时必须走正文抽取（而不是老的粗提取）。"""
    import inspect

    from warden_agent.web import search

    source = inspect.getsource(search.HttpFetchProvider._fetch_once)
    assert "extract_main_text" in source
