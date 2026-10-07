r"""Markdown 表格里未转义的竖线：GFM 把它们当分隔符，而本地 diff 看不出来（第七十二批）。

为什么要有这条：

- 失败模式是**静默**的。表格单元格里的竖线（尤其是写在代码 span 里的，如 `` `limit|max_` ``）
  会被 GFM 当成列分隔符：那一行在 GitHub 上多切出格子、反引号变成字面量；一行比表头多格时，
  多出来的内容被**直接丢掉**。而 `git diff` / 编辑器里这段 Markdown 看起来完全正常——
  第七十一批就是靠人肉写脚本才在 `docs/复核索引.md` 里翻出 2 处（先于那批存在）。
- 权威渲染器的实测（第七十二批，`gh api -X POST /markdown -f mode=gfm`，原始 HTML 存档在
  `wa72/out.html`，结论抄进 CHANGELOG）：

    | 输入（2 列表头） | 渲染结果 | 判定 |
    | --- | --- | --- |
    | `` | `x|y` | z | `` | `<td>\`x</td><td>y\`</td>` | 切列 + 反引号当字面量 ⇒ 缺陷 |
    | `` | `x\|y` | z | `` | `<td><code class="notranslate">x|y</code></td><td>z</td>` | 正确写法 |
    | `` | a | b | c | d | ``（多 2 格） | 多出来的格子不出现在 HTML 里 | 静默丢弃 ⇒ 缺陷 |
    | `` | a | ``（少 1 格） | 补一个空格子 | 无害，但"漏写"与"有意留空"分不清 ⇒ 缺陷 |

- 本守卫**不**去问 GitHub 渲染得对不对（CI 里不许依赖网络），它按 GFM 的解析规则自己数格：
  只认字面量、不做推断，所以在本地与 CI 给出同一个结论。

有意**不**检查的（写清楚，免得下一个人以为漏了）：

- 内联 HTML（`<br>` 之类）不解析；只数竖线。
- 不在表格块里的竖线（例如散文里的 shell 片段 `a || b`）不算缺陷。
- 围栏代码块（``` / ~~~）整块跳过——那里的竖线就是字面量。

用法：
    python scripts/check_md_tables.py                  # 守本仓库所有 Markdown
    python scripts/check_md_tables.py --root <目录>     # 守任意目录树（负向对照用）
    python scripts/check_md_tables.py --paths a.md b.md
"""

from __future__ import annotations

import argparse
import contextlib
import re
import sys
from collections.abc import Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# 目录名一跳过就不进（`**` 会进 node_modules，那里有成百上千个 README）
SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        # 本机审计工具（mimosa）的产物目录，已在 .gitignore（第 53 行）、不在交付面上。
        # 不排除它的话，CI 的干净 clone（那里没有这个目录）与本机扫到的文件数就不一样了——
        # 门禁在本地红、在 CI 绿，是假红里最难查的一种。
        ".mimosa",
        # 本机才有的**产物**目录（`.zcode/` = Agent 计划产物、`mutants/` = 变异脚本产物，
        # 均在 .gitignore 里）：
        # 第七十二批实测——只排 `.mimosa/` 时，本机扫 12 个、提交树（= CI 的干净 clone）只扫 10 个，
        # 差集就是这两个目录里的文件。它们今天干净所以不红，但一旦哪个产物里出现表格缺陷，
        # 就是“本机红、CI 绿”，而这种情况最难查。所以排除集由测试钉住（
        # `test_本机扫到的文件集与提交里的文件集一致`）——新增产物目录时它会红，逼你做一次明示选择。
        ".zcode",
        "mutants",
        "node_modules",
        "__pycache__",
        "build",
        "dist",
        "site-packages",
    }
)
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
DELIM_CELL_RE = re.compile(r"^:?-+:?$")


class Defect:
    """一处渲染缺陷：kind 是说给机器看的，message 是说给下一个人看的。"""

    def __init__(self, kind: str, path: str, line: int, column: int, message: str) -> None:
        self.kind = kind
        self.path = path
        self.line = line
        self.column = column
        self.message = message

    def render(self) -> str:
        where = f"{self.path}:{self.line}"
        if self.column:
            where = f"{where}:{self.column}"
        return f"❌ {where} {self.message}"


def scan_row(line: str) -> tuple[list[int], list[int]]:
    """返回 (所有未转义竖线的列号, 其中落在代码 span 里的竖线列号)。

    列号是 0-based 的字符下标。反引号串按 CommonMark 的规则配对：**同样长度**的
    反引号串关闭（`` `` `` 不会关掉 `` ` ``），这样 `` `a``|b` `` 这种写法不会算错。
    """
    pipes: list[int] = []
    in_code_pipes: list[int] = []
    open_run = 0  # 0 = 不在代码 span 里，否则是开启它的反引号个数
    index = 0
    length = len(line)
    while index < length:
        char = line[index]
        if char == "\\":
            index += 2  # 转义：下一个字符是字面量，不参与任何判定
            continue
        if char == "`":
            end = index
            while end < length and line[end] == "`":
                end += 1
            run = end - index
            if open_run == 0:
                open_run = run
            elif run == open_run:
                open_run = 0
            index = end
            continue
        if char == "|":
            pipes.append(index)
            if open_run:
                in_code_pipes.append(index)
        index += 1
    return pipes, in_code_pipes


def split_cells(line: str) -> list[str]:
    """按 GFM 的规则把一行切成格子：行首/行尾的竖线是可选边框，要去掉。"""
    pipes, _ = scan_row(line)
    if not pipes:
        return [line.strip()]
    pieces: list[str] = []
    start = -1
    for position in pipes:
        pieces.append(line[start + 1 : position])
        start = position
    pieces.append(line[start + 1 :])
    if pieces and pieces[0].strip() == "":
        pieces.pop(0)  # 行首竖线
    if pieces and pieces[-1].strip() == "":
        pieces.pop()  # 行尾竖线
    return [piece.strip() for piece in pieces]


def is_delimiter_row(line: str) -> bool:
    """GFM 的分隔行：每个格子都是 `:?-+:?`；且**自己必须带竖线**。

    要求带竖线是为了不把 setext 标题的 `---` 认成 1 列表格：
    `a | b` 后面单独一行 `---` 是标题下划线，不是表格（已用 GitHub 渲染器实测，
    见 tests/test_md_tables.py 里那条负向对照）。
    """
    if not _has_pipe(line):
        return False
    cells = split_cells(line)
    if not cells:
        return False
    return all(DELIM_CELL_RE.match(cell) for cell in cells)


def _has_pipe(line: str) -> bool:
    return bool(scan_row(line)[0])


def iter_markdown_files(root: Path, globs: tuple[str, ...]) -> Iterator[Path]:
    seen: set[Path] = set()
    for pattern in globs:
        for path in sorted(root.glob(pattern)):
            if not path.is_file() or path.suffix.lower() != ".md":
                continue
            if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
                continue
            if path not in seen:
                seen.add(path)
                yield path


def _code_span_hint(line: str, column: int) -> str:
    """把这处竖线所在的代码 span 原文摘出来（摘不到就给空串）。"""
    start = line.rfind("`", 0, column)
    end = line.find("`", column)
    if start == -1 or end == -1 or end <= column:
        return ""
    return f"（本行的 {line[start : end + 1]} 这处代码 span）"


def scan_text(text: str, path: str) -> tuple[list[Defect], int]:
    """扫一份 Markdown，返回 (缺陷, 表格张数)。"""
    defects: list[Defect] = []
    lines = text.splitlines()
    tables = 0
    in_fence = False
    fence_char = ""
    fence_len = 0
    index = 0
    while index < len(lines):
        line = lines[index]
        fence = FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)
            if not in_fence:
                in_fence, fence_char, fence_len = True, marker[0], len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_len:
                in_fence = False
            index += 1
            continue
        if in_fence:
            index += 1
            continue
        next_line = lines[index + 1] if index + 1 < len(lines) else ""
        if not (_has_pipe(line) and is_delimiter_row(next_line)):
            index += 1
            continue
        tables += 1
        header_cells = split_cells(line)
        delim_cells = split_cells(next_line)
        expected = len(header_cells)
        if len(delim_cells) != expected:
            defects.append(
                Defect(
                    "header_delim_mismatch",
                    path,
                    index + 2,
                    0,
                    f"表头是 {expected} 格、分隔行是 {len(delim_cells)} 格 ⇒ 这两行不构成表格，"
                    "整块会被渲染成普通段落（竖线原样露出来）。"
                    "修法：把两行改成同一个格数。",
                )
            )
        for row_index in range(index, len(lines)):
            row = lines[row_index]
            if row_index == index + 1:
                continue  # 分隔行：格数由它自己定义，不参与"行 vs 表头"比较
            if row_index > index and (not row.strip() or not _has_pipe(row)):
                break  # 空行/没有竖线：表格到此结束
            if row_index > index and FENCE_RE.match(row):
                break
            cells = split_cells(row)
            _, in_code_pipes = scan_row(row)
            for column in in_code_pipes:
                defects.append(
                    Defect(
                        "pipe_in_code_span",
                        path,
                        row_index + 1,
                        column + 1,
                        "代码 span 里的竖线没转义"
                        f"{_code_span_hint(row, column)}：GFM 先按竖线切格、后解析行内，"
                        "所以它会被当分隔符——这一行的内容被切开、反引号当字面量显示。"
                        r"修法：在代码 span 里也写 \|（渲染出来就是 |）。",
                    )
                )
            if row_index > index + 1 and len(cells) != expected:
                extra = len(cells) - expected
                if extra > 0:
                    why = (
                        f"多出来的 {extra} 格内容在 GitHub 上会被**静默丢掉**（实测："
                        "多出的格不出现在渲染后的 HTML 里）"
                    )
                else:
                    why = f"少的 {-extra} 格会被补成空格子（看着像漏写，读者分不清是否有意）"
                defects.append(
                    Defect(
                        "cell_count_mismatch",
                        path,
                        row_index + 1,
                        0,
                        f"这一行被切成 {len(cells)} 格，表头是 {expected} 格 ⇒ {why}。"
                        r"修法：把竖线写成 \|，或者补齐/合并格子。",
                    )
                )
        index += 1
    return defects, tables


def audit(
    root: Path, globs: tuple[str, ...], paths: list[Path] | None
) -> tuple[list[Defect], int, int]:
    """返回 (缺陷, 文件数, 表格张数)。"""
    targets = paths if paths else list(iter_markdown_files(root, globs))
    defects: list[Defect] = []
    tables = 0
    for path in targets:
        rel = path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # 读不出来也是缺陷：守不住的东西不能算通过
            defects.append(Defect("unreadable", rel, 0, 0, f"读不出这个文件：{exc}"))
            continue
        found, count = scan_text(text, rel)
        defects.extend(found)
        tables += count
    return defects, len(targets), tables


def main(argv: list[str] | None = None) -> int:
    # 本机控制台是 cp936，直接 print `❌` 会 UnicodeEncodeError（本仓已踩过三次：
    # scripts/load_test.py、rag/eval.py、check_pinned_images.py）。门禁崩掉比报红更坏：
    # 它把"结论"换成了"traceback"。
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    parser = argparse.ArgumentParser(description="Markdown 表格里未转义的竖线会被 GFM 当分隔符")
    parser.add_argument("--root", default=str(REPO_ROOT), help="仓库根目录（默认本脚本所在仓库）")
    parser.add_argument(
        "--glob",
        action="append",
        default=None,
        help="要扫的 Markdown 通配（可重复，默认 `**/*.md`）",
    )
    parser.add_argument("--paths", nargs="*", default=None, help="只扫这些文件（负向对照用）")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"❌ 不是目录：{root}")
        return 2
    globs = tuple(args.glob) if args.glob else ("**/*.md",)
    paths = None
    if args.paths:
        paths = [Path(item) if Path(item).is_absolute() else root / item for item in args.paths]
    defect_list, files, tables = audit(root, globs, paths)
    for defect in defect_list:
        print(defect.render())
    print(f"\n扫描 {files} 个 Markdown / {tables} 张表：缺陷 {len(defect_list)} 处")
    if not files:
        # 「一个文件都没扫到」和「全部干净」在输出里长得一模一样——这正是本批要防的那种缺口：
        # 门禁自己空转（比如 CI 步骤里的 glob 被改错）会直接绿灯。所以空扫描不是通过，是启动失败。
        print(f"❌ 一个 Markdown 都没扫到（glob={globs}，root={root}）：门禁空转不能算通过")
        return 2
    if defect_list:
        return 1
    print("✅ 表格里没有未转义的竖线")
    return 0


if __name__ == "__main__":
    sys.exit(main())
