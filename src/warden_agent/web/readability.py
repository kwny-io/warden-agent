"""正文抽取（Readability 那一类的轻量实现，**零新依赖**）。

为什么需要它：`web.fetch` 之前只有"去标签的粗提取"（`html_to_text`）——一个新闻页抓回来，
导航栏、侧边栏、页脚、"相关推荐"全都混在正文里。塞给模型既费 token 又干扰判断
（模型会把菜单当成正文内容）。正文抽取要解决的就是"**只留主要内容**"。

做法（Readability 的核心思路，用标准库 `html.parser` 实现）：
  1. 建一棵轻量 DOM（只关心标签、属性里我们用到的那几个、文本）；
  2. **按块打分**：正文容器（`article`/`main`/`[role=main]`）加权；再看"文本密度"——
     段落文本越长越像正文；**链接密度高的块降权**（导航/相关推荐就是一堆链接）；
     命中的关键词（`comment`/`nav`/`sidebar`/`footer`/`share`… 在 id/class 里）**直接扣分**；
  3. 取最高分的块，按块级标签还原段落换行，剔除脚本/样式。
  4. **兜底**：抽出来的内容太短（< `MIN_CHARS`）就退回整页粗提取——
     "抽不出正文"时给全页文本，总比给一段碎片强（宁可多一点噪音，别丢内容）。

诚实边界：这是**启发式**，不是浏览器。它不执行 JS（SPA 拿不到）、不做复杂布局推断、
不如 Mozilla Readability 精准。但它是**可离线验证**的：用 fixture HTML 就能测
"导航/侧边栏有没有被排掉"。
"""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

# 出现这些词的 id/class 通常是"非正文"区域 → 该块降权
_NEGATIVE_HINTS = (
    "nav", "menu", "sidebar", "side-bar", "footer", "header", "comment", "share",
    "social", "related", "recommend", "promo", "advert", "sponsor", "banner",
    "breadcrumb", "pagination", "toolbar", "meta", "tag-list", "widget",
)
# 出现这些词的 id/class 通常是正文容器 → 该块加权
_POSITIVE_HINTS = (
    "article", "content", "post", "entry", "story", "main", "body", "text", "markdown",
)
# 整块丢掉（内容与正文无关）
_DROP_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "iframe", "form"})
# 这些标签本身就能当"正文候选块"
_CANDIDATE_TAGS = frozenset({"article", "main", "section", "div", "td", "body"})
# 块级标签：还原成换行
_BLOCK_TAGS = frozenset({
    "p", "div", "section", "article", "header", "footer", "li", "tr", "br",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "table",
})
# 抽出来的正文少于此长度就认为"没抽到"，退回整页粗提取
# （**只对启发式候选块生效**：明确标了 article/main 的块直接信任，见 extract_main_text）
MIN_CHARS = 200
# 渲染时要跳过的"非正文节点"：`<head>` 里的 title/meta 是**元信息**，不是页面正文。
# （`extract_article` 取标题时会单独找 meta，所以跳掉不影响标题抽取。）
_NON_CONTENT_TAGS = frozenset({"head", "title", "meta", "link", "base"})
# 明确标成"这里是内容"的标签：**信任它们**（作者已经声明过了）
_EXPLICIT_CONTENT = frozenset({"article", "main"})
_MAX_TITLE = 300


class _Node:
    """轻量 DOM 节点：只保留我们真正要用的东西（标签、属性、子节点、文本）。"""

    __slots__ = ("tag", "attrs", "children", "text", "parent")

    def __init__(self, tag: str = "", attrs: dict[str, str] | None = None) -> None:
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list[_Node] = []
        self.text = ""
        self.parent: _Node | None = None

    # ---- 便捷读取 ----
    @property
    def classes_and_id(self) -> str:
        return (self.attrs.get("id", "") + " " + self.attrs.get("class", "")).lower()

    @property
    def is_candidate(self) -> bool:
        return self.tag in _CANDIDATE_TAGS or self.attrs.get("role") == "main"

    def own_text(self) -> str:
        return self.text

    def all_text(self) -> str:
        parts = [self.text]
        for child in self.children:
            parts.append(child.all_text())
        return " ".join(p for p in parts if p)


class _DomBuilder(HTMLParser):
    """把 HTML 建成轻量 DOM。遇到 `_DROP_TAGS` 直接跳过整棵子树。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("body")
        self._stack: list[_Node] = [self.root]
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_depth:
            if tag in _DROP_TAGS:
                self._skip_depth += 1
            return
        if tag in _DROP_TAGS:
            self._skip_depth = 1
            return
        node = _Node(tag, {k: (v or "") for k, v in attrs})
        node.parent = self._stack[-1]
        self._stack[-1].children.append(node)
        if tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        if self._skip_depth:
            if tag in _DROP_TAGS:
                self._skip_depth -= 1
            return
        # 容错：不匹配就往上找一层（真实网页的标签经常没闭合）
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                return

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not data.strip():
            return
        self._stack[-1].text += data


_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})


def _link_density(node: _Node) -> float:
    """块内"链接文本"占比（0~1）。导航/相关推荐这类块会很高 → 用来降权。"""
    total = len(node.all_text().strip())
    if total == 0:
        return 1.0
    link_chars = sum(len(c.all_text().strip()) for c in _descendants(node, "a"))
    return min(1.0, link_chars / total)


def _descendants(node: _Node, tag: str) -> list[_Node]:
    found: list[_Node] = []
    for child in node.children:
        if child.tag == tag:
            found.append(child)
        found.extend(_descendants(child, tag))
    return found


def _score(node: _Node) -> float:
    """给一个候选块打分：文本越长越像正文；链接密度高/命中负面词则扣分。"""
    text = node.all_text().strip()
    length = len(text)
    if length == 0:
        return 0.0
    score = float(length)
    hints = node.classes_and_id
    if any(h in hints for h in _NEGATIVE_HINTS):
        score -= length          # 命中"导航/侧边栏/页脚"一类 → 整块基本清零
    if any(h in hints for h in _POSITIVE_HINTS):
        score += length * 0.5
    if node.tag in ("article", "main") or node.attrs.get("role") == "main":
        score += length * 1.0
    # 链接密度：超过 1/3 基本是链接列表，不是正文
    density = _link_density(node)
    if density > 0.33:
        score -= length * (density * 2)
    # 段落数：正文通常由多个 <p> 组成
    score += 25.0 * len(_descendants(node, "p"))
    return score


def _render(node: _Node) -> str:
    """把一个块渲染成文本：块级标签转换行，跳过 head/meta 这类元信息节点。"""
    out: list[str] = []

    def walk(n: _Node) -> None:
        if n.tag in _NON_CONTENT_TAGS:
            return
        if n.text:
            out.append(n.text)
        for child in n.children:
            walk(child)
            if child.tag in _BLOCK_TAGS:
                out.append("\n")

    walk(node)
    text = unescape("".join(out))
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def _title_of(root: _Node, html: str) -> str:
    """取标题：优先 `og:title`，其次 `<title>`，最后第一个 `<h1>`。"""
    metas = [n for n in _all_nodes(root) if n.tag == "meta"]
    for meta in metas:
        if meta.attrs.get("property", "").lower() == "og:title" and meta.attrs.get("content"):
            return meta.attrs["content"].strip()[:_MAX_TITLE]
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if match:
        return unescape(match.group(1)).strip()[:_MAX_TITLE]
    h1 = next((n.all_text().strip() for n in _all_nodes(root) if n.tag == "h1"), "")
    return h1[:_MAX_TITLE]


def _all_nodes(node: _Node) -> list[_Node]:
    out = [node]
    for child in node.children:
        out.extend(_all_nodes(child))
    return out


def extract_main_text(html: str) -> str:
    """抽出页面**主要内容**的文本（抽不到就退回整页粗提取，见模块说明的兜底策略）。"""
    from warden_agent.web.search import html_to_text  # 兜底复用（避免重复实现）

    root = _DomBuilder()
    try:
        root.feed(html)
        root.close()
    except Exception:  # noqa: BLE001 - 解析异常不该让抓取失败
        return html_to_text(html)

    candidates = [n for n in _all_nodes(root.root) if n.is_candidate]
    if not candidates:
        return html_to_text(html)

    best = max(candidates, key=_score)
    text = _render(best)
    # 兜底规则**只对启发式候选块生效**：如果块被明确标成 article/main，
    # 那是作者已经声明过的"这里是内容"——**信任它**，哪怕它很短
    # （否则一篇短笔记会被判成"没抽到"，退回整页、把导航也带进来）。
    explicit = best.tag in _EXPLICIT_CONTENT or best.attrs.get("role") == "main"
    if not explicit and len(text) < MIN_CHARS:
        # 抽出来的太短 → 宁可用整页（可能带点噪音），也别把内容丢成一段碎片
        return html_to_text(html)
    return text


def extract_article(html: str) -> tuple[str, str]:
    """返回 `(标题, 正文)`。标题取不到时为空串。"""
    root = _DomBuilder()
    try:
        root.feed(html)
        root.close()
        title = _title_of(root.root, html)
    except Exception:  # noqa: BLE001 - 同上
        title = ""
    return title, extract_main_text(html)
