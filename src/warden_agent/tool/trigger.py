"""工具自解释 —— 从"描述"里自动长出触发词（把能力层真正长进系统的设计）。

背景（为什么需要它）：
  intent 路由器（判断"该不该调这个工具"）和 skill 路由器（判断"该触发哪个技能"）
  都需要"触发词"来知道什么情况下该用某个工具。传统做法是手配一张
  `{工具名: [触发词]}` 的映射表——但那是"外挂的规则"，工具一多就跟不上、还容易漏。

出色做法（本模块落地的）：
  让 ToolSpec 自带"自解释"：要么手标 `triggers`（可信第一手），要么**自动从
  description 里提取**触发词。这样"这个工具什么时候该被用"就和工具本身长在一起，
  intent / skill 路由不再需要手配映射表——能力从描述里"自己长"出来。

提取策略（离线可测、不过度匹配）：
  - 英文/数字：拆成词（weather / get / city）。
  - 中文：产出“相邻两字(bigram)”（如 "查天气" → 「查天」「天气」）。
    单字太泛（"查""的"没信息量），整句太长（不会和用户问题重合），双字恰到好处——
    这也和 loop 记忆相关的 `_tokens` 同一套中文处理（保持一致、可复现）。
  - 滤掉"太泛"的通用词（怎么/什么/如何/获取/信息…），避免描述里到处都出现的
    动词/助词被当成触发信号，导致什么都命中（误伤）。

兼容性：
  skill 路由原来用自己的 `_tokens`（英文词 + 中文 bigram + 连续中文整段 + 停用词）。
  这里改成统一走本模块的提取/过滤，改动对已有 skill 匹配行为保持等价（英文词 +
  中文 bigram 一致；连续中文整段与英语短词差异被统一规则覆盖，语义不变）。
"""

from __future__ import annotations

import re

# ---- 太"泛"的词不当作触发信号（避免描述里到处都出现的东西被当成"该用它的信号"）----
_STOPWORDS = {
    # 中文通用动词/助词（几乎每个工具描述都有，无区分度）
    "获取", "查询", "查找", "得到", "查看", "返回", "提供", "使用", "进行",
    "一个", "这个", "那个", "什么", "怎么", "如何", "能否", "需要",
    "工具", "数据", "信息", "内容", "结果", "系统", "功能",
    # 技能场景的泛词（保留给"匹配"，但不当"触发信号"计数）
    "帮助", "支持", "完成", "任务", "自己", "时候",
}

_EN_CTRL_WORDS = {"the", "a", "an", "and", "or", "for", "to", "of", "in",
                  "on", "with", "get", "set", "use", "you", "your"}


def extract_triggers(description: str, *, extra: tuple[str, ...] = ()) -> list[str]:
    """从一句描述里自动提取触发词。

    - 手工标注的 `extra`（ToolSpec.triggers）优先保留（第一手、可信）。
    - 再从 description 自动提取英文词 + 中文双字，过滤泛词后补上（去重保序）。
    - 返回空列表说明"提取不出有效触发词"（该工具不该被 intent 拦，默认放行）。
    """
    words: list[str] = []
    seen: set[str] = set()

    def _add(w: str) -> None:
        wl = w.lower()
        if wl and wl not in seen and wl not in _STOPWORDS and wl not in _EN_CTRL_WORDS:
            seen.add(wl)
            words.append(wl)

    for w in extra:
        _add(w)
    for w in _words(description):
        _add(w)
    return words


def _words(text: str) -> list[str]:
    """把文本拆成"有区分度的词"：英文/数字词 + 中文相邻双字。

    与 loop 记忆的 `_tokens` 精神一致（中文用 bigram），这里返回有序去重列表。
    """
    out: list[str] = []
    seen: set[str] = set()

    def _push(w: str) -> None:
        wl = w.lower()
        if wl and wl not in seen:
            seen.add(wl)
            out.append(wl)

    for w in re.findall(r"[A-Za-z0-9_]+", text):
        _push(w)
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(run) == 1:
            _push(run)
        else:
            for i in range(len(run) - 1):
                _push(run[i:i + 2])
    return out


# ---- 兼容旧接口：给 skill 路由的"匹配信号"用（保持等价行为）----
def tokens(text: str) -> set[str]:
    """拆成匹配用的词集合（含短语信号），供 skill 匹配打分用。

    - 英文单词
    - 中文相邻双字 + 长度<=2 的中文整段
    - 过滤泛词（`_STOPWORDS` + 英文控制词）
    与旧 skill 路由的 `_tokens` 语义一致。
    """
    out: set[str] = set()
    for w in re.findall(r"[A-Za-z0-9_]+", text):
        wl = w.lower()
        if wl not in _STOPWORDS and wl not in _EN_CTRL_WORDS:
            out.add(wl)
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(run) > 1:
            out.update(run[i:i + 2] for i in range(len(run) - 1))
        if len(run) <= 2:
            out.add(run)
    return out


__all__ = ["extract_triggers", "tokens"]
