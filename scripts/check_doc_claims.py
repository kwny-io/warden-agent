#!/usr/bin/env python3
"""交付文档里的两类「说了不该说的话」守卫。

判据一：正文里出现「反斜杠 + 反引号」这种写法。
    GFM 在行内代码 span 里**不认**反斜杠转义：作者以为反斜杠能把反引号“转义”掉，
    实际是反引号把 span 提前关掉了 —— 反斜杠被原样渲染成可见字符，后半句掉出代码块。
    要显示一个字面反引号，正解是**双反引号包裹**，不是加反斜杠（本仓文档里就有几处正解示例）。
    这类字符**排版期看不出、CI 也全绿**（没有任何东西会读 Markdown 的渲染），只能靠人眼在
    GitHub 上看出来——所以要有守卫。实测被它弄坏的：`CHANGELOG.md` 里那两行回顾旧记账的句子。

    判据的边界（否则会把“正确写法”也报红）：**双反引号 span 里的这种写法不算缺陷**，
    它正是要显示一个字面反引号时的正解。

判据二：``路径:行号`` 引用越界。
    文档点名「实现在 X 文件第 N 行」，而 N 超出该文件的行数（或文件压根不存在）。
    说明这段散文是在别的时间点写的：读者照着跳过去会扑空。实测抓到一处——
    ``docs/复核索引.md`` 引的 ``src/warden_agent/web/server.py:1433``，而那个文件只有 886 行。

两条判据的边界（**故意保守**，宁可少报也不放假阳）：

  - 只在**围栏之外**判。围栏里是「给别人抄的脚本原文」，属于 ``scripts/check_pinned_images.py``
    的判据范围，不是这里。
  - ``路径:行号`` 只在**整个行内 span 正好是「路径:行号」**（可带 ``12-34`` 区间）时判；
    命令行里夹一半的（``python scripts/x.py:12 --flag``）不算，因为那更像示例而不是坐标。
  - 引用必须落在**本仓目录**（见 ``_SCOPE_PREFIXES``）下才判；``mutmut/mutation/core.py:395``
    这类第三方源码引用天然被排除。
  - ``CHANGELOG.md`` 是历史日志：里面的行号是**当天**的，拿今天的文件去比它等于要求改假
    （同一条取向见 ``docs/复核索引.md`` 里那句「拿今天的台账去比它等于要求改假」）。

与 ``tests/test_doc_claims.py`` 的分工：本脚本是**被测对象**；测试侧用 fixture 造小仓库、
用另一条 argv（``git ls-files '*.md'``）当独立 oracle，不从本脚本 import 断言辅助函数——
否则辅助函数漏文件时两侧会一起变小，断言恒真（第七十四批踩过这个坑）。

退出码：**0** 干净；**1** 有缺陷；**2** 启动/环境问题（缺 git、读不到文件）。
「查不动」必须与「没问题」分开，否则守卫失效时是一个绿灯。
"""

from __future__ import annotations

import argparse
import contextlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# 只判这几个仓内目录下的引用；不在里面的（第三方源码、别人的桌面路径……）一律不管。
_SCOPE_PREFIXES = ("src/", "tests/", "scripts/", "docs/", "deploy/", "web/", ".github/")

# 历史日志：里面的行号是「当时的」，用今天的文件去比它等于要求改假。
_HISTORY: dict[str, str] = {
    "CHANGELOG.md": "历史日志：里面的行号与行为是当天的记录，拿今天的文件去比它等于要求改假",
}

_FENCE_MARKERS = ("```", "~~~")
_ESCAPED_TICK = "\\`"

# 双反引号 span：里面写「反斜杠 + 反引号」是**正解**（要显示一个字面反引号时），不是缺陷。
# 判据之前先把这类 span 拿掉，剩下的才是「想转义但转错了」。
# 内容允许含单个反引号（这正是它的用途）；所以用非贪婪匹配到下一对双反引号，
# 而不是 `[^`]+`（那样会把 `` \` `` 这种正解自己也当成缺陷）。
_DOUBLE_TICK_SPAN_RE = re.compile(r"``.*?``")

# 文档里混用两种写法：仓根相对的（`src/warden_agent/web/server.py:1433`）与**相对包根的短写**
# （`web/run_server.py:556-560`）。两种都认：先在仓根找，再到这个前缀下找。
_PACKAGE_PREFIX = "src/warden_agent/"

# 整个 span 正好是「路径:行号」才算引用；行号上限 6 位，避免把时间戳之类当引用。
_REF_RE = re.compile(r"^(?P<path>[^\s`'\"]+?):(?P<start>\d{1,6})(?:[-\u2013](?P<end>\d{1,6}))?$")

_RULE_ESCAPED_TICK = "行内代码里的反斜杠转义反引号"
_RULE_REF_RANGE = "路径:行号 引用越界"
_RULE_REF_MISSING = "路径:行号 引用的文件不存在"

_EXIT_CLEAN = 0
_EXIT_DEFECT = 1
_EXIT_UNABLE = 2


@dataclass(frozen=True)
class Finding:
    """一处缺陷：文档里的位置 + 判据 + 人话解释。"""

    file: str
    line: int
    rule: str
    detail: str


def _warn(message: str) -> None:
    # 这条走 stderr，而 main() 只 reconfigure 了 stdout ⇒ 在本机 cp936 控制台上这句话是乱码：
    # 「查不动」正是最需要人看清原因的一路（乱码比报红更坏：它把「为什么」也一起藏了）。
    with contextlib.suppress(AttributeError, ValueError):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    print(f"check_doc_claims: {message}", file=sys.stderr)


def _tracked_docs(root: Path | None = None) -> list[str]:
    """提交里的 Markdown 清单。

    argv 里只给 pathspec（``*.md``），后缀过滤交给 git——这样 ``tests`` 那侧可以用
    **另一条 argv**（``git ls-files '*.md'`` 加 ``--recurse-submodules`` 之类）当独立 oracle，
    而不是两边都调这个函数（第七十四批的教训：两边都调同一个辅助函数 ⇒ 断言恒真）。
    """
    proc = subprocess.run(
        ["git", "-c", "core.quotePath=false", "ls-files", "*.md"],
        cwd=root or _ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip()[:200]
        raise RuntimeError(f"git ls-files 失败（rc={proc.returncode}）：{detail}")
    return sorted(name for name in proc.stdout.splitlines() if name.strip())


def _fence_mask(source: str) -> tuple[str, set[int]]:
    """把围栏**之外**的正文掏空（行号保持），返回掏空后的文本 + 围栏内的行号集合。

    掏空的两个目的：正文不能跨空行/跨围栏拼出一个引用；行号仍是原文件的行号。
    只认 ``` 与 ~~~ 两种围栏；缩进四格的代码块不管（本仓文档不用那种写法），
    这是**有意记下的边界**，不是遗漏。
    """
    masked: list[str] = []
    fenced: set[int] = set()
    inside = False
    for number, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith(_FENCE_MARKERS):
            inside = not inside
            fenced.add(number)
            masked.append("")
            continue
        if inside:
            fenced.add(number)
            masked.append("")
            continue
        masked.append(line)
    return "\n".join(masked), fenced


def _inline_spans(text: str) -> list[str]:
    """一行里的行内代码 span 内容（单反引号包裹的）。

    双反引号包裹的（`` ``含有 ` 的内容`` ``）不在此列——那正是「反斜杠转义」的正解。
    """
    parts = text.split("`")
    return [part for index, part in enumerate(parts) if index % 2 == 1]


def _masked_lines(source: str) -> list[tuple[int, str]]:
    """(原文件行号, 围栏之外的该行文本)。"""
    masked, _fenced = _fence_mask(source)
    return list(enumerate(masked.splitlines(), start=1))


def _without_double_tick_spans(text: str) -> str:
    """抹掉整行里的双反引号 span（双反引号 + 非反引号内容 + 双反引号）。

    Markdown 的 span 不跨行，所以逐行匹配就够。抹成不含反斜杠的占位符，
    让里面的「反斜杠 + 反引号」不会在后面被当成缺陷。
    """
    return _DOUBLE_TICK_SPAN_RE.sub("[双反引号 span]", text)


def _file_lines(path: Path) -> int:
    return len(path.read_text(encoding="utf-8", errors="replace").splitlines())


def _resolve(base: Path, target: str) -> Path | None:
    """把文档里的引用解析成仓内文件；两种写法都认（见 ``_PACKAGE_PREFIX`` 处的注释）。"""
    for candidate in (base / target, base / _PACKAGE_PREFIX / target):
        if candidate.is_file():
            return candidate
    return None


def scan_docs(docs: list[str], root: Path | None = None) -> tuple[list[Finding], int, int]:
    """扫描给定文档，返回 (缺陷列表, 检查过的行内引用数, 读过的文件数)。

    不抛异常去「代表缺陷」：文件读不到是环境问题，由调用方决定怎么报。
    """
    base = root or _ROOT
    findings: list[Finding] = []
    checked_refs = 0
    read_files = 0

    for rel in docs:
        path = base / rel
        source = path.read_text(encoding="utf-8", errors="replace")
        read_files += 1
        for line_number, text in _masked_lines(source):
            if _ESCAPED_TICK in _without_double_tick_spans(text):
                findings.append(
                    Finding(
                        file=rel,
                        line=line_number,
                        rule=_RULE_ESCAPED_TICK,
                        detail=(
                            "GFM 在行内代码里不认反斜杠转义，这个反斜杠会被原样渲染出来；"
                            "改成双反引号包裹，或直接删掉反斜杠"
                        ),
                    )
                )
            if rel in _HISTORY:
                continue
            for span in _inline_spans(text):
                match = _REF_RE.match(span.strip())
                if match is None:
                    continue
                target = match.group("path")
                if not target.startswith(_SCOPE_PREFIXES):
                    continue
                checked_refs += 1
                target_file = _resolve(base, target)
                if target_file is None:
                    findings.append(
                        Finding(
                            file=rel,
                            line=line_number,
                            rule=_RULE_REF_MISSING,
                            detail=(
                                f"引用了 {target}，但仓根与 {_PACKAGE_PREFIX} 下都没有这个文件"
                            ),
                        )
                    )
                    continue
                start = int(match.group("start"))
                end = int(match.group("end") or start)
                total = _file_lines(target_file)
                if end > total or start > total:
                    findings.append(
                        Finding(
                            file=rel,
                            line=line_number,
                            rule=_RULE_REF_RANGE,
                            detail=(
                                f"引用 {target}:{start}"
                                + (f"-{end}" if end != start else "")
                                + f"，但该文件只有 {total} 行"
                            ),
                        )
                    )
    return findings, checked_refs, read_files


def main(argv: list[str] | None = None) -> int:
    # 本机控制台是 cp936，直接 print `✗`/`✅` 会 UnicodeEncodeError（本仓已踩过三次，见
    # scripts/check_md_tables.py 同一段注释）。门禁崩掉比报红更坏：它把「结论」换成了「traceback」。
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    parser = argparse.ArgumentParser(description="交付文档里的反引号转义与 路径:行号 守卫")
    parser.add_argument(
        "--root",
        default=str(_ROOT),
        help="仓库根目录（默认本脚本所在仓库；测试拿它对着临时小仓库跑）",
    )
    args = parser.parse_args(argv)
    root = Path(args.root)

    try:
        docs = _tracked_docs(root)
    except (RuntimeError, OSError) as exc:
        _warn(f"扫描集取不到（不是「没问题」，是「查不动」）：{exc}")
        return _EXIT_UNABLE

    try:
        findings, checked_refs, read_files = scan_docs(docs, root)
    except OSError as exc:
        _warn(f"读文档失败（不是「没问题」，是「查不动」）：{exc}")
        return _EXIT_UNABLE

    print(
        f"扫描 {read_files} 个 Markdown（跳过 {len(_HISTORY)} 个历史日志的行内引用）："
        f"检查行内引用 {checked_refs} 处"
    )
    if findings:
        for finding in findings:
            print(f"  ✗ {finding.file}:{finding.line} — {finding.rule}：{finding.detail}")
        print(f"缺陷 {len(findings)} 处 ⛔")
        return _EXIT_DEFECT
    print("缺陷 0 处 ✅")
    return _EXIT_CLEAN


if __name__ == "__main__":
    raise SystemExit(main())
