r"""CHANGELOG 结构守卫 —— 保证「每一批的条目都还是一个独立的 `## ` 段落」。

为什么要这条测试（真实踩到、而且是**我自己**踩的）：
    编辑 CHANGELOG 时，`edit` 的 oldText 很容易**跨过下一个 `## ` 标题**去匹配
    （例如为了在顶部插入新批次，把"前面的分隔线 + 下一批的标题"一起写进 oldText），
    而 newText 里忘了把那个标题补回去 —— 结果是**下一批的标题被静默吃掉**，
    两批正文并进同一个 `## ` 段落。本会话就这么丢过两次标题（第四十九、四十八批），
    而文件表面看起来完全正常：正文都在、只少了 `## ` 那一行。

    丢掉的是"这是第几批"这个索引信息 —— 而这本 CHANGELOG 的全部价值就在于
    「git 历史 ↔ 文档」能对上号（见文件头）。所以它必须能被机器查出来。

判据（三条，只看标题，不解析正文）：
  1. 同一个 `## ` 段落内不允许出现重复的 `### ` 标题 —— 两批正文合并后，
     各自都有「### 一、…」，重复立刻暴露（这是**症状**，最直接的一条）；
  2. 批号集合**不新增异常**：重复号与缺号必须与 `_KNOWN_ANOMALIES` 里钉住的完全一致
     （标题被吃掉 = 突然缺一个号；凭空多一个号/重号同样要报）；
  3. `_cn_to_int` 自己也被测 —— 判据的红绿全挂在那张换算表上。

关于批号：本文件的批号是**中文数字**（第五十批），不是阿拉伯数字。
第一版守卫写成 `第(\d+)批`，于是永远解析到 0 个标题、直接红在 `assert nums` 上；
第二版又假设"批号严格递减"，被真实历史推翻（见 `_KNOWN_ANOMALIES` 的理由）——
**两次都是"判据想当然"，所以这条守卫的判据本身也必须拿真实文件验过**。
"第 N 批**补**"是同批的补记（故意复用批号），不参与重号判定。
"""
from __future__ import annotations

import collections
import re
from pathlib import Path

import pytest

CHANGELOG = Path(__file__).resolve().parents[1] / "CHANGELOG.md"
_BATCH = re.compile(
    r"^## \d{4}-\d{2}-\d{2}（第([〇零一二三四五六七八九十百]+)批(补)?", re.MULTILINE
)

# 中文数字：只支持本文件用得到的形态（几十、一百零几），不做通用数词解析。
_CN_DIGITS = {
    "〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100}

#: 已知的历史异常（**逐条写明理由**，别再让新人以为是 bug；新增异常会让测试变红）。
#: 之所以不是"严格递减"：早期条目根本不用全局批号（见下），拿后来的规则去套旧账只会得到假红。
_KNOWN_ANOMALIES: dict[str, str] = {
    "dup:2": "2026-09-18/19 那两天按「日期 + 当日第 N 批」编号，与后来的全局批号不同源",
    "dup:3": "同上（同一个 2026-09-18/19 的编号体系）",
    "gap:9": "第 9 批没有单独条目，历史遗留；本守卫只保证「不再新增异常」",
}


def _cn_to_int(text: str) -> int:
    """中文数字 → int（如 四十九 → 49，五十 → 50，一百零五 → 105）。"""
    total = 0
    number = 0
    for ch in text:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            total += (number or 1) * _CN_UNITS[ch]  # “十”开头的简写（十九 = 19）
            number = 0
    return total + number


def _entries(text: str) -> list[tuple[int, bool]]:
    """所有批次标题 → [(批号, 是否补记)]。"""
    return [(_cn_to_int(m.group(1)), bool(m.group(2))) for m in _BATCH.finditer(text)]


def _anomalies(numbers: list[int]) -> set[str]:
    """重号 + 缺号（缺号范围取 [2, max]，最小号之前的空洞不算异常）。"""
    found: set[str] = set()
    for n, count in collections.Counter(numbers).items():
        if count > 1:
            found.add(f"dup:{n}")
    for n in range(2, max(numbers) + 1):
        if n not in numbers:
            found.add(f"gap:{n}")
    return found


def _sections(text: str) -> list[tuple[str, list[str]]]:
    """把文档切成 (一级标题, 该段里的二级标题列表)。"""
    out: list[tuple[str, list[str]]] = []
    cur: tuple[str, list[str]] | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            cur = (line, [])
            out.append(cur)
        elif line.startswith("### ") and cur is not None:
            cur[1].append(line)
    return out


def test_每个批次段落内的二级标题不重复() -> None:
    """重复的 `### 一、…` = 两批正文被并进了一个 `## ` 段落（标题被吃掉）。"""
    text = CHANGELOG.read_text(encoding="utf-8")
    bad = []
    for title, subs in _sections(text):
        dup = [s for s, n in collections.Counter(subs).items() if n > 1]
        if dup:
            bad.append(f"  {title}\n    重复：{dup}")
    assert not bad, (
        "CHANGELOG 里有 `## ` 段落包含重复的 `### ` 标题——通常意味着某一批的 `## ` 标题"
        "在编辑时被吃掉了，两批正文并到了一起：\n" + "\n".join(bad)
    )


def test_批号不新增重复与缺号() -> None:
    """批号异常集合必须与 `_KNOWN_ANOMALIES` 完全一致（抬高天花板：只准变少，不准变多）。"""
    numbers = [n for n, is_addendum in _entries(CHANGELOG.read_text(encoding="utf-8"))
               if not is_addendum]
    assert numbers, "一个批次标题都没解析到：标题格式变了？（判据见本文件开头）"
    actual = _anomalies(numbers)
    known = set(_KNOWN_ANOMALIES)
    new = sorted(actual - known)
    assert not new, (
        "CHANGELOG 出现了**新的**批号异常（重复或缺号）："
        + ", ".join(f"{k}（{_hint(k)}）" for k in new)
        + "\n最常见的原因是编辑时把某一批的 `## ` 标题吃掉了（两批正文并到一起，"
        "于是那个号突然缺失）。如果确实是历史遗留，请连理由一起加进 `_KNOWN_ANOMALIES`。"
    )
    stale = sorted(known - actual)
    assert not stale, (
        f"`_KNOWN_ANOMALIES` 里这些异常已经不存在了（{stale}）：把对应条目删掉，"
        "让这张表始终等于真实情况，否则它就成了下一个「没人核对的数字」。"
    )


def _hint(key: str) -> str:
    kind, _, value = key.partition(":")
    return "重复" if kind == "dup" else "缺号"


@pytest.mark.parametrize(
    ("text", "want"),
    [("十", 10), ("十九", 19), ("四十九", 49), ("五十", 50), ("一百零五", 105)],
)
def test_中文数字换算(text: str, want: int) -> None:
    """判据自己也要被测：上面那条守卫的红/绿全挂在这张换算表上。"""
    assert _cn_to_int(text) == want
