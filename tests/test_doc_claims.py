"""交付文档里的「反引号转义」「路径:行号」与「符号定义在它引用的文件里」守卫（第七十六、七十七批）。

为什么要有这条守卫、以及它守的两个真实失败模式（GFM 在行内代码里不认反斜杠转义；
文档点名的行号已经越界）写在 `scripts/check_doc_claims.py` 的模块文档里。这里只钉判据：

- **必须红**：`路径:行号` 超出目标文件行数；引用一个不在提交里的文件；正文里出现裸的
  「反斜杠 + 反引号」写法；写了「`符号` 定义在 `某.py`」而那个文件里没有定义它——包括
  **只被 import 过**的情形（文本搜索会放过这种假坐标，ast 不会；第七十七批）。
- **必须不红**（假红比漏检更坏，它教人忽略门禁）：行号在界内；围栏代码块里的同样的字符串；
  `CHANGELOG.md`（历史日志——里面的行号是当天的）；第三方源码路径（`mutmut/…`）；
  命令行里夹一半的引用；**双反引号包裹的写法**（要显示一个字面反引号时的正解）；
  符号与路径只是碰巧挨着、句子并没写「定义在」这类谓语；两个 span 隔得太远。

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
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "check_doc_claims.py"

# 判据里提到的两条规则名（与守卫里的常量逐字一致；改了守卫这里也要改）。
_RULE_ESCAPED_TICK = "行内代码里的反斜杠转义反引号"
_RULE_REF_RANGE = "路径:行号 引用越界"
_RULE_REF_MISSING = "路径:行号 引用的文件不存在"
_RULE_SYMBOL_NOT_DEFINED = "点名的符号不定义在被引用的文件里"

# 判据三的谓语清单：改了守卫里的 `_DEFINITION_MARKERS` 就要同步这里（有专门的用例钉住）。
_MARKERS = ["定义在", "定义于", "实现在", "住在", "位于"]


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


def _scan(root: Path, docs: list[str]) -> list[Any]:
    findings, _refs, _files, _pairs = _GUARD.scan_docs(docs, root)
    # 守卫是动态加载的模块对象，它的返回值在 mypy 眼里是 Any；这里收回具体类型，
    # 好让下面的断言还能被检查（CI 只跑 `mypy src/warden_agent`，这里是自己给自己加的尺子）。
    return list(findings)


def _pairs(root: Path, docs: list[str]) -> int:
    """核对过的「定义在」配对数——它为 0 时「零缺陷」不证明任何事。"""
    _findings, _refs, _files, pairs = _GUARD.scan_docs(docs, root)
    return int(pairs)


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


def test_短写范围由包目录派生不是手写清单(tmp_path: Path) -> None:
    """相对包根的短写（`store/base.py`）也算本仓坐标——第一版手写清单里漏了 `store/`。

    这条用例同时钉住**配对数**：范围漏掉时的症状不是「报错」而是「什么都不报」，
    「缺陷 0」与「核对配对 0」在输出里长得一模一样（第七十七批自查抓到）。
    """
    root = _repo(
        tmp_path,
        {
            "src/warden_agent/store/__init__.py": "",
            "src/warden_agent/store/base.py": "class RunStore:\n    pass\n",
            "docs/x.md": "持久化靠 `RunStore` 定义在 `store/base.py`。\n",
        },
    )
    assert _scan(root, ["docs/x.md"]) == []
    assert _pairs(root, ["docs/x.md"]) == 1


def test_短写指错了文件必须红(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "src/warden_agent/store/__init__.py": "",
            "src/warden_agent/store/base.py": "def other() -> None:\n    return None\n",
            "src/warden_agent/store/postgres.py": "class RunStore:\n    pass\n",
            "docs/x.md": "持久化靠 `RunStore` 定义在 `store/base.py`。\n",
        },
    )
    assert _rules(root, ["docs/x.md"]) == [_RULE_SYMBOL_NOT_DEFINED]


def test_命令行里夹一半的引用不算(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"docs/x.md": "跑 `python scripts/a.py:999 --flag` 看看。\n"})
    assert _scan(root, ["docs/x.md"]) == []


def test_符号定义在别的文件必须红(tmp_path: Path) -> None:
    """第七十七批普查抓到的那类漂移：文档写的是「谁定义在哪」，此前只核过行号在界内。"""
    root = _repo(
        tmp_path,
        {
            "src/warden_agent/web/foo.py": "def other() -> None:\n    return None\n",
            "src/warden_agent/web/bar.py": "class SessionRegistry:\n    pass\n",
            "docs/x.md": "会话缓存 `SessionRegistry` 定义在 `web/foo.py`。\n",
        },
    )
    assert _rules(root, ["docs/x.md"]) == [_RULE_SYMBOL_NOT_DEFINED]


def test_只被import过不算定义(tmp_path: Path) -> None:
    """这是本条判据的存在理由：`"SessionRegistry" in source` 为真，而它没被定义。"""
    root = _repo(
        tmp_path,
        {
            "src/warden_agent/web/foo.py": (
                "from .session_registry import SessionRegistry\n"
                "\n\n"
                "def get() -> object:\n"
                "    return SessionRegistry()\n"
            ),
            "src/warden_agent/web/session_registry.py": "class SessionRegistry:\n    pass\n",
            "docs/x.md": "会话缓存 `SessionRegistry` 定义在 `web/foo.py`。\n",
        },
    )
    assert _rules(root, ["docs/x.md"]) == [_RULE_SYMBOL_NOT_DEFINED]


def test_定义对了不报且配对数不是零(tmp_path: Path) -> None:
    """配对数是判据三的「地板」：它为 0 时正解用例与空扫描长得一模一样。"""
    root = _repo(
        tmp_path,
        {
            "src/warden_agent/web/session_registry.py": "class SessionRegistry:\n    pass\n",
            "docs/x.md": "会话缓存 `SessionRegistry` 定义在 `web/session_registry.py`。\n",
        },
    )
    assert _scan(root, ["docs/x.md"]) == []
    assert _pairs(root, ["docs/x.md"]) == 1


def test_带成员的写法只看第一段(tmp_path: Path) -> None:
    """文档会写 `SessionRegistry._sessions`；定义的是一个类，成员名不必自己是个定义。"""
    root = _repo(
        tmp_path,
        {
            "src/warden_agent/web/session_registry.py": (
                "class SessionRegistry:\n"
                "    def __init__(self) -> None:\n"
                "        self._sessions: dict[str, int] = {}\n"
            ),
            "docs/x.md": "`SessionRegistry._sessions` 定义在 `web/session_registry.py`。\n",
        },
    )
    assert _scan(root, ["docs/x.md"]) == []


def test_带注解的成员单独点名也算定义(tmp_path: Path) -> None:
    """文档也可能只点成员名（`_sessions`）而不带类名。

    这时定义它的那行是**带注解的赋值**（`self._sessions: dict[...] = {}`）。
    守卫里 AnnAssign 那条路漏了，「成员单独点名」的文档就会被误报——
    变异矩阵 M2 盯的就是这里。
    """
    root = _repo(
        tmp_path,
        {
            "src/warden_agent/web/session_registry.py": (
                "class SessionRegistry:\n"
                "    def __init__(self) -> None:\n"
                "        self._sessions: dict[str, int] = {}\n"
            ),
            "docs/x.md": "缓存 `_sessions` 定义在 `web/session_registry.py`。\n",
        },
    )
    assert _scan(root, ["docs/x.md"]) == []
    assert _pairs(root, ["docs/x.md"]) == 1


def test_类名不对时必须红(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "src/warden_agent/web/session_registry.py": "class Other:\n    pass\n",
            "docs/x.md": "`SessionRegistry._sessions` 定义在 `web/session_registry.py`。\n",
        },
    )
    assert _rules(root, ["docs/x.md"]) == [_RULE_SYMBOL_NOT_DEFINED]


def test_没写谓语不算主张(tmp_path: Path) -> None:
    """共现与邻接不是主张：把它们当主张会报出一堆 `Dockerfile` 这类假阳（实测 681 处）。"""
    root = _repo(
        tmp_path,
        {
            "src/warden_agent/web/foo.py": "class Something:\n    pass\n",
            "docs/x.md": "看 `Something`（另有 `web/foo.py`）。\n",
        },
    )
    assert _scan(root, ["docs/x.md"]) == []
    assert _pairs(root, ["docs/x.md"]) == 0


def test_两个span隔太远不成对(tmp_path: Path) -> None:
    """隔得远的两段是两句话；这里的目标文件**确实是定义处**，所以只有配对数能证伪。"""
    root = _repo(
        tmp_path,
        {
            "src/warden_agent/web/foo.py": "class Something:\n    pass\n",
            "docs/x.md": "`Something` 定义在（这段是后来补的说明，早已过时）`web/foo.py`。\n",
        },
    )
    assert _scan(root, ["docs/x.md"]) == []
    assert _pairs(root, ["docs/x.md"]) == 0


def test_谓语的几种写法都认(tmp_path: Path) -> None:
    """五种谓语里任一种都算主张；只认一种就等于把其余写法上的漂移全放过。

    写成一圈循环而不是 `parametrize`：参数值是中文时 pytest 会把节点 id 转义成
    `[\\u5b9a\\u4e49\\u5728]` 这种 ASCII 形式，变异矩阵里「指名该红哪一条」就得跟着转义。
    """
    assert sorted(_MARKERS) == sorted(_GUARD._DEFINITION_MARKERS), "守卫加了新谓语这里要同步"
    for marker in _MARKERS:
        root = _repo(
            tmp_path / marker,
            {
                "src/warden_agent/web/foo.py": "def other() -> None:\n    return None\n",
                "docs/x.md": f"`SessionRegistry` {marker} `web/foo.py`。\n",
            },
        )
        assert _rules(root, ["docs/x.md"]) == [_RULE_SYMBOL_NOT_DEFINED], marker


def test_小写工具名不当符号(tmp_path: Path) -> None:
    """`ruff`/`pytest`/`git` 这类小写工具名就住在同名的项目里，硬判就是噪声。"""
    root = _repo(
        tmp_path,
        {
            "src/warden_agent/web/foo.py": "class Something:\n    pass\n",
            "docs/x.md": "`ruff` 定义在 `web/foo.py`。\n",
        },
    )
    assert _scan(root, ["docs/x.md"]) == []
    assert _pairs(root, ["docs/x.md"]) == 0


def test_非py路径不成对(tmp_path: Path) -> None:
    """`定义在` 后面跟的不是仓内 .py 时无从判：配置文件、目录、别人的插件都行。

    目标文件在这里是**存在的**，否则「不判」可能是因为解析不到文件——那样用例就没齿了。
    """
    root = _repo(
        tmp_path,
        {
            "src/warden_agent/web/foo.yaml": "a: 1\n",
            "docs/x.md": "`SessionRegistry` 定义在 `web/foo.yaml`。\n",
        },
    )
    assert _pairs(root, ["docs/x.md"]) == 0


def test_文件名本身就是定义(tmp_path: Path) -> None:
    """文档会写「`check_md_tables` 定义在 `scripts/check_md_tables.py`」——文件自己的名字算。"""
    root = _repo(
        tmp_path,
        {
            "scripts/check_md_tables.py": "x = 1\n",
            "docs/x.md": "`check_md_tables` 定义在 `scripts/check_md_tables.py`。\n",
        },
    )
    assert _scan(root, ["docs/x.md"]) == []


def test_语法错的文件不抛异常(tmp_path: Path) -> None:
    """解析不了的文件另有 ruff/mypy 兜着；这里只要求它表现为「名字找不到」而不是 traceback。"""
    root = _repo(
        tmp_path,
        {
            "src/warden_agent/web/foo.py": "def broken(:\n",
            "docs/x.md": "`SessionRegistry` 定义在 `web/foo.py`。\n",
        },
    )
    assert _rules(root, ["docs/x.md"]) == [_RULE_SYMBOL_NOT_DEFINED]


def test_短写路径也认(tmp_path: Path) -> None:
    """与判据一同一套解析：`web/…` 是相对 `src/warden_agent/` 的短写，也要能解析到。"""
    files = {
        "src/warden_agent/web/session_registry.py": "class SessionRegistry:\n    pass\n",
        "src/warden_agent/web/other.py": "class Other:\n    pass\n",
    }
    good = _repo(
        tmp_path / "good",
        {**files, "docs/x.md": "`SessionRegistry` 定义在 `web/session_registry.py`。\n"},
    )
    assert _scan(good, ["docs/x.md"]) == []
    # 反例必须报：若短写解析不到文件，这条会被静默跳过、缺陷消失——所以这里断言的是规则。
    bad = _repo(
        tmp_path / "bad",
        {**files, "docs/x.md": "`SessionRegistry` 定义在 `web/other.py`。\n"},
    )
    assert _rules(bad, ["docs/x.md"]) == [_RULE_SYMBOL_NOT_DEFINED]


def test_定义行号取自起始行(tmp_path: Path) -> None:
    """判据三只看「有没有定义」，行号是留给人看的；这里钉住取的是定义那一行的行号。"""
    path = tmp_path / "web" / "m.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "import os\n"
        "\n"
        "\n"
        "class A:\n"
        "    def method(self) -> None:\n"
        "        self.attr = 1\n"
        "\n"
        "\n"
        "CONST = 2\n",
        encoding="utf-8",
    )
    names = _GUARD._defined_names(path)
    assert names["A"] == 4
    assert names["method"] == 5
    assert names["attr"] == 6
    assert names["CONST"] == 9
    assert names["m"] == 1, "文件自己的名字要算，否则 sphinx 风格的自我引用会被误报"


def test_真仓库现在零缺陷() -> None:
    """真值用例：交付面上的 Markdown 本身是干净的，且有足够多的引用真的被检查过。

    下面三个「地板」（引用数、核对过的「定义在」配对数、正解出现次数）是故意加的下限：
    判据放宽或扫描集缩小到空集时，「零缺陷」会变成假绿——第七十三、七十四批都吃过这个亏。
    """
    docs = _GUARD._tracked_docs()
    findings, checked_refs, read_files, checked_pairs = _GUARD.scan_docs(docs, ROOT)
    assert findings == [], [f"{f.file}:{f.line} {f.rule}" for f in findings]
    assert read_files == len(docs)
    assert checked_refs >= 40, f"检查过的引用只有 {checked_refs} 处，判据可能被放宽了"
    assert checked_pairs >= 5, (
        f"核对过的「定义在」配对只有 {checked_pairs} 处：文档里写出的定义主张被删光了，"
        "或者判据三自己不吭声了"
    )
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
