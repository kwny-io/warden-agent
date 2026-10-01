"""Unified diff 解析器与应用器的契约测试（功能 + 预算 + 边界）。

为什么值得单独一个文件：`git.apply_patch` 是这个仓库里**唯一会按不可信输入改写工作区文件**的
代码路径——diff 来自模型生成，而模型又可能被 `web.fetch` 抓回来的网页正文牵着走。
它的每一条边界（hunk 起点、行计数、换行归一化、预算、hash 前置校验）都直接对应
"补丁会不会写到不该写的地方、会不会改错行"。所以这里不测"能不能解析"，而是逐条钉死契约：

  - 解析：文件头三种形态（`--- a/`、`--- /dev/null`、单独 `+++ b/`）、hunk 头计数省略、
    多文件块之间没有空行、CRLF/裸 CR 归一化、`\\ No newline` 标记行忽略；
  - 预算：字节 / 文件数 / 行数三个上限，**恰好等于上限必须通过**（> 而不是 >=）；
  - 应用：乱序 hunk 按 old 起点排序、删除行只前进一步、上下文行在文件末尾处按文本补齐、
    未涉及的行与缩进原样保留、hash 不符时拒绝且不改文件；
  - 失败面：冲突要带 `path`，越界要报"越界"而不是抛 IndexError。

这一组是 2026-09-30 的 patch-guard 变异分片（当时 291 个突变体里存活 80 个）补出来的。
"""

from __future__ import annotations

import hashlib
import inspect
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from warden_agent.git.patch import (
    FilePatch,
    PatchApplier,
    PatchConflict,
    PatchDocument,
    PatchHunk,
    PatchLine,
    UnifiedPatchParser,
)

ADD = "--- a/{p}\n+++ b/{p}\n@@ -0,0 +1 @@\n+new\n"


def _write(path: Path, text: str) -> None:
    """显式写 LF：Windows 上 `Path.write_text` 默认会把 \\n 换成 \\r\\n，
    那会让「缩进/内容逐字节相等」这类断言在本地与 CI 上不一致。"""
    path.write_text(text, encoding="utf-8", newline="\n")


def _two_files() -> str:
    """两个文件块**紧邻**（中间不留空行）——真实 diff 常见，解析器必须能在正确位置收尾。"""
    return (
        "--- a/one.txt\n+++ b/one.txt\n@@ -1,1 +1,1 @@\n-old\n+new\n"
        "--- a/two.txt\n+++ b/two.txt\n@@ -1,1 +1,1 @@\n-old2\n+new2\n"
    )


# ---- 文件头三种形态 ----


def test_文件头a与b前缀都被剥掉() -> None:
    doc = UnifiedPatchParser().parse(ADD.format(p="src/pkg/mod.py"))
    assert len(doc.files) == 1
    assert doc.files[0].old_path == "src/pkg/mod.py"
    assert doc.files[0].target_path == "src/pkg/mod.py"
    assert doc.files[0].deleted is False


def test_新增文件用dev_null写old_path但target是new_path() -> None:
    """`--- /dev/null` + `+++ b/x` = 新增；目标必须是 new_path（否则会去删 /dev/null）。"""
    doc = UnifiedPatchParser().parse("--- /dev/null\n+++ b/fresh.txt\n@@ -0,0 +1 @@\n+hi\n")
    assert doc.files[0].old_path == "/dev/null"
    assert doc.files[0].target_path == "fresh.txt"
    assert doc.files[0].deleted is False


def test_删除文件时target取old_path且标记deleted() -> None:
    doc = UnifiedPatchParser().parse("--- a/gone.txt\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-old\n")
    assert doc.files[0].deleted is True
    assert doc.files[0].target_path == "gone.txt"


def test_a_dev_null这种写法也要识别为dev_null() -> None:
    """git 在带 -p 前缀的输出里会写成 `a/dev/null`——不识别就等于把 /dev/null 当成真路径。"""
    doc = UnifiedPatchParser().parse("--- a/dev/null\n+++ b/fresh.txt\n@@ -0,0 +1 @@\n+hi\n")
    assert doc.files[0].old_path == "/dev/null"
    assert doc.files[0].target_path == "fresh.txt"


def test_只有加号文件头时也能解析出文件() -> None:
    """没有 `---` 行、只有 `+++ b/x`：也要成一个文件块（而不是整份 diff 都丢掉）。"""
    doc = UnifiedPatchParser().parse("+++ b/only_new.txt\n@@ -0,0 +1 @@\n+hi\n")
    assert [f.target_path for f in doc.files] == ["only_new.txt"]


# ---- hunk 头 ----


def test_hunk头计数省略时按1处理显式时按显式处理() -> None:
    doc = UnifiedPatchParser().parse(
        "--- a/x\n+++ b/x\n@@ -3,2 +9,4 @@\n-a\n-b\n+c\n"
    )
    h = doc.files[0].hunks[0]
    assert (h.old_start, h.old_count, h.new_start, h.new_count) == (3, 2, 9, 4)

    doc2 = UnifiedPatchParser().parse("--- a/x\n+++ b/x\n@@ -5 +7 @@\n-a\n+b\n")
    h2 = doc2.files[0].hunks[0]
    assert (h2.old_start, h2.old_count, h2.new_start, h2.new_count) == (5, 1, 7, 1)


def test_行长标记行被忽略() -> None:
    """`\\ No newline at end of file` 不是内容行：混进 hunk 会多出一行幽灵改动。"""
    doc = UnifiedPatchParser().parse(
        "--- a/x\n+++ b/x\n@@ -1,1 +1,1 @@\n-old\n\\ No newline at end of file\n+new\n"
    )
    lines = doc.files[0].hunks[0].lines
    assert [(ln.kind, ln.text) for ln in lines] == [("-", "old"), ("+", "new")]


def test_两个文件块紧邻也能正确切分() -> None:
    doc = UnifiedPatchParser().parse(_two_files())
    assert [f.target_path for f in doc.files] == ["one.txt", "two.txt"]
    # 第一个 hunk 的行不能把第二个文件的文件头（以 `-` 开头）吞进来
    assert [ln.text for ln in doc.files[0].hunks[0].lines] == ["old", "new"]


# ---- 换行归一化 ----


def test_CRLF被归一化成LF() -> None:
    doc = UnifiedPatchParser().parse(
        "--- a/x\r\n+++ b/x\r\n@@ -1,1 +1,1 @@\r\n-old\r\n+new\r\n"
    )
    lines = doc.files[0].hunks[0].lines
    assert [(ln.kind, ln.text) for ln in lines] == [("-", "old"), ("+", "new")]
    assert not any("\r" in ln.text for ln in lines)


def test_裸CR也能当行分隔符解析() -> None:
    """只有 `\\r`（老 Mac 换行）的 diff：不归一化就会变成一整行无法识别的内容。"""
    doc = UnifiedPatchParser().parse("--- a/x\r+++ b/x\r@@ -1,1 +1,1 @@\r-old\r+new\r")
    lines = doc.files[0].hunks[0].lines
    assert [(ln.kind, ln.text) for ln in lines] == [("-", "old"), ("+", "new")]


# ---- 预算：三个上限，恰好等于上限必须通过 ----


def test_字节预算恰好等于上限时通过超一字节才拒绝() -> None:
    text = ADD.format(p="x.txt")
    size = len(text.encode("utf-8"))
    assert UnifiedPatchParser(max_bytes=size).parse(text).files
    with pytest.raises(PatchConflict):
        UnifiedPatchParser(max_bytes=size - 1).parse(text)


def test_文件数预算按块数算恰好等于上限通过() -> None:
    text = _two_files()
    assert len(UnifiedPatchParser(max_files=2).parse(text).files) == 2
    assert len(UnifiedPatchParser(max_files=1).parse(ADD.format(p="solo.txt")).files) == 1
    with pytest.raises(PatchConflict):
        UnifiedPatchParser(max_files=1).parse(text)


def test_行数预算恰好等于上限通过超一行拒绝() -> None:
    # 两个内容行（-a / +b）
    text = "--- a/x\n+++ b/x\n@@ -1,1 +1,1 @@\n-a\n+b\n"
    assert UnifiedPatchParser(max_lines=2).parse(text).files
    with pytest.raises(PatchConflict):
        UnifiedPatchParser(max_lines=1).parse(text)


def test_NUL字节被拒绝() -> None:
    with pytest.raises(PatchConflict) as ei:
        UnifiedPatchParser().parse("--- a/x\n+++ b/x\n@@ -1,1 +1,1 @@\n-a\n+b\x00c\n")
    assert "NUL" in ei.value.reason


def test_解析不出任何文件时报错() -> None:
    with pytest.raises(PatchConflict):
        UnifiedPatchParser().parse("这不是 diff\n随便几行\n")


# ---- 应用器 ----


def _apply(diff: str, root: Path) -> list[str]:
    return PatchApplier().apply(UnifiedPatchParser().parse(diff), str(root))


def test_应用补丁保留未涉及的行与缩进和空行(tmp_path: Path) -> None:
    f = tmp_path / "code.py"
    _write(f, "def f():\n    return 1\n\n\nx = 2\n")
    diff = (
        "--- a/code.py\n+++ b/code.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def f():\n"
        "-    return 1\n"
        "+    return 42\n"
    )
    assert _apply(diff, tmp_path) == ["code.py"]
    # 缩进、空行、后面的行都要原样保留（split(None) 那种写法会把缩进吃掉）
    assert f.read_text(encoding="utf-8") == "def f():\n    return 42\n\n\nx = 2\n"


def test_删除行只让游标前进一步(tmp_path: Path) -> None:
    """一个 `-` 行只吃掉旧文件的一行——多跳一步就会静默删掉/错位一行。"""
    f = tmp_path / "t.txt"
    _write(f, "a\nb\nc\nd\n")
    diff = (
        "--- a/t.txt\n+++ b/t.txt\n"
        "@@ -2,3 +2,2 @@\n"
        "-b\n"
        "-c\n"
        "+B\n"
    )
    _apply(diff, tmp_path)
    assert f.read_text(encoding="utf-8") == "a\nB\nd\n"


def test_乱序写出的hunk按old起点排序应用(tmp_path: Path) -> None:
    """hunk 顺序以 old_start 为准（不是文件里的书写顺序），否则第二处改动会错位。"""
    f = tmp_path / "m.txt"
    _write(f, "1\n2\n3\n4\n5\n6\n7\n8\n")
    diff = (
        "--- a/m.txt\n+++ b/m.txt\n"
        "@@ -6,1 +6,1 @@\n-6\n+SIX\n"
        "@@ -2,1 +2,1 @@\n-2\n+TWO\n"
    )
    _apply(diff, tmp_path)
    assert f.read_text(encoding="utf-8") == "1\nTWO\n3\n4\n5\nSIX\n7\n8\n"


def test_hunk起点超出文件末尾报越界而不是炸下标(tmp_path: Path) -> None:
    f = tmp_path / "short.txt"
    _write(f, "only\n")
    diff = "--- a/short.txt\n+++ b/short.txt\n@@ -50,1 +50,1 @@\n-only\n+x\n"
    with pytest.raises(PatchConflict) as ei:
        _apply(diff, tmp_path)
    assert "越界" in ei.value.reason
    assert f.read_text(encoding="utf-8") == "only\n", "报错前不得改动文件"


def test_上下文行超出文件末尾时按文本补齐(tmp_path: Path) -> None:
    """空文件上带上下文的 hunk（git 对新文件有时会这么写）：按 diff 文本补齐，不能炸下标。"""
    f = tmp_path / "empty.txt"
    _write(f, "")
    diff = "--- a/empty.txt\n+++ b/empty.txt\n@@ -1,1 +1,2 @@\n ctx\n+added\n"
    _apply(diff, tmp_path)
    assert f.read_text(encoding="utf-8") == "ctx\nadded"


def test_越界冲突带path且changed只含真正改动的文件(tmp_path: Path) -> None:
    f = tmp_path / "ok.txt"
    _write(f, "a\n")
    doc = UnifiedPatchParser().parse(
        "--- a/ok.txt\n+++ b/ok.txt\n@@ -1,1 +1,1 @@\n-a\n+b\n"
    )
    assert PatchApplier().apply(doc, str(tmp_path)) == ["ok.txt"]

    # 手工构造一个绕过了解析器校验的文档（模拟“别的地方塞进来一份 unchecked 文档”）：
    # 应用期的 resolve 包含校验是第二道防线，必须自己挡住。
    hunk = PatchHunk(0, 0, 1, 1, (PatchLine("+", "pwned"),))
    bad = PatchDocument([FilePatch("../escape.txt", "../escape.txt", (hunk,))])
    with pytest.raises(PatchConflict) as ei:
        PatchApplier().apply(bad, str(tmp_path))
    assert ei.value.path == "../escape.txt"
    assert not (tmp_path.parent / "escape.txt").exists()


def test_删除文件会真的删掉文件并计入changed(tmp_path: Path) -> None:
    f = tmp_path / "gone.txt"
    _write(f, "bye\n")
    diff = "--- a/gone.txt\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-bye\n"
    assert _apply(diff, tmp_path) == ["gone.txt"]
    assert not f.exists()


def test_删除不存在的文件不算改动也不报错(tmp_path: Path) -> None:
    diff = "--- a/nope.txt\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-x\n"
    assert _apply(diff, tmp_path) == []
    assert not (tmp_path / "nope.txt").exists()


def test_预期hash相符才应用不符则拒绝且不动文件(tmp_path: Path) -> None:
    """`expected_hashes` 是“补丁基于的内容”这道前置校验：不符必须拒绝（乐观应用会改错行）。"""
    f = tmp_path / "h.txt"
    _write(f, "base\n")
    diff = "--- a/h.txt\n+++ b/h.txt\n@@ -1,1 +1,1 @@\n-base\n+patched\n"

    good = hashlib.sha256(b"base\n").hexdigest()
    assert PatchApplier().apply(
        UnifiedPatchParser().parse(diff), str(tmp_path), expected_hashes={"h.txt": good}
    ) == ["h.txt"]
    assert f.read_text(encoding="utf-8") == "patched\n"

    _write(f, "base\n")
    with pytest.raises(PatchConflict) as ei:
        PatchApplier().apply(
            UnifiedPatchParser().parse(diff), str(tmp_path),
            expected_hashes={"h.txt": hashlib.sha256(b"other\n").hexdigest()},
        )
    assert "hash" in ei.value.reason
    assert ei.value.path == "h.txt"
    assert f.read_text(encoding="utf-8") == "base\n", "hash 不符时不得写入"


def test_新增文件时会自动建目录(tmp_path: Path) -> None:
    diff = "--- /dev/null\n+++ b/deep/nested/new.txt\n@@ -0,0 +1 @@\n+hi\n"
    assert _apply(diff, tmp_path) == ["deep/nested/new.txt"]
    assert (tmp_path / "deep" / "nested" / "new.txt").read_text(encoding="utf-8") == "hi"


def test_中文内容按utf8往返(tmp_path: Path) -> None:
    """读写都必须显式 UTF-8：交给平台默认编码会在非 UTF-8 机器上把内容写坏。"""
    f = tmp_path / "zh.txt"
    _write(f, "旧值：中文\n")
    diff = "--- a/zh.txt\n+++ b/zh.txt\n@@ -1,1 +1,1 @@\n-旧值：中文\n+新值：中文\n"
    _apply(diff, tmp_path)
    # 读回来的文本必须是中文（不是乱码），且磁盘上的字节确实是 UTF-8
    assert f.read_text(encoding="utf-8") == "新值：中文\n"
    assert f.read_bytes().decode("utf-8").strip() == "新值：中文"


# ---- 变异分片清缺口（2026-10-01）----
# 2026-09-30 的 patch-guard 分片还剩 48 条存活。逐条看下来它们不是“改了没区别”：
#   (1) 拒绝文案被改成 None / 套一层 XX —— 异常类型还对，模型与审计拿到的那句解释没了；
#   (2) 默认预算那三个数字（200 / 20 万 / 5MiB）从没被钉住（只在小预算上验过边界）；
#   (3) 解析循环里的分支（`or`/`and`、`idx` 步进、`+++` 前瞻）只在“正常 diff”上走过。

_SRC_ROOT = str(Path(__file__).resolve().parents[1] / "src")


def test_拒绝文案逐字_模型看到的就是这句话() -> None:
    """异常类型对≠拿到解释。这些 reason 会原样回给模型/写进审计，所以是契约。"""
    cases: list[tuple[Callable[[], object], str]] = [
        (lambda: UnifiedPatchParser(max_bytes=10).parse("x" * 20), "diff 超过字节预算"),
        (lambda: UnifiedPatchParser().parse("a\x00b"), "diff 含 NUL 字节，拒绝"),
        (
            lambda: UnifiedPatchParser(max_files=1).parse(
                ADD.format(p="a") + ADD.format(p="b")
            ),
            "diff 超过文件数预算",
        ),
        (
            lambda: UnifiedPatchParser(max_lines=1).parse(
                "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"
            ),
            "diff 超过行数预算",
        ),
        (lambda: UnifiedPatchParser().parse("没有文件头\n"), "无法解析出任何文件 patch"),
        (lambda: UnifiedPatchParser().parse("--- a/\n"), "diff 里出现空路径"),
    ]
    for call, expected in cases:
        with pytest.raises(PatchConflict) as ei:
            call()
        assert ei.value.reason == expected


def test_默认预算就是文档里那三个数_200文件_20万行_5MiB() -> None:
    defaults = inspect.signature(UnifiedPatchParser.__init__).parameters
    assert defaults["max_files"].default == 200
    assert defaults["max_lines"].default == 200_000
    assert defaults["max_bytes"].default == 5 * 1024 * 1024


def test_默认文件数上限恰好200个通过201个拒绝() -> None:
    """小预算的边界另有用例；这条专门钉住**默认值**：改成 201 就放过了第 201 个文件块。"""
    two_hundred = "".join(ADD.format(p=f"f{i}.txt") for i in range(200))
    assert len(UnifiedPatchParser().parse(two_hundred).files) == 200
    too_many = "".join(ADD.format(p=f"f{i}.txt") for i in range(201))
    with pytest.raises(PatchConflict) as ei:
        UnifiedPatchParser().parse(too_many)
    assert ei.value.reason == "diff 超过文件数预算"


def test_dev_null三种写法都算删除() -> None:
    for raw in ("/dev/null", "a/dev/null", "b/dev/null"):
        doc = UnifiedPatchParser().parse(f"--- a/gone.txt\n+++ {raw}\n")
        assert doc.files[0].deleted is True, raw
        assert doc.files[0].target_path == "gone.txt", raw


def test_只有减号头的块按anchor兜底且不吃掉后面的hunk() -> None:
    doc = UnifiedPatchParser().parse("--- a/x.txt\n@@ -1 +1 @@\n-old\n+new\n")
    assert len(doc.files) == 1
    assert doc.files[0].old_path == "x.txt"
    assert doc.files[0].target_path == "x.txt"
    assert [ln.text for ln in doc.files[0].hunks[0].lines] == ["old", "new"]


def test_连续两个只有减号头的块都要收进文件列表() -> None:
    doc = UnifiedPatchParser().parse("--- a/one.txt\n--- a/two.txt\n")
    assert [f.target_path for f in doc.files] == ["one.txt", "two.txt"]


def test_减号头是最后一行时不炸下标() -> None:
    doc = UnifiedPatchParser().parse("--- a/x.txt")
    assert [f.target_path for f in doc.files] == ["x.txt"]


def test_加号dev_null在末行且无换行时仍算删除() -> None:
    doc = UnifiedPatchParser().parse("--- a/gone.txt\n+++ /dev/null")
    assert doc.files[0].deleted is True
    assert doc.files[0].target_path == "gone.txt"


def test_改名的块old与new各自记下不被下一块污染() -> None:
    text = (
        "--- a/old.txt\n+++ b/new.txt\n@@ -1 +1 @@\n-a\n+b\n"
        "--- a/second.txt\n+++ b/second.txt\n@@ -1 +1 @@\n-c\n+d\n"
    )
    doc = UnifiedPatchParser().parse(text)
    assert [(f.old_path, f.target_path) for f in doc.files] == [
        ("old.txt", "new.txt"),
        ("second.txt", "second.txt"),
    ]


def test_删除一个文件不会中断后面文件的处理(tmp_path: Path) -> None:
    _write(tmp_path / "gone.txt", "x\n")
    _write(tmp_path / "keep.txt", "base\n")
    diff = (
        "--- a/gone.txt\n+++ /dev/null\n"
        "--- a/keep.txt\n+++ b/keep.txt\n@@ -1 +1 @@\n-base\n+patched\n"
    )
    assert _apply(diff, tmp_path) == ["gone.txt", "keep.txt"]
    assert (tmp_path / "keep.txt").read_text(encoding="utf-8") == "patched\n"


def test_越出工作区根的报错带路径且文案完整(tmp_path: Path) -> None:
    doc = PatchDocument([FilePatch("../escape.txt", "../escape.txt", ())])
    with pytest.raises(PatchConflict) as ei:
        PatchApplier().apply(doc, str(tmp_path))
    assert ei.value.reason == "../escape.txt: 路径越出工作区根"
    assert ei.value.path == "../escape.txt"


def test_读写必须显式utf8而不是跟随本机locale(tmp_path: Path) -> None:
    """`encoding=None` 会退回 `locale.getpreferredencoding()`：本机 cp936、CI 容器 C locale 下
    连非 ASCII 内容都会报错。所以“显式 utf-8”是契约（diff 与用户内容都可能非 ASCII）。

    子进程里把 UTF-8 模式与 C locale 强制关掉，这条在 Windows(cp936) 与 CI(C locale) 上都有判别力。
    """
    root = tmp_path / "root"
    root.mkdir()
    old_lit, new_lit = "\u65e7\uff1a\U0001f004", "\u65b0\uff1a\U0001f004"
    code = (
        "import sys\n"
        f"sys.path.insert(0, {_SRC_ROOT!r})\n"
        "from warden_agent.git.patch import PatchApplier, UnifiedPatchParser\n"
        f"old, new = {ascii(old_lit)}, {ascii(new_lit)}\n"
        "root = sys.argv[1]\n"
        "with open(root + '/x.txt', 'w', encoding='utf-8', newline='\\n') as fh:\n"
        "    fh.write(old + '\\n')\n"
        "diff = '--- a/x.txt\\n+++ b/x.txt\\n@@ -1 +1 @@\\n-' + old + '\\n+' + new + '\\n'\n"
        "PatchApplier().apply(UnifiedPatchParser().parse(diff), root)\n"
    )
    env = {
        **os.environ,
        "LC_ALL": "C",
        "LANG": "C",
        "LC_CTYPE": "C",
        "PYTHONUTF8": "0",
        "PYTHONCOERCECLOCALE": "0",
        "PYTHONIOENCODING": "utf-8",
    }
    proc = subprocess.run(
        [sys.executable, "-X", "utf8=0", "-c", code, str(root)],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert proc.returncode == 0, f"显式 utf-8 时不该失败：{proc.stderr[-500:]}"
    assert (root / "x.txt").read_bytes().decode("utf-8") == new_lit + "\n"


# ---- 变异分片第三、四轮：把剩下的存活/超时逐条裁决（2026-10-01）----
# 上游那一轮把 84.4% 抬到 93.1%（19 存活 + 5 超时）。把变异逐条打回源码重跑之后分三类：
#   · 7 条等价变异（`..` 过滤集、`cur_old` 恒真、codec 名大小写、`cur_new = None`）——见
#     docs/mutation.md 第五节，**不补测试**（补不出来）；
#   · 6 条被下面这一批杀掉（解析循环里三处自增、hunk 正文的 `+++` 收尾、CRLF 归一化）；
#   · 6 条仍在存活，看下来是**“测试写法”的陷阱**而不是实现的：
#       1) 预算默认值（`200_000` → `200001`）：`inspect.signature` 这种反射断言**永远杀不死变异**——
#          变异运行只跑“覆盖到该函数体”的测试，而反射不执行函数体（文件末尾那条用例有说明）；
#       2) `apply` 的 `encoding="utf-8"` / `newline="\n"`：在 Linux + UTF-8 环境里它们与显式值
#          **行为完全一样**，行为断言没有判别力（只在 Windows 的 cp936/CRLF 上才差异），
#          所以要直接钉 kwargs 这个契约。
# 教训一句：**“能杀死变异的断言”必须是“真的把改坏的那段跑起来、并且看一眼只有它会变的那处输出”。**


def test_CRLF换行的diff归一化后hunk完整且落盘不带回车(tmp_path: Path) -> None:
    """Windows 与不少 JS 工具链产出的 diff 是 CRLF（git 也可能配成 `core.autocrlf=true`）。

    `parse()` 开头那行 `text.replace("\r\n", "\n")` 被改坏（匹配串首尾被塞进 `XX`）时，
    行尾会留下裸 `\r`：`_HUNK_RE` 末尾的 `$` 不认 `\r`，于是**hunk 头整行匹配不上** ——
    hunk 全丢、正文行各自带回车。两个后果都是静默的：补丁"应用成功"却一行没改，
    或者把 `\r` 写进了文件（要等下游 hash 校验/CI diff 检查才发现）。
    """
    crlf = "--- a/x.txt\r\n+++ b/x.txt\r\n@@ -1 +1 @@\r\n-old\r\n+new\r\n"
    doc = UnifiedPatchParser().parse(crlf)
    assert [f.target_path for f in doc.files] == ["x.txt"]
    assert [(ln.kind, ln.text) for ln in doc.files[0].hunks[0].lines] == [
        ("-", "old"),
        ("+", "new"),
    ]
    # 解析出来的文本不带 \r，`apply` 的 newline="\n" 才有意义；两边一起钉住
    _write(tmp_path / "x.txt", "old\n")
    assert _apply(crlf, tmp_path) == ["x.txt"]
    assert (tmp_path / "x.txt").read_bytes() == b"new\n"


def test_块前面有一行前导行时两个块各自只收一次且hunk不丢() -> None:
    """真实 diff 每个文件块前面还有 `diff --git` / `index` 这类前导行（循环底部 `idx += 1` 跳过）。

    那个自增被改成 `idx += 2` 时，跳过的就是它后面那一行；如果紧挨着的正是 `--- a/` 文件头，
    整个块的头就被跨过去，块被并到上一个块里（目标路径与 hunk 归属全错）。
    前导行只留**一行**：留两行时多跨一步恰好又落在 `---` 上，这条变异就分辨不出来了。
    """
    text = (
        "diff --git a/x.txt b/x.txt\n--- a/x.txt\n+++ b/x.txt\n@@ -1 +1 @@\n-a\n+b\n"
        "diff --git a/y.txt b/y.txt\n--- a/y.txt\n+++ b/y.txt\n@@ -1 +1 @@\n-c\n+d\n"
    )
    doc = UnifiedPatchParser().parse(text)
    assert [f.target_path for f in doc.files] == ["x.txt", "y.txt"]
    assert [len(f.hunks) for f in doc.files] == [1, 1]
    assert [ln.text for ln in doc.files[1].hunks[0].lines] == ["c", "d"]


def test_CRLF的删除补丁必须真的算删除(tmp_path: Path) -> None:
    """归一化那行被改坏时，嵌在 `\r\n` 里的额外空白会把**删除标志**吃掉。

    `text.replace("XX\r\nXX", "\n")` 匹配不上，后面那句 `.replace("\r", "\n")` 仍会把
    CRLF 拆成**两行**（多出一个空行）。空行本身无害——有害的是它插在了
    `--- a/x` 与 `+++ /dev/null` 之间：紧邻 `+++` 的前瞻失效，而兜底那个分支只认
    `+++ b/`，于是 `+++ /dev/null` 被当成无关行跳过：

      * `deleted` 从 True 静默变成 **False**；
      * `target_path` 从 `/dev/null`（= 删除）变成旧路径——补丁“应用成功”但**文件没被删掉**。

    为什么用 CRLF：`core.autocrlf=true`、Windows 编辑器、`git diff` 管道到文件都会产出 CRLF。
    """
    deleted_diff = "--- a/gone.txt\r\n+++ /dev/null\r\n"
    doc = UnifiedPatchParser().parse(deleted_diff)
    assert [f.deleted for f in doc.files] == [True]
    assert [f.target_path for f in doc.files] == ["gone.txt"]
    # 真落盘一次：文件必须真的没了（只断言对象结构会漏掉 apply 那边的错）
    _write(tmp_path / "gone.txt", "x\n")
    assert _apply(deleted_diff, tmp_path) == ["gone.txt"]
    assert not (tmp_path / "gone.txt").exists()


def test_块内非hunk行只跳过它自己一行() -> None:
    """循环底部那句 `idx += 1` 被改成 `idx += 2` 时，它会**连下一行一起跳掉**。

    `diff --git` / `index` 这类元信息行除了在块前面，也可能落在块**内部**
    （`+++` 之后、`@@` 之前）——拼接/裁剪过的补丁、手写的补丁都会这样。
    下一行正好是 `@@` 时，整个 hunk 被丢掉：补丁仍然“成功”，但**一行都没改**。
    """
    text = (
        "--- a/x.txt\n+++ b/x.txt\nindex 0123..4567 100644\n"
        "@@ -1 +1 @@\n-a\n+b\n"
    )
    doc = UnifiedPatchParser().parse(text)
    assert [len(f.hunks) for f in doc.files] == [1]
    assert [(ln.kind, ln.text) for ln in doc.files[0].hunks[0].lines] == [
        ("-", "a"),
        ("+", "b"),
    ]


def test_正文里孤立的加号头会收尾并更新目标路径() -> None:
    """`+++ b/` 也可能不紧跟 `---`（只有减号头的块后面接一个加号头），解析器为此单开了一个分支。

    两处边界一起钉：
      · hunk 正文收集循环里"遇到 `+++` 就停"被改坏时，`+++ b/y.txt` 会因为它以 `+` 开头
        被当成新增行收进上一个 hunk —— 多出一行**虚假新增**，且目标路径不再更新（补丁打到错文件）；
      · 那个独立分支自己的 `idx += 1`/`continue` 被改成跨两步或 `break` 时，后面的文件块会丢。
    """
    text = (
        "--- a/x.txt\n@@ -1 +1 @@\n-a\n+b\n+++ b/y.txt\n"
        "--- a/z.txt\n+++ b/z.txt\n@@ -1 +1 @@\n-c\n+d\n"
    )
    doc = UnifiedPatchParser().parse(text)
    assert [(f.old_path, f.target_path) for f in doc.files] == [
        ("x.txt", "y.txt"),
        ("z.txt", "z.txt"),
    ]
    assert [(ln.kind, ln.text) for ln in doc.files[0].hunks[0].lines] == [
        ("-", "a"),
        ("+", "b"),
    ]
    assert [ln.text for ln in doc.files[1].hunks[0].lines] == ["c", "d"]


def test_行数预算的默认值必须真的是20万行_多一行就要拒绝() -> None:
    """默认预算被改成 `200001` 时，多出来的那一行会被静默放行，而调用方无从发现。

    这条**故意不用** `inspect.signature` 那种反射断言：变异运行时只跑“覆盖到该函数体”的
    测试（`mutmut/__main__.py` 用 `tests_by_mangled_function_name` 逐条筛），而反射拿参数
    根本不执行函数体，于是那种断言看着很严、实际一条变异也杀不掉。
    要真钉住它，必须用一个**超预算的 diff 走一整遍 `parse()`**（200_001 行正文 ≈ 600KB）。
    """

    def diff_with(content_lines: int) -> str:
        return "--- a/x.txt\n+++ b/x.txt\n@@ -1,1 +1,1 @@\n" + " x\n" * content_lines

    # 上界：恰好 200_000 行（默认上限）要通过
    assert UnifiedPatchParser().parse(diff_with(200_000)).files[0].target_path == "x.txt"
    # 多一行就要拒绝，且理由逐字（顺带钉住默认值不是 200_001 而是 200_000）
    with pytest.raises(PatchConflict) as ei:
        UnifiedPatchParser().parse(diff_with(200_001))
    assert ei.value.reason == "diff 超过行数预算"


def test_apply读写文件必须显式指定utf8和LF(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """模块 docstring 里写的契约：读写一律显式 utf-8、写回一律 `newline="\n"`。

    为什么这条必须钉 **kwargs** 而不是行为：在 Linux + UTF-8 环境里 `encoding=None` 恰好就是
    UTF-8、`newline=None` 恰好也写 `\n` —— 与显式值**逐字节相同**，于是行为断言在这类机器上
    一辈子都杀不掉这两条变异（只在 Windows 的 cp936/CRLF 上才露马脚，而变异测试跑在
    Linux 容器里）。而契约本身就是“必须显式传这两个参数”，所以直接断言参数。
    """
    calls: list[dict[str, object]] = []
    real_read, real_write = Path.read_text, Path.write_text

    def spy_read(self: Path, *args: object, **kwargs: object) -> str:
        calls.append({"op": "read", **kwargs})
        return real_read(self, *args, **kwargs)  # type: ignore[arg-type]

    def spy_write(self: Path, data: str, *args: object, **kwargs: object) -> int:
        calls.append({"op": "write", **kwargs})
        return real_write(self, data, *args, **kwargs)  # type: ignore[arg-type]

    # 先造现场再装 spy：否则 _write 自己也会记一笔，弱化“确实走过 apply 的读写”这个结论
    _write(tmp_path / "x.txt", "旧\n")
    monkeypatch.setattr(Path, "read_text", spy_read)
    monkeypatch.setattr(Path, "write_text", spy_write)

    assert _apply("--- a/x.txt\n+++ b/x.txt\n@@ -1 +1 @@\n-旧\n+新\n", tmp_path) == ["x.txt"]

    reads = [c for c in calls if c["op"] == "read"]
    writes = [c for c in calls if c["op"] == "write"]
    # 先确认“真的读写过”：否则入口被改坏时会退化成空断言通过
    assert reads and writes, calls
    for c in reads:
        assert c.get("encoding") == "utf-8", c
    for c in writes:
        assert c.get("encoding") == "utf-8", c
        assert c.get("newline") == "\n", c
    # 顺带确认 spy 没把写入变成空转
    assert (tmp_path / "x.txt").read_bytes() == b"\xe6\x96\xb0\n"
