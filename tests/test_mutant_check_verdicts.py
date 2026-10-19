"""`scripts/mutant_check.py` 的**结论分档**与「算不算问题」（第八十四批）。

为什么值得单独守这个「装置的装置」：变异核对的全部产出就是「牙齿有多硬」这一句话，
而它只看 pytest 的退出码。2026-10-15 真踩过——换错解释器（系统 `python` 的
`cryptography` 是残缺的 namespace 包）⇒ 基线 59 个 collection ERROR（rc=2）⇒ 旧逻辑
把这一片变异全记成 KILLED，报告看起来是 100% 杀死率，其实是**假证据**。
所以「基线不绿 / 跑不起来时不许算强证据」这条必须由测试钉死，而不是靠跑的人记得。

本文件 import `scripts/mutant_check.py`（同仓其它守卫测试多数是自包含重写判据，
这里不行：判据本身就是那个装置自己的行为）。import 只执行模块级常量与 `CASES` 表，
`main()` 里的改写/备份/信号处理一概不跑，没有副作用。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import mutant_check  # noqa: E402  （见模块文档：故意 import 被守卫者）

KILL = mutant_check.KILL
EQUIV = mutant_check.EQUIV


@pytest.mark.parametrize(
    ("mutation_failed", "green", "broken", "want", "why"),
    [
        (True, True, False, "KILLED", "基线绿 ⇒ 强证据"),
        (True, False, False, "KILLED?", "基线本来就红 ⇒ 弱证据（台账守卫就是这一例）"),
        (True, False, True, "BROKEN", "基线跑不起来（收集失败）⇒ 不算证据"),
        (True, True, True, "BROKEN", "判定顺序不能反：发现收集失败就必须按 BROKEN"),
        (False, True, False, "SURVIVED", "变异后仍全绿 ⇒ 存活"),
        (False, False, False, "SURVIVED", "基线红不改变「全绿=存活」"),
        (False, False, True, "SURVIVED", "基线坏也不改变「全绿=存活」"),
    ],
)
def test_分档(mutation_failed: bool, green: bool, broken: bool, want: str, why: str) -> None:
    """分档表：`KILLED` 只在基线绿时才算强证据（2026-10-15 换错解释器那次的产物）。"""
    assert mutant_check._verdict(mutation_failed, green, broken) == want, why


def test_基线跑不起来时那片变异的杀死不算证据() -> None:
    """BROKEN 与 KILLED 必须分开——否则坏环境会伪装成 100% 杀死率（假证据）。"""
    assert mutant_check._verdict(True, False, True) != mutant_check._verdict(True, True, False)


@pytest.mark.parametrize(
    ("row", "is_problem"),
    [
        (("KILLED", KILL, "a"), False),
        (("KILLED?", KILL, "a"), False),
        (("SURVIVED", EQUIV, "a"), False),
        (("SURVIVED", KILL, "a"), True),  # 真缺口
        (("SKIP", KILL, "a"), True),  # 原文没匹配上（源码变了？）
        (("BROKEN", KILL, "a"), True),  # 基线不可用
        (("BROKEN", EQUIV, "a"), True),  # 等价变异也一样：这一片证据无效
    ],
)
def test_哪些情况算问题(row: tuple[str, str, str], is_problem: bool) -> None:
    """BROKEN 必须算问题——否则「环境坏了」会以退出码 0 混过去，那正是假证据的入口。"""
    assert mutant_check._problem([row]) is is_problem


# ---------------------------------------------------------------------------
# 「重定向到文件」这条路必须活着（2026-10-19 真踩）
#
# 本装置的正常用法就是 `python scripts/mutant_check.py > evidence/matrix/<sha12>.log`,
# 而 Windows 下重定向时 sys.stdout 默认是 **gbk**：跑到第一个 `⇒` 就
# `UnicodeEncodeError: 'gbk' codec can't encode character '\u21d2'`，矩阵在
# #[34/75] 死掉，日志变成台账最需要的那份产物**恰恰生成不了**。
# 下面这条用子进程 + 强制 gbk 复现那个环境（进程内 reconfigure 骗不过去）。


def test_重定向到文件时不许死于字符编码(tmp_path: Path) -> None:
    """`PYTHONIOENCODING=gbk` + 重定向 ⇒ 必须 rc=0，且输出里不许有 UnicodeEncodeError。

    反向对照（真做过）：去掉 `_force_utf8_stdio()` 后这条立刻变红，
    报的正是 `'gbk' codec can't encode character '\u21d2'`。
    """
    import os
    import subprocess

    out = tmp_path / "dry.log"
    env = dict(os.environ, PYTHONIOENCODING="gbk")
    with out.open("w", encoding="utf-8") as fh:
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "mutant_check.py"), "--dry"],
            stdout=fh,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(REPO_ROOT),
        )
    text = out.read_text(encoding="utf-8", errors="replace")
    assert "UnicodeEncodeError" not in text, text[-800:]
    assert proc.returncode == 0, text[-800:]
    assert "锚点检查" in text
