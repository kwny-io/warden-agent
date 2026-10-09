"""交付文档里的「反引号转义」与「路径:行号」守卫（第七十六批）。

为什么要有这条守卫、以及它守的两个真实失败模式（GFM 在行内代码里不认反斜杠转义；
文档点名的行号已经越界）写在 `scripts/check_doc_claims.py` 的模块文档里。这里只钉判据：

- **必须红**：`路径:行号` 超出目标文件行数；引用一个不在提交里的文件；正文里出现裸的
  「反斜杠 + 反引号」写法。
- **必须不红**（假红比漏检更坏，它教人忽略门禁）：行号在界内；围栏代码块里的同样的字符串；
  `CHANGELOG.md`（历史日志——里面的行号是当天的）；第三方源码路径（`mutmut/…`）；
  命令行里夹一半的引用；**双反引号包裹的写法**（要显示一个字面反引号时的正解）。

两条「真值」用例（对着真仓库跑）：`test_真仓库现在零缺陷` 与
`test_扫描集等于另一条独立argv给的md集`。前者保证交付面上的文件本身干净；后者用**另一条
argv**（`git ls-files --cached -z` 后自己过滤后缀）当 oracle，避免「两侧都调同一个辅助函数、
于是辅助函数漏文件时两侧一起变小、断言恒真」——第七十四批在别处踩过这个坑。
"""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "check_doc_claims.py"

# 判据里提到的两条规则名（与守卫里的常量逐字一致；改了守卫这里也要改）。
_RULE_ESCAPED_TICK = "行内代码里的反斜杠转义反引号"
_RULE_REF_RANGE = "路径:行号 引用越界"
_RULE_REF_MISSING = "路径:行号 引用的文件不存在"


def _load_guard() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_doc_claims", GUARD)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # 先登记进 sys.modules：守卫里有 `@dataclass(frozen=True)`，而 dataclasses 在
    # `from __future__ import annotations` 下要回查 `sys.modules[cls.__module__]` 解析注解，
    # 不登记就是 `AttributeError: 'NoneType' object has no attribute '__dict__'`。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_GUARD = _load_guard()

# 30 行的目标文件：所有行号判据都围着它转。
_SOURCE_30 = "".join(f"line {index}\n" for index in range(1, 31))


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


def _scan(root: Path, docs: list[str]) -> list:
    findings, _refs, _files = _GUARD.scan_docs(docs, root)
    return findings


def _rules(root: Path, docs: list[str]) -> list[str]:
    return sorted({finding.rule for finding in _scan(root, docs)})


def test_查不动的提示在cp936控制台上也不乱码() -> None:
    """`_warn` 走 stderr 而 main() 只 reconfigure 了 stdout ⇒ 不修的话「查不动」的原因就是乱码。

    在 cp936 严格模式下，`✗`/`「」` 这些字符写进去会直接 UnicodeEncodeError，
    这里用同样的编码造一个假 stderr，断言写完能按 utf-8 读回中文。
    """
    buffer = io.BytesIO()
    fake = io.TextIOWrapper(buffer, encoding="cp936", errors="strict")
    original = sys.stderr
    sys.stderr = fake
    try:
        _GUARD._warn("不是「没问题」，是「查不动」：✗")
        fake.flush()
    finally:
        sys.stderr = original
    assert "查不动" in buffer.getvalue().decode("utf-8")


def test_行号越界必须红(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"src/a.py": _SOURCE_30, "docs/x.md": "| 证据 | `src/a.py:31` |\n"})
    assert _rules(root, ["docs/x.md"]) == [_RULE_REF_RANGE]


def test_行号在界内不报(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"src/a.py": _SOURCE_30, "docs/x.md": "| 证据 | `src/a.py:30` |\n"})
    assert _scan(root, ["docs/x.md"]) == []


def test_区间也逐端检查(tmp_path: Path) -> None:
    files = {"src/a.py": _SOURCE_30}
    bad = _repo(tmp_path / "bad", {**files, "docs/x.md": "| 证据 | `src/a.py:28-31` |\n"})
    assert _rules(bad, ["docs/x.md"]) == [_RULE_REF_RANGE]
    good = _repo(tmp_path / "good", {**files, "docs/x.md": "| 证据 | `src/a.py:25-30` |\n"})
    assert _scan(good, ["docs/x.md"]) == []


def test_围栏里的引用不报(tmp_path: Path) -> None:
    """围栏里是「给别人抄的脚本原文」，属于别的判据（pinned_images），不是这里的。"""
    text = "示例：\n\n```bash\ncat src/a.py:9999\n```\n\n正文里也没有引用。\n"
    root = _repo(tmp_path, {"src/a.py": _SOURCE_30, "docs/x.md": text})
    assert _scan(root, ["docs/x.md"]) == []


def test_历史日志里越界的引用不报(tmp_path: Path) -> None:
    """CHANGELOG 里的行号是当天的：拿今天的文件去比它等于要求改假。"""
    root = _repo(
        tmp_path, {"src/a.py": _SOURCE_30, "CHANGELOG.md": "当时改的是 `src/a.py:9999`。\n"}
    )
    assert _scan(root, ["CHANGELOG.md"]) == []


def test_裸的反斜杠反引号必须红(tmp_path: Path) -> None:
    """作者以为反斜杠能转义反引号，实际是 span 被提前关掉、反斜杠原样渲染。"""
    root = _repo(tmp_path, {"docs/x.md": "它得区分注释行、`\\`` 转义这两种写法。\n"})
    assert _rules(root, ["docs/x.md"]) == [_RULE_ESCAPED_TICK]


def test_双反引号包裹是正解不报(tmp_path: Path) -> None:
    """要显示一个字面反引号，正解是双反引号包裹——守卫不能把正解也报红。"""
    root = _repo(tmp_path, {"docs/x.md": "它得区分注释行、`` \\` `` 转义这两种写法。\n"})
    assert _scan(root, ["docs/x.md"]) == []


def test_围栏里的反斜杠反引号不报(tmp_path: Path) -> None:
    text = "示例（围栏里是原文）：\n\n```python\nx = '`\\`'\n```\n"
    root = _repo(tmp_path, {"docs/x.md": text})
    assert _scan(root, ["docs/x.md"]) == []


def test_第三方源码路径不在判据范围(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"docs/x.md": "上游实现在 `mutmut/mutation/core.py:99999`。\n"})
    assert _scan(root, ["docs/x.md"]) == []


def test_引用的文件不存在必须红(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"docs/x.md": "守卫在 `scripts/nope.py:1` 里。\n"})
    assert _rules(root, ["docs/x.md"]) == [_RULE_REF_MISSING]


def test_包根短写会解析到包目录下(tmp_path: Path) -> None:
    """本仓文档混用两种写法：仓根相对的与相对 `src/warden_agent/` 的短写，两种都要认。"""
    files = {"src/warden_agent/web/run_server.py": _SOURCE_30}
    bad = _repo(tmp_path / "bad", {**files, "docs/x.md": "入口在 `web/run_server.py:31`。\n"})
    assert _rules(bad, ["docs/x.md"]) == [_RULE_REF_RANGE]
    good = _repo(tmp_path / "good", {**files, "docs/x.md": "入口在 `web/run_server.py:30`。\n"})
    assert _scan(good, ["docs/x.md"]) == []


def test_命令行里夹一半的引用不算(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"docs/x.md": "跑 `python scripts/a.py:999 --flag` 看看。\n"})
    assert _scan(root, ["docs/x.md"]) == []


def test_真仓库现在零缺陷() -> None:
    """真值用例：交付面上的 Markdown 本身是干净的，且有足够多的引用真的被检查过。

    下面两个「地板」（引用数、正解出现次数）是故意加的下限：判据放宽或扫描集缩小到空集时，
    「零缺陷」会变成假绿——第七十三、七十四批都吃过这个亏。
    """
    docs = _GUARD._tracked_docs()
    findings, checked_refs, read_files = _GUARD.scan_docs(docs, ROOT)
    assert findings == [], [f"{f.file}:{f.line} {f.rule}" for f in findings]
    assert read_files == len(docs)
    assert checked_refs >= 40, f"检查过的引用只有 {checked_refs} 处，判据可能被放宽了"
    correct_form = "`` \\` ``"
    occurrences = sum(
        (ROOT / rel).read_text(encoding="utf-8").count(correct_form) for rel in docs
    )
    assert occurrences >= 2, "文档里应当有双反引号包裹的正解示例，否则这条判据没人示范"


def test_扫描集等于另一条独立argv给的md集() -> None:
    """独立 oracle：另一条 argv（`--cached -z`）后自己过滤后缀，而不是再调一次同一个函数。"""
    proc = subprocess.run(
        ["git", "-c", "core.quotePath=false", "ls-files", "--cached", "-z"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    independent = sorted(name for name in proc.stdout.split("\0") if name.endswith(".md"))
    assert len(independent) >= 8, f"独立 argv 只看到 {len(independent)} 个 Markdown"
    assert _GUARD._tracked_docs() == independent


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [("clean", 0), ("defect", 1), ("not_a_repo", 2)],
)
def test_退出码把三种情况分开(tmp_path: Path, scenario: str, expected: int) -> None:
    """「查不动」必须与「没问题」分开：守卫失效时不能是一个绿灯（退出码 2 而不是 0）。"""
    if scenario == "not_a_repo":
        target = tmp_path
    else:
        target = _repo(tmp_path, {"docs/x.md": "# 干净\n"})
        assert (
            subprocess.run(["git", "init", "-q"], cwd=target, check=False).returncode == 0
        ), "临时仓库初始化失败"
        if scenario == "defect":
            (target / "docs" / "x.md").write_text(
                "写着 `\\`` 这种转义。\n", encoding="utf-8"
            )
        subprocess.run(["git", "add", "-A"], cwd=target, check=True)

    proc = subprocess.run(
        [sys.executable, str(GUARD), "--root", str(target)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert proc.returncode == expected, proc.stdout + proc.stderr
