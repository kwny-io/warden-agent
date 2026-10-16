"""成本断言必须能被重放：写「N 次 git spawn」「本文件 N 条用例」，就得同段指名量它的尺子。

第 86 批发现的病：同一件事有三套数字，量级差 25 倍，而且谁都没引用过量它的工具——

  · `tests/test_delivery_ledger.py` 的批量取证注释：每条用例 94 次 spawn、43 条用例 ≈ 4000 次；
  · `scripts/mutant_check.py` 的 docstring：全套 52 条「40 分钟上下」（重构后实测已是 473s）；
  · `CHANGELOG.md` 第八十四批：整文件 4.5 分钟。

而 `tests/test_doc_claims.py` 只管**引用**（行号越界、符号定义在别的文件、裸反引号），对**数字
零判据**——所以数字一旦写下就再也不会被核对。同一小时里我自己也犯过一次同类错（把旧树说成
「44 条用例里 1 条红」，实际那棵树是 35 条），印证了「记忆与估算都不是证据」。

两条判据（都对着真仓库跑，另有反面对照证明它们会红）：

  1. 出现 git spawn 次数断言的段落，同段必须出现 `scripts/count_git_spawn.py`；
  2. 出现「本文件 N 条用例」的段落，N 必须等于这条文件里 `def test_` 的实际条数——除非同段有
     7~40 位十六进制树锚（那这条是有日期的观测记录，不是当下的事实）。

CHANGELOG 只查**最新小节**：旧小节的数字是当天的观测记录，与 `tests/test_doc_claims.py` 对历史
行号的豁免同一个理由。新增成本断言时，把落点加进 `_TARGETS`，并在这里补一条反面对照。

尺子自己也得有牙（第八十六批补）：上面两条判据只核「有没有指名尺子」，尺子失聪时却无人发现——
所以末尾两条「尺子自己会量」的用例拿真目标反向对照（有 git 的目标必须量出非零、不碰 git 的目标
必须量出零）。变异登记见 `scripts/mutant_check.py` 的「成本断言的尺子与判据」一节。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

# 量 git spawn 次数的那把尺子（成本断言必须指名它）
_RULER = "scripts/count_git_spawn.py"

# 成本断言住在哪儿（新增落点：加进来 + 补反面对照）
_TARGETS = (
    "tests/test_delivery_ledger.py",
    "scripts/mutant_check.py",
    "CHANGELOG.md",
)

_SPAWN_CLAIM = re.compile(r"\d+\s*次")
# `\bgit\b`：`git` 不带词界会把 `GitHub` 也算进去——「GitHub 上重跑了 2 次」这种段落会被误判成
# 成本断言（第八十六批现场：形状守卫的红来自 GitHub 的 push 事件语义，与 git 子进程成本无关）。
_SPAWN_WORD = re.compile(r"spawn|\bgit\b", re.IGNORECASE)
_CASE_CLAIM = re.compile(r"(\d+)\s*条用例")
_TREE_ANCHOR = re.compile(r"\b[0-9a-f]{7,40}\b")

# 「尺子自己会量」用的真目标：一个必然拉起 git（rev-parse / merge-base）的用例，
# 与一个纯数据、不碰 git 的用例。二者配对才能证明尺子不是在瞎报数。
_RULER_TARGET = "tests/test_ci_evidence.py::test_切点在本仓历史里"
_NO_GIT_TARGET = "tests/test_mutation_shards.py::test_分片名唯一且默认分片存在"


def _paragraphs(text: str) -> list[str]:
    """按空行切段；注释块里单独一行的 `#` 也算分段边界。"""
    return [p for p in re.split(r"\n\s*(?:#\s*)?\n", text) if p.strip()]


def _newest_changelog_section(text: str) -> str:
    """CHANGELOG 只看最新小节：第一个 `## ` 到第二个 `## ` 之间。"""
    headers = [m.start() for m in re.finditer(r"(?m)^## ", text)]
    return text[headers[0] : headers[1]] if len(headers) >= 2 else text


def _scanned(root: Path) -> list[tuple[str, list[str]]]:
    out: list[tuple[str, list[str]]] = []
    for rel in _TARGETS:
        text = (root / rel).read_text(encoding="utf-8")
        if rel == "CHANGELOG.md":
            text = _newest_changelog_section(text)
        out.append((rel, _paragraphs(text)))
    return out


def _counted_tests(root: Path, rel: str) -> int:
    return len(re.findall(r"(?m)^def test_", (root / rel).read_text(encoding="utf-8")))


def _one_line(para: str) -> str:
    return re.sub(r"\s+", " ", para.strip())[:120]


def _violations(root: Path) -> list[str]:
    """返回「成本断言不可重放」的清单：空 = 干净。"""
    out: list[str] = []
    for rel, paras in _scanned(root):
        for para in paras:
            if _SPAWN_WORD.search(para) and _SPAWN_CLAIM.search(para) and _RULER not in para:
                out.append(f"{rel}: spawn 次数没同段指名尺子 `{_RULER}`：{_one_line(para)}")
            if "本文件" in para and not _TREE_ANCHOR.search(para):
                for claim in _CASE_CLAIM.findall(para):
                    actual = _counted_tests(root, rel) if rel.startswith("tests/") else None
                    if actual is not None and int(claim) != actual:
                        detail = _one_line(para)
                        out.append(
                            f"{rel}: 说「{claim} 条用例」，实测 {actual} 条：{detail}"
                        )
    return out


def _hits(root: Path) -> tuple[int, int]:
    """两类断言各命中几次（用来钉住「判据不是空转」）。"""
    spawn_hits = case_hits = 0
    for _rel, paras in _scanned(root):
        for para in paras:
            if _SPAWN_WORD.search(para) and _SPAWN_CLAIM.search(para):
                spawn_hits += 1
            if "本文件" in para and _CASE_CLAIM.search(para):
                case_hits += 1
    return spawn_hits, case_hits


def _fake(root: Path, ledger_cost: str) -> Path:
    """造一棵最小假仓库：两个扫描目标 + 尺子本体（判据只看引用，不看尺子内容）。"""
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / _RULER).write_text("", encoding="utf-8")
    (root / "scripts/mutant_check.py").write_text(
        f"成本（尺子 `{_RULER}`）：全套 57 条 473s。\n", encoding="utf-8"
    )
    (root / "CHANGELOG.md").write_text(
        f"## 最新批\n\n成本见 `{_RULER}`：416 次 git spawn。\n\n## 更早的批\n\n没数字。\n",
        encoding="utf-8",
    )
    body = "def test_甲() -> None:\n    pass\n\n\n" + ledger_cost
    (root / "tests/test_delivery_ledger.py").write_text(body, encoding="utf-8")
    return root


def test_成本数字必须同段指名尺子() -> None:
    assert _violations(_ROOT) == []


def test_判据不是空转_两类断言在真仓库里都命中() -> None:
    """成本断言被搬走或换写法时这条会红——提醒「判据已经对着空气跑」。"""
    spawn_hits, case_hits = _hits(_ROOT)
    assert spawn_hits >= 1, "仓库里已经没有 spawn 次数断言了？_TARGETS 或判据得重瞄"
    assert case_hits >= 1, "仓库里已经没有「本文件 N 条用例」断言了？判据得重瞄"


def test_把尺子引用删掉会被抓(tmp_path: Path) -> None:
    """正对照：同一段话，只是不再指名尺子 ⇒ 必须红。"""
    root = _fake(tmp_path, "本文件 1 条用例，每条用例 spawn 94 次 git。\n")
    assert any("count_git_spawn" in item for item in _violations(root)), _violations(root)


def test_条数写错会被抓(tmp_path: Path) -> None:
    """正对照：给了尺子但条数是估的（假树里实际 1 条）⇒ 必须红。"""
    root = _fake(tmp_path, f"本文件 35 条用例（尺子 `{_RULER}`，spawn 94 次 git）。\n")
    assert any("条用例" in item for item in _violations(root)), _violations(root)


def test_有树锚的历史数字不算当下事实(tmp_path: Path) -> None:
    """反面对照：「重构前（树 5c0faa5）本文件 35 条用例」是观测记录，文件长到 44 条也不该红。"""
    root = _fake(
        tmp_path,
        f"重构前（树 5c0faa5）本文件 35 条用例、整文件 2676 次 git spawn（尺子 `{_RULER}`）。\n",
    )
    assert _violations(root) == []


def _run_ruler(target: str) -> int:
    """在本树上跑尺子，取它自报的 git 调用次数。

    子进程的编码必须显式钉成 UTF-8：尺子打的是中文，而 `text=True, encoding="utf-8"`
    只钉了**父进程怎么解**。本机 locale 是 cp936 时，子进程按 cp936 写出、按 utf-8 解开
    ⇒ 计数行变乱码、正则匹配不到 —— 这条判据曾因此在「外面没设 PYTHONUTF8」的环境里红。
    """
    r = subprocess.run(
        [sys.executable, str(_ROOT / _RULER), target],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        # 不许依赖调用者环境：子进程也必须按 UTF-8 写，与上面的解码成对
        env=dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8"),
    )
    m = re.search(r"git 子进程真实调用次数：(\d+)", r.stdout or "")
    assert m, (
        f"尺子没打出计数行（rc={r.returncode}）："
        f"{(r.stdout or '')[-400:]}{(r.stderr or '')[-400:]}"
    )
    return int(m.group(1))


def test_尺子自己会量_有_git_的目标必须量出非零() -> None:
    """尺子失聪时（计数点被拆掉、Popen 挂钩失效）这条必须红——否则「N 次」又回到没人核的状态。

    目标选 `tests/test_ci_evidence.py::test_切点在本仓历史里`：它自己会 rev-parse / merge-base。
    浅检出里那条用例会红，但尺子的契约是「测试红也算量到了」⇒ 这里只钉计数，不钉它的红绿。
    """
    assert _run_ruler(_RULER_TARGET) >= 1


def test_尺子自己会量_没有_git_的目标必须量出零() -> None:
    """反面对照：不碰 git 的用例量出来必须是 0——证明它不是「每次都报一堆」。"""
    assert _run_ruler(_NO_GIT_TARGET) == 0
