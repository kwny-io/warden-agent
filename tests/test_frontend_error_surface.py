"""结构守卫：前端**每个** `usePolling` 的取用处都必须接住 `error`。

为什么判据放在这一层（第五十五批）：`usePolling` 失败时只做两件事——把 `error`
置上、保留旧 `data`。接不接是调用方的事，而 TypeScript 管不了：

    const { data, refresh } = usePolling<T>(…)   // 编译期完全合法

后果不是崩溃，而是**界面上出现一句在那一刻为假的话**：

- `ConversationList` 把 401/500/断网显示成「还没有对话」（用户以为历史没了）；
- `ModelPanel` 把 `/models` 挂了显示成「加载中…」（永远转圈，不报错）；
- `ApprovalPanel` 在拉取失败时同时显示红字**和**「（暂无待审批）」。

三个组件各自都"看着正常"，全套测试也没有一条红——因为它们的用例只覆盖 happy path。
所以这里把判据钉在**机制**上：扫一遍组件源码，凡是用 `usePolling<` 取数的，
解构里必须有 `error`（允许改名 `error: xxx`）；并逐个点名那几句「假话文案」，
要求它们确实被 error 压住。新增面板忘了接 → 这里立刻红。
"""

from __future__ import annotations

import re
from pathlib import Path

_WEB_SRC = Path(__file__).resolve().parents[1] / "web" / "src"
_COMPONENTS = _WEB_SRC / "components"

# `const { … } = usePolling<` —— 解构体允许跨行（[^}] 能吞换行）
_DESTRUCT = re.compile(r"const\s*\{([^}]*)\}\s*=\s*usePolling\s*<")
_HAS_ERROR = re.compile(r"\berror\b")


def _component_sources() -> list[tuple[str, str]]:
    """组件源码（排除 .test.tsx）。返回 (文件名, 源码)。"""
    return [
        (p.name, p.read_text(encoding="utf-8"))
        for p in sorted(_COMPONENTS.glob("*.tsx"))
        if not p.name.endswith(".test.tsx")
    ]


def test_每个usePolling取用处都接住了error() -> None:
    missing: list[str] = []
    total = 0
    for name, src in _component_sources():
        for m in _DESTRUCT.finditer(src):
            total += 1
            if not _HAS_ERROR.search(m.group(1)):
                missing.append(f"{name}: const {{{m.group(1).strip()}}} = usePolling<…>(…)")

    assert missing == [], (
        "这些取用点丢掉了 usePolling 的 error（拉取失败时会显示成空态/永久加载）：\n  "
        + "\n  ".join(missing)
    )
    # 守卫本身不能空跑：实测 4 个文件 8 处（ApprovalPanel 2 / ConversationList 1 /
    # InfoPanel 4 / ModelPanel 1）。扫到 0 处说明规则失效（写法变了），不是"没问题了"。
    assert total >= 5, f"只扫到 {total} 处 usePolling，扫描规则可能已失效"


def test_扫描规则本身有效_丢掉error的写法必须被认出来() -> None:
    """反向控制：故意造一个丢 error 的解构，规则必须认得出（否则上面那条测试是空的）。"""
    bad = "const { data, refresh } = usePolling<X>((s) => api.x({ signal: s }), 5000);"
    good = "const { data, error, refresh } = usePolling<X>((s) => api.x({ signal: s }), 3000);"
    aliased = "const { data, error: loadError } = usePolling<X>((s) => api.x({ signal: s }), 3000);"

    m_bad = _DESTRUCT.search(bad)
    assert m_bad is not None and not _HAS_ERROR.search(m_bad.group(1))
    for src in (good, aliased):
        m = _DESTRUCT.search(src)
        assert m is not None and _HAS_ERROR.search(m.group(1)), src


def test_三句假话都必须被error压住() -> None:
    """逐个点名：文案还在，但它必须挂在「没有 error 才显示」的条件上。

    这几条是**回归锁**：谁把条件改回去（或把文案挪到无条件分支），这里就红。
    """
    cases = [
        (
            "ConversationList.tsx",
            "还没有对话",
            r"error \? null : \(",  # 拉取失败 → 不再说「还没有对话」
        ),
        (
            "ConversationList.tsx",
            "列表不是数组",  # 拉到了东西但形状不对（后端契约变了）
            r"shapeBad \?",
        ),
        (
            "ApprovalPanel.tsx",
            "（暂无待审批）",
            r"error \? null : <p",
        ),
        (
            "ModelPanel.tsx",
            "加载中…",
            r"!loadError &&",  # 拉取失败 → 不再说「加载中…」
        ),
    ]
    problems: list[str] = []
    for filename, text, guard in cases:
        src = (_COMPONENTS / filename).read_text(encoding="utf-8")
        if text not in src:
            problems.append(f"{filename}: 找不到文案「{text}」（本测试已过期，需同步更新判据）")
        elif re.search(guard, src) is None:
            problems.append(f"{filename}: 文案「{text}」没有被 error 条件压住（缺 /{guard}/）")
    assert problems == [], "\n".join(problems)
