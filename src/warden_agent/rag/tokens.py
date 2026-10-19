"""词元切分：中文按 2~4 字窗口、英文/数字按连续串。

单独成模块的原因：BM25（字面分）和词频嵌入（向量分）必须用**同一套切分**，
否则"同一个词在两路眼里不是同一个词"，融合出来的名次会自相矛盾。
"""

from __future__ import annotations

import re


def terms(text: str) -> list[str]:
    """把文本切成"模拟词"的词元列表。

    中文不按单字切（太碎：单字命中几乎没有信息量），而是滑 2/3/4 字窗口；
    英文和数字按连续串切（`WARDEN_PLANNER` → `warden` / `planner`，
    所以查工具名、错误码这类标识符时仍然命中）。
    """
    text = text.lower()
    out: list[str] = re.findall(r"[a-z0-9]+", text)
    cjk_start, cjk_end = chr(0x4E00), chr(0x9FFF)
    buf: list[str] = []
    segments: list[str] = []
    for ch in text:
        if cjk_start <= ch <= cjk_end:
            buf.append(ch)
        elif buf:
            segments.append("".join(buf))
            buf = []
    if buf:
        segments.append("".join(buf))
    for seg in segments:
        for n in (2, 3, 4):
            if len(seg) >= n:
                out.extend(seg[i:i + n] for i in range(len(seg) - n + 1))
    return out
