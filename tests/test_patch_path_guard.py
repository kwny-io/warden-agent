"""patch 路径边界守卫（安全回归）。

为什么单独一个文件：`git.apply_patch` 的 diff 来自**不可信输入**——模型生成的 diff，
或 `web.fetch` 抓到的网页正文经提示注入诱导出的 diff。补丁里的路径如果不校验，
`--- a/../../<任意路径>` 就能把文件写到工作区之外（任意写），配 `+++ /dev/null` 还能删任意文件。

这组测试把"路径必须待在根目录内"钉死：解析期拒绝 `..`/绝对路径/盘符/反斜杠，
应用期再用 resolve 后的包含校验兜底（挡符号链接指向外部）。
"""

from __future__ import annotations

import os
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


def _write_diff(path: str) -> str:
    return f"--- a/{path}\n+++ b/{path}\n@@ -0,0 +1 @@\n+pwned\n"


# ---- 解析期：拒绝一切逃逸形态 ----


@pytest.mark.parametrize(
    "path",
    [
        "../escape.txt",            # 上一级
        "../../escape.txt",         # 更上层
        "sub/../../escape.txt",     # 中间夹带
        "/tmp/escape.txt",          # 绝对路径（POSIX）
        "//server/share/x.txt",     # UNC 风格
        "C:/Windows/x.txt",         # Windows 盘符
        "..\\escape.txt",           # 反斜杠
        "sub\\..\\..\\escape.txt",  # 反斜杠 + ..
    ],
)
def test_解析期拒绝逃逸路径(path: str) -> None:
    with pytest.raises(PatchConflict):
        UnifiedPatchParser().parse(_write_diff(path))


def test_正常相对路径仍可解析与定位() -> None:
    doc = UnifiedPatchParser().parse(_write_diff("src/pkg/mod.py"))
    assert doc.files[0].target_path == "src/pkg/mod.py"


# ---- 应用期：兜底包含校验（即使 parser 被绕过也要挡住）----


def _direct_document(target: str) -> PatchDocument:
    """直接构造 FilePatch，模拟"绕过解析器"的情况。"""
    hunk = PatchHunk(0, 0, 1, 1, (PatchLine("+", "pwned"),))
    return PatchDocument((FilePatch(target, target, (hunk,)),))


def test_应用期拒绝越出根目录(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "escape.txt"

    with pytest.raises(PatchConflict):
        PatchApplier().apply(_direct_document("../escape.txt"), str(root))
    assert not outside.exists(), "越界写入必须没有发生"


def test_应用期拒绝符号链接逃逸(tmp_path: Path) -> None:
    """仓库内建一个指向外部的软链，补丁写 `link/x` —— resolve 后应判定越界。"""
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "link"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前平台/权限不支持创建符号链接")

    with pytest.raises(PatchConflict):
        PatchApplier().apply(_direct_document("link/evil.txt"), str(root))
    assert not (outside / "evil.txt").exists()


def test_正常写入仍在根目录内(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    doc = UnifiedPatchParser().parse(_write_diff("a/b/c.txt"))
    changed = PatchApplier().apply(doc, str(root))
    assert changed == ["a/b/c.txt"]
    assert (root / "a" / "b" / "c.txt").read_text(encoding="utf-8") == "pwned"
