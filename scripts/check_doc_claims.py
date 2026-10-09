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

判据三：文档里「**定义在**某文件」这句话必须为真。
    文档里的「谁定义在哪个文件」是照着 grep 的地图。实测抓到一处漂移：
    ``docs/deployment-boundaries.md`` 说会话缓存 ``SessionRegistry._sessions`` 住在
    ``web/server.py``，而它实际上在 ``src/warden_agent/web/session_registry.py``。

    **判据靠 ast，不靠文本搜索**：``web/server.py`` 里有 ``import ... SessionRegistry``，
    文本法会被这行 import 骗过去——“提到”不等于“定义在”。

    为什么只认写出来的「定义在/实现在/位于/住在/定义于」（见 ``_DEFINITION_MARKERS``）：
    文档在同一行里把「符号」和「文件」摆在一起，**多数时候不是定义主张**——可能是“这条规则由
    该测试守着”“这个值在那里被读”。第七十七批实测过两种写法：按“同行共现”配对 ⇒ 真仓
    681 处报告，绝大多数是噪声（``pg_dump``、``docker.m.daocloud.io`` 都成了「符号」）；改成
    “紧挨着的下一个 span” ⇒ 降到 16 处，仍有 ``Dockerfile``、``uv.lock``、``Pytest``、
    ``AssertionError`` 这类假阳。结论：**共现与邻接都不是主张，只有写出来的谓语才是**。

    这条判据的边界（**故意保守**）：
      - 只判带明确谓语的句子；没写谓语的引用（“`X`（`path:12`）”）只能靠人读，机器不猜。
      - 只判**定义在不在**：符号在该文件里被 *调用* 也算合格（调用点本来就是合法坐标）；
        行号是否指向定义处**不判**（同一文件里的调用点、测试点都是合理引用）。
      - 符号得**看着像代码符号**（含下划线、或驼峰、或带点）；``ruff``/``pytest`` 这类
        同行的工具名不算；文件**自己的名字**（``check_doc_claims`` 之于
        ``scripts/check_doc_claims.py``）算。

三条判据的边界（**故意保守**，宁可少报也不放假阳）：

  - 只在**围栏之外**判。围栏里是「给别人抄的脚本原文」，属于 ``scripts/check_pinned_images.py``
    的判据范围，不是这里。
  - ``路径:行号`` 只在**整个行内 span 正好是「路径:行号」**（可带 ``12-34`` 区间）时判；
    命令行里夹一半的（``python scripts/x.py:12 --flag``）不算，因为那更像示例而不是坐标。
  - 引用必须落在**本仓目录**（见 ``_in_scope``）下才判；``mutmut/mutation/core.py:395``
    这类第三方源码引用天然被排除。范围里「相对包根的短写」（``store/base.py``）**由包目录派生**，
    不手写列表：手写过一次，漏了 ``store/``，于是「``RunStore`` 定义在 ``store/base.py``」这类主张
    一条都没被核——「没被核」与「核过是对的」在输出里长得一模一样（第七十七批自查抓到）。
  - **裸文件名不判**（``cli.py``、``agent.py``）：仓里有同名文件（``tools.py`` 三处、
    ``trigger.py`` 两处……），要判就得猜「最接近的那个」，而猜出来的坐标命名不出证据。
    目前文档里也没有「裸名:行号」这种写法，所以这条边界今天不欠账。
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
import ast
import contextlib
import functools
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# 只判这几个仓内目录下的引用；不在里面的（第三方源码、别人的桌面路径……）一律不管。
# 「相对包根的短写」（`store/base.py`）不在这里手写，见 `_short_form_prefixes`。
_TOP_LEVEL_PREFIXES = ("src/", "tests/", "scripts/", "docs/", "deploy/", ".github/")

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


@functools.lru_cache(maxsize=8)
def _short_form_prefixes(base: Path) -> tuple[str, ...]:
    """短写前缀 = 包目录里挂着的子包目录，从文件系统派生（跳过 ``__pycache__`` 这类双下划线目录）。

    为什么要派生而不是手写：第一版的清单里没有 ``store/``，于是文档里
    「``RunStore`` 定义在 ``store/base.py``」这类主张**一条都没进判据**——缺陷是 0、
    核对配对也是 0，和「核过确实是对的」在输出里长得一模一样（第七十七批自查抓到）。
    派生还有个好处：包里新加子包，判据范围自动跟着变，不会静悄悄落后于目录结构。
    """
    package = base / _PACKAGE_PREFIX
    if not package.is_dir():
        return ()
    subpackages = [
        path.name + "/"
        for path in package.iterdir()
        if path.is_dir() and not path.name.startswith("__")
    ]
    return tuple(sorted(subpackages))


def _in_scope(target: str, base: Path) -> bool:
    """这个引用算不算本仓坐标。判定放在解析之前：范围之外的一律不碰。"""
    return target.startswith(_TOP_LEVEL_PREFIXES) or target.startswith(_short_form_prefixes(base))

# 整个 span 正好是「路径:行号」才算引用；行号上限 6 位，避免把时间戳之类当引用。
_REF_RE = re.compile(r"^(?P<path>[^\s`'\"]+?):(?P<start>\d{1,6})(?:[-\u2013](?P<end>\d{1,6}))?$")

# 判据三用：同一行里的「裸符号 span」与「.py 路径 span（可带行号）」。两者成对出现才判。
_SYMBOL_RE = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*$")
_PATH_RE = re.compile(
    r"^(?P<path>[^\s`'\"]+\.py)(?::(?P<start>\d{1,6})(?:[-\u2013](?P<end>\d{1,6}))?)?$"
)
# 「符号」与「路径」两个 span 之间最多隔这么多字符才算一处主张（见 _symbol_pair_findings）。
_ADJACENT_GAP = 12

# 只有写出这些谓语的句子才算「定义在哪」的主张；没写谓语的引用只能靠人读，机器不猜
# （理由与实测数字见模块 docstring 的判据三）。
_DEFINITION_MARKERS = ("定义在", "定义于", "实现在", "住在", "位于")

_RULE_ESCAPED_TICK = "行内代码里的反斜杠转义反引号"
_RULE_REF_RANGE = "路径:行号 引用越界"
_RULE_REF_MISSING = "路径:行号 引用的文件不存在"
_RULE_SYMBOL_NOT_DEFINED = "点名的符号不定义在被引用的文件里"

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


def _looks_like_code_symbol(text: str) -> bool:
    """像代码符号而不是小写工具名的裸 span（判据三的第一道闸，先卡掉 ruff/pytest 这类）。

    放行的是文档会点名的东西：``SessionRegistry``、``SessionRegistry._sessions``、
    ``idem_key``、``scan_docs``。拦掉的是 ``ruff``/``pytest``/``git`` 这种小写单词工具名。
    """
    if "." in text or "_" in text:
        return True
    return text[:1].isupper() and any(char.islower() for char in text)


def _target_names(target: ast.expr) -> list[str]:
    """赋值目标的标识符（``X = ...`` / ``self.x = ...`` / ``a, b = ...`` / ``X[0] = ...``）。"""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Attribute):
        return [target.attr]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for element in target.elts for name in _target_names(element)]
    if isinstance(target, ast.Subscript):
        return _target_names(target.value)
    return []


def _defined_names(path: Path) -> dict[str, int]:
    """该 ``.py`` 文件里**定义出来**的名字 → 定义行号。

    用 ast，不用文本搜索：``web/server.py`` 里 ``import ... SessionRegistry`` 会让
    ``"SessionRegistry" in source`` 为真，而它恰恰**不是**定义——第七十七批修的那处漂移
    就是靠这行 import 骗过文本法的。文件自己的名字（``check_doc_claims`` 之于
    ``scripts/check_doc_claims.py``）也算「本来就该在这儿」的名字。

    认这几种定义：类、函数/方法（含 async）、赋值与带注解赋值（``self._sessions`` 这类也收，
    因为文档会用 ``类.成员`` 的形式点名）。语法错的文件不抛异常——返回只剩文件自己名字的字典，
    让「解析不了」表现为「名字找不到」；这类文件在 CI 里另有 ruff/mypy 兜着。
    """
    names: dict[str, int] = {path.stem: 1}
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except (OSError, SyntaxError, ValueError):
        return names
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.setdefault(node.name, node.lineno)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                for name in _target_names(target):
                    names.setdefault(name, node.lineno)
        elif isinstance(node, ast.AnnAssign):
            for name in _target_names(node.target):
                names.setdefault(name, node.lineno)
    return names


def _spans_with_positions(text: str) -> list[tuple[int, int, str]]:
    """行内代码 span 的 (起, 止, 内容) —— 与 ``_inline_spans`` 同一套切法（按单反引号切、
    取奇数段）。

    判据三要的是**位置**：同一行里带着「定义在」这类谓语的「符号 + 路径」才算主张。
    """
    parts = text.split("`")
    spans: list[tuple[int, int, str]] = []
    cursor = 0
    for index, part in enumerate(parts):
        if index % 2 == 1:
            spans.append((cursor, cursor + len(part), part.strip()))
        cursor += len(part) + 1
    return spans


def _symbol_pair_findings(
    file: str,
    line: int,
    text: str,
    base: Path,
    definitions: dict[Path, dict[str, int]],
) -> tuple[list[Finding], int]:
    """判据三：写了「定义在/实现在/位于」的句子，被点名的符号必须真定义在那个文件里。

    只认符号 span 后面紧跟的那一个：中间夹着谓语、且它是仓内 ``.py`` 路径才判；
    否则放过（宁可少报也不放假阳——实测把“共现”或“邻接”当主张会报出一堆 ``Dockerfile``
    这类假阳，见模块 docstring 判据三的实测数字）。
    返回 (缺陷, 核对过的配对数)：配对数若为 0，就该怀疑判据没在跑。
    """
    spans = _spans_with_positions(text)
    findings: list[Finding] = []
    checked = 0
    for index, (_start, end, symbol) in enumerate(spans):
        if not (_SYMBOL_RE.match(symbol) and _looks_like_code_symbol(symbol)):
            continue
        if index + 1 >= len(spans):
            continue
        neighbour_start, _neighbour_end, neighbour = spans[index + 1]
        if neighbour_start - end > _ADJACENT_GAP:
            continue
        if not any(marker in text[end:neighbour_start] for marker in _DEFINITION_MARKERS):
            continue
        match = _PATH_RE.match(neighbour)
        if match is None:
            continue
        target = match.group("path")
        if not _in_scope(target, base):
            continue
        target_file = _resolve(base, target)
        if target_file is None or target_file.suffix != ".py":
            continue
        if target_file not in definitions:
            definitions[target_file] = _defined_names(target_file)
        checked += 1
        if symbol.split(".")[0] in definitions[target_file]:
            continue
        findings.append(
            Finding(
                file=file,
                line=line,
                rule=_RULE_SYMBOL_NOT_DEFINED,
                detail=(
                    f"写了 {symbol} 定义在 {target}，但这个文件里没有定义它"
                    "（“提到”不算：那行 import 也能让文本搜索命中）"
                ),
            )
        )
    return findings, checked


def _file_lines(path: Path) -> int:
    return len(path.read_text(encoding="utf-8", errors="replace").splitlines())


def _resolve(base: Path, target: str) -> Path | None:
    """把文档里的引用解析成仓内文件；两种写法都认（见 ``_PACKAGE_PREFIX`` 处的注释）。"""
    for candidate in (base / target, base / _PACKAGE_PREFIX / target):
        if candidate.is_file():
            return candidate
    return None


def scan_docs(docs: list[str], root: Path | None = None) -> tuple[list[Finding], int, int, int]:
    """扫描给定文档，返回 (缺陷列表, 检查过的行内引用数, 读过的文件数, 核对过的「定义在」配对数)。

    不抛异常去「代表缺陷」：文件读不到是环境问题，由调用方决定怎么报。
    最后一个返回值是判据三的「地板」：它是 0 就意味着那条判据没在跑（而不是「文档都写对了」）。
    """
    base = root or _ROOT
    findings: list[Finding] = []
    checked_refs = 0
    checked_pairs = 0
    read_files = 0
    # 每个 .py 只解析一次：文档里同一个文件往往被引用很多次。
    definitions: dict[Path, dict[str, int]] = {}

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
            spans = [span.strip() for span in _inline_spans(text)]
            for span in spans:
                match = _REF_RE.match(span)
                if match is None:
                    continue
                target = match.group("path")
                if not _in_scope(target, base):
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
            pair_findings, pairs = _symbol_pair_findings(
                rel, line_number, text, base, definitions
            )
            checked_pairs += pairs
            findings.extend(pair_findings)
    return findings, checked_refs, read_files, checked_pairs


def main(argv: list[str] | None = None) -> int:
    # 本机控制台是 cp936，直接 print `✗`/`✅` 会 UnicodeEncodeError（本仓已踩过三次，见
    # scripts/check_md_tables.py 同一段注释）。门禁崩掉比报红更坏：它把「结论」换成了「traceback」。
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    parser = argparse.ArgumentParser(
        description="交付文档里的反引号转义、路径:行号 与「符号定义在它引用的文件里」守卫"
    )
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
        findings, checked_refs, read_files, checked_pairs = scan_docs(docs, root)
    except OSError as exc:
        _warn(f"读文档失败（不是「没问题」，是「查不动」）：{exc}")
        return _EXIT_UNABLE

    print(
        f"扫描 {read_files} 个 Markdown（跳过 {len(_HISTORY)} 个历史日志的行内引用）："
        f"检查行内引用 {checked_refs} 处、核对「符号定义在它引用的文件里」{checked_pairs} 处"
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
