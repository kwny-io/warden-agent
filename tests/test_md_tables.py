"""Markdown 表格里未转义的竖线会被 GFM 当分隔符（第七十二批）。

为什么要有这条守卫、以及权威渲染器的实测证据（`gh api -X POST /markdown -f mode=gfm`
返回的原始 HTML），写在 `scripts/check_md_tables.py` 的模块文档里；这里只钉判据本身：

- **必须红**：代码 span 里的未转义竖线（内容被切开、反引号变成字面量）、行比表头多格
  （多出来的内容在渲染结果里**直接消失**）、行比表头少格（补空格子，读者分不清是不是漏写）、
  表头与分隔行格数不一致（那两行不构成表格，整块渲染成普通段落、竖线原样露出来）。
- **必须不红**（假红比漏检更坏：它教人忽略门禁）：转义后的 `\\|`、setext 标题那行单独的
  `---`、围栏代码块里的竖线、散文里的 `a || b`。

为什么要有 `test_仓库里的Markdown现在是干净的` 这条"真值"用例：上面全是对着**人造字符串**
做的判据测试。真正要守的是**交付面上的那些文件**——README 上那张表在 GitHub 上是不是完整的、
`docs/复核索引.md` 的审计表有没有丢格。所以必须有一条对着真仓库跑的用例（它在 CI 的干净
clone 里跑，也因此顺带守住"本机能过、CI 里也要能过"）。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]


def _load_guard() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_md_tables", ROOT / "scripts" / "check_md_tables.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_GUARD = _load_guard()

# 2 列表头的合法表格：后面每个用例都拿它当基准，只改要被检查的那一行。
_HEADER = "| 列一 | 列二 |\n| --- | --- |\n"


def _defects(text: str) -> list:
    found, _ = _GUARD.scan_text(text, "样例.md")
    return found


def _kinds(text: str) -> list[str]:
    return sorted({defect.kind for defect in _defects(text)})


def test_合法表格零缺陷() -> None:
    assert _defects(_HEADER + "| a | b |\n| c | d |\n") == []


def test_代码span里未转义的竖线必须红() -> None:
    """这是本守卫的招牌用例：`` `x|y` `` 会被切成两格，反引号当字面量显示。"""
    text = _HEADER + "| `WARDEN_SEARCH_PROVIDER=tavily|brave` | z |\n"
    found = _defects(text)
    kinds = {defect.kind for defect in found}
    assert "pipe_in_code_span" in kinds, found
    assert "cell_count_mismatch" in kinds, "切列了就必须同时报格数不一致（两条判据互相印证）"
    span = [defect for defect in found if defect.kind == "pipe_in_code_span"]
    assert len(span) == 1
    assert span[0].line == 3 and span[0].column == 33, (span[0].line, span[0].column)
    assert "反引号当字面量" in span[0].message
    assert span[0].render().startswith("❌ 样例.md:3:33")


def test_代码span里转义的竖线必须过() -> None:
    assert _defects(_HEADER + r"| `tavily\|brave` | z |" + "\n") == []


def test_双反斜杠后面的竖线仍然算分隔符() -> None:
    """`\\` 是一个真的反斜杠，它后面的竖线还是分隔符（GitHub 渲染器实测：`| a\\ | b |` →
    两个 <th>，第一个是 `a\\`）。这条防的是“把 `\\|` 也当成转义”那种过度宽容。
    """
    assert _defects(_HEADER + "| a\\\\ | b |\n") == []


def test_双反引号span里的竖线也要红() -> None:
    """反引号串按长度配对：`` `` `` 开着的时候，里面的单反引号不能把它关掉。"""
    assert "pipe_in_code_span" in _kinds(_HEADER + "| ``a|b`` | z |\n")


def test_行比表头多格必须红_因为多出来的内容会消失() -> None:
    found = _defects(_HEADER + "| a | b | c |\n")
    assert [defect.kind for defect in found] == ["cell_count_mismatch"]
    assert "静默丢掉" in found[0].message
    assert "被切成 3 格" in found[0].message


def test_行比表头少格必须红() -> None:
    found = _defects(_HEADER + "| a |\n")
    assert [defect.kind for defect in found] == ["cell_count_mismatch"]
    assert "补成空格子" in found[0].message


def test_表头与分隔行格数不一致必须红() -> None:
    found = _defects("| a | b |\n| --- | --- | --- |\n| x | y |\n")
    assert "header_delim_mismatch" in {defect.kind for defect in found}
    assert "不构成表格" in found[0].message


def test_setext标题不会被当成表格() -> None:
    """`a | b` 后面单独一行 `---` 是 H2 下划线（GitHub 渲染器实测：<h2>，不是 <table>）。

    这条是**假红防护**：本仓文档里大量用 `---` 收尾段落，一旦这里误判，门禁就会对着
    完全正常的文档报红，而"门禁会乱红"比"门禁漏检"更坏。
    """
    defects, tables = _GUARD.scan_text("a | b\n---\n", "样例.md")
    assert defects == [] and tables == 0


def test_围栏代码块里的竖线不算缺陷() -> None:
    text = "```\n| `x|y` | z |\n| --- | --- |\n```\n"
    defects, tables = _GUARD.scan_text(text, "样例.md")
    assert defects == [] and tables == 0


def test_散文里的竖线不算缺陷() -> None:
    defects, tables = _GUARD.scan_text("命令是 `a || b`，或者 a | b 这样写。\n", "样例.md")
    assert defects == [] and tables == 0


def test_本机脏目录不会被算进扫描范围() -> None:
    """本机才有的产物目录（`.mimosa/`、`.zcode/`、`mutants/`，都在 .gitignore）必须排除。

    不排除的话，CI 的干净 clone 与本机扫到的**文件数不同**——同一个仓库在本地红、在 CI 绿，
    是假红里最难查的一种（第七十二批实测：只排 `.mimosa/` 时，本机 12 个、提交树 10 个）。
    """
    root = ROOT
    files = {
        path.relative_to(root).as_posix()
        for path in _GUARD.iter_markdown_files(root, ("**/*.md",))
    }
    for dirty in (".mimosa/", ".zcode/", "mutants/"):
        assert not any(name.startswith(dirty) for name in files), (dirty, sorted(files)[:5])
    assert "README.md" in files


def test_本机扫到的文件集与提交里的文件集一致() -> None:
    """守卫看的文件集必须**等于** git 跟踪的集合——否则“本机红、CI 绿”是迟早的事。

    价值不在于今天这两个目录，而在于**下一个**产物目录：新目录里出现 `.md` 时这条用例会红，
    逼你在“跟进提交”与“加进 SKIP_DIRS 并写清理由”之间做一次明示选择。
    同一个道理：门禁自己的缺席也要看得见（与 `test_扫到零个文件不算通过` 同源）。
    """
    root = ROOT
    scanned = {
        path.relative_to(root).as_posix()
        for path in _GUARD.iter_markdown_files(root, ("**/*.md",))
    }
    tracked_raw = subprocess.run(
        ["git", "ls-files", "-z", "*.md"], cwd=root, capture_output=True, check=True
    ).stdout
    tracked = {name for name in tracked_raw.decode("utf-8").split("\0") if name}
    assert scanned == tracked, (
        "守卫扫到的文件集与 git 跟踪的不一致：\n"
        f"  只在本机（未被跟踪）={sorted(scanned - tracked)}\n"
        f"  只在提交里={sorted(tracked - scanned)}\n"
        "未被跟踪的产物目录 ⇒ 加进 scripts/check_md_tables.py 的 SKIP_DIRS（并写清理由）；"
        "新文档 ⇒ 跟进提交。"
    )


def test_仓库里的Markdown现在是干净的() -> None:
    """真值守卫：交付面上的 Markdown 一条缺陷都不许有。"""
    defects, files, tables = _GUARD.audit(ROOT, ("**/*.md",), None)
    assert tables >= 50, f"只扫到 {tables} 张表——守卫本身失效了（应该上百张）"
    assert files >= 5, f"只扫到 {files} 个 Markdown——守卫本身失效了"
    assert defects == [], "\n".join(defect.render() for defect in defects)


def test_负面对照_把真实文件改坏必须红(tmp_path: Path) -> None:
    """拿**真的 README**（而不是人造字符串）做一次对照：改坏一处，必须变红。

    这条证明"本守卫能抓住真实交付面的缺陷"，而不只是能抓住测试里编的那几个字符串。
    """
    source = (ROOT / "README.md").read_text(encoding="utf-8")
    escaped = r"WARDEN_SEARCH_PROVIDER=tavily\|brave\|custom"
    assert escaped in source, "README 里那处转义没了？先看看是不是又被人改回去了"
    broken = tmp_path / "README.md"
    broken.write_text(source.replace(escaped, escaped.replace("\\", "")), encoding="utf-8")
    defects, files, tables = _GUARD.audit(tmp_path, ("**/*.md",), None)
    assert files == 1 and tables >= 5, (files, tables)
    kinds = {defect.kind for defect in defects}
    assert kinds == {"pipe_in_code_span", "cell_count_mismatch"}, defects


def test_守卫真的接在ciyml里() -> None:
    """守卫没接进 CI 就等于只是文档（第七十一批的教训：人肉脚本不是交付面）。"""
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "uv run --frozen python scripts/check_md_tables.py" in workflow


def test_扫到零个文件不算通过(tmp_path: Path) -> None:
    """门禁空转（glob 打错 / 根目录指错）必须显式失败。

    「一个文件都没扫到」与「全部干净」在输出里长得一模一样，而 CI 只看退出码：
    空转绿灯就是本批主题（缺席要看得见）里最容易漏的一种形状——**门禁自己的缺席**。
    """
    empty = tmp_path / "empty"
    empty.mkdir()
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "check_md_tables.py"),
            "--root",
            str(empty),
            "--glob",
            "**/*.md",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
        cwd=str(ROOT),
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "一个 Markdown 都没扫到" in proc.stdout
    assert "✅" not in proc.stdout


def test_CLI退出码契约(tmp_path: Path) -> None:
    """CI 只看退出码：绿必须是 0，红必须是 1，且红的时候要能读出是哪一行。"""
    good = tmp_path / "good.md"
    good.write_text(_HEADER + "| a | b |\n", encoding="utf-8")
    good_proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_md_tables.py"), "--paths", str(good)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
        cwd=str(ROOT),
    )
    assert good_proc.returncode == 0, good_proc.stdout + good_proc.stderr
    assert "✅" in good_proc.stdout

    bad = tmp_path / "bad.md"
    bad.write_text(_HEADER + "| a | b | c |\n", encoding="utf-8")
    bad_proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_md_tables.py"), "--paths", str(bad)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
        cwd=str(ROOT),
    )
    assert bad_proc.returncode == 1, bad_proc.stdout + bad_proc.stderr
    assert "❌" in bad_proc.stdout and "bad.md:3" in bad_proc.stdout
