r"""Unified diff（统一 diff）解析与逐 hunk 应用到工作区文件。

  - 文件头：`--- a/<path>` / `+++ b/<path>`（前缀必须是 a/、b/；`/dev/null`=新增/删除）。
  - Hunk 头：`@@ -l,c +l,c @@`。
  - 行前缀：` ` = 上下文，`+` = 新增，`-` = 删除。—— 换行缺失标记 `\ No newline ...`。
  - 预算：max_files / max_lines / max_bytes，NUL 抛错，换行归一化 LF。
  - 应用校验：逐 hunk 按 old line 数定位应用；加上"每文件预期 hash"校验
    （expectedHashes + expectedRevision）。

设计要点：只改动 patch 里明确列出的文件/行，绝不隐式改动其它地方，且应用前校验基准。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PatchLine:
    kind: str  # " " context / "+" add / "-" remove
    text: str


@dataclass(frozen=True)
class PatchHunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[PatchLine, ...]


@dataclass(frozen=True)
class FilePatch:
    old_path: str
    new_path: str
    hunks: tuple[PatchHunk, ...]

    @property
    def target_path(self) -> str:
        """要写到哪个文件（新增/修改用 new；删除用 old）。"""
        if self.new_path == "/dev/null":
            return self.old_path
        return self.new_path

    @property
    def deleted(self) -> bool:
        return self.new_path == "/dev/null"


@dataclass
class PatchDocument:
    files: list[FilePatch]

    @property
    def sha256(self) -> str:
        """对整个 diff 做 sha256。"""
        digest = hashlib.sha256()
        for f in self.files:
            digest.update(f"{f.old_path}\0{f.new_path}\0".encode())
            for h in f.hunks:
                for ln in h.lines:
                    digest.update(f"{ln.kind}{ln.text}\0".encode())
        return f"sha256:{digest.hexdigest()}"


@dataclass
class PatchConflict(Exception):
    """patch 应用冲突。"""

    reason: str
    path: str | None = None


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$")
# Windows 盘符（`C:` / `c:/`）——绝对路径的另一种写法，也必须当成逃逸
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _reject_unsafe_path(path: str) -> None:
    """拒绝会逃出工作区的路径：绝对路径 / 盘符 / `..` 段 / 反斜杠。

    反斜杠也拒（而不是"在 Windows 上正好当分隔符"）：git diff 用正斜杠，
    出现反斜杠基本就是有人想借平台差异绕过检查。
    """
    if not path.strip():
        raise PatchConflict("diff 里出现空路径")
    if "\\" in path:
        raise PatchConflict(f"拒绝含反斜杠的路径：{path!r}")
    if path.startswith("/") or _DRIVE_RE.match(path):
        raise PatchConflict(f"拒绝绝对路径：{path!r}")
    segments = [seg for seg in path.split("/") if seg not in ("", ".")]
    if ".." in segments:
        raise PatchConflict(f"拒绝含 '..' 的路径：{path!r}")


class UnifiedPatchParser:
    """把 unified diff 文本解析成 PatchDocument。带预算限制。"""

    def __init__(self, max_files: int = 200, max_lines: int = 200_000,
                 max_bytes: int = 5 * 1024 * 1024) -> None:
        self.max_files = max_files
        self.max_lines = max_lines
        self.max_bytes = max_bytes

    def parse(self, text: str) -> PatchDocument:
        if len(text.encode("utf-8")) > self.max_bytes:
            raise PatchConflict("diff 超过字节预算")
        if "\x00" in text:
            raise PatchConflict("diff 含 NUL 字节，拒绝")
        text = text.replace("\r\n", "\n").replace("\r", "\n")

        files: list[FilePatch] = []
        cur_old = cur_new = ""
        hunks: list[PatchHunk] = []
        idx = 0
        lines = text.split("\n")
        total_lines = 0

        while idx < len(lines):
            line = lines[idx]
            if line.startswith("--- a/") or line.startswith("--- /dev/null"):
                # 新文件块
                if cur_old or cur_new:
                    self._flush(files, cur_old, cur_new, hunks, cur_old)
                cur_old = self._norm_path(line[4:].strip())
                cur_new = ""
                hunks = []
                if len(files) + (1 if cur_old else 0) > self.max_files:
                    raise PatchConflict("diff 超过文件数预算")
                # 找 +++
                if idx + 1 < len(lines) and lines[idx + 1].startswith("+++"):
                    cur_new = self._norm_path(lines[idx + 1][4:].strip())
                    idx += 1
                idx += 1
                continue
            if line.startswith("+++ b/"):
                cur_new = self._norm_path(line[4:].strip())
                idx += 1
                continue
            m = _HUNK_RE.match(line)
            if m:
                old_s = int(m.group(1))
                new_s = int(m.group(3))
                old_c = int(m.group(2) or 1)
                new_c = int(m.group(4) or 1)
                hunk_lines: list[PatchLine] = []
                a = idx + 1
                # 收集 hunk 的正文直到下一个 @@ 或文件已尽
                while a < len(lines) and not _HUNK_RE.match(lines[a]) \
                        and not lines[a].startswith("---") \
                        and not lines[a].startswith("+++"):
                    content = lines[a]
                    if content.startswith((" ", "+", "-")):
                        hunk_lines.append(PatchLine(content[0], content[1:]))
                        total_lines += 1
                        if total_lines > self.max_lines:
                            raise PatchConflict("diff 超过行数预算")
                    # 忽略 "\ No newline ..." 等标记行
                    a += 1
                hunks.append(PatchHunk(old_s, old_c, new_s, new_c, tuple(hunk_lines)))
                idx = a
                continue
            idx += 1

        if cur_old or cur_new:
            self._flush(files, cur_old, cur_new, hunks, cur_old)
        if not files:
            raise PatchConflict("无法解析出任何文件 patch")
        return PatchDocument(files)

    @staticmethod
    def _norm_path(p: str) -> str:
        """去掉 a/、b/ 前缀（保持 /dev/null 原样），并**拒绝逃出工作区根的路径**。

        安全边界（这是必校验项，不是清洁工作）：补丁的路径来自**不可信输入**——
        模型生成的 diff，或 `web.fetch` 抓到的网页正文经提示注入诱导出的 diff。
        若不校验，`--- a/../../<任意路径>` 就能把文件写到工作区之外（任意写），
        配合 `+++ /dev/null` 还能删任意文件。所以 `..`、绝对路径、Windows 盘符/反斜杠
        一律在这里拒绝（`PatchApplier.apply` 里还有一道 resolve 后的包含校验兜底，
        用来挡符号链接指向外部的情况）。
        """
        if p in ("/dev/null", "a/dev/null", "b/dev/null"):
            return "/dev/null"
        for prefix in ("a/", "b/"):
            if p.startswith(prefix):
                p = p[len(prefix):]
                break
        _reject_unsafe_path(p)
        return p

    @staticmethod
    def _flush(files: list[FilePatch], oldp: str, newp: str,
               hunks: list[PatchHunk], anchor: str) -> None:
        if oldp.strip() or newp.strip():
            files.append(FilePatch(anchor, newp if newp else anchor, tuple(hunks)))


class PatchApplier:
    """把 PatchDocument 应用到工作区文件（带每文件预期 hash 校验）。

    应用算法（逐 hunk、逐行、按 old 行定位）：
      维护一个"旧文件行游标"，按 hunk 的 old_start 顺序处理每个 hunk：
        - 先用上下文行把游标推进到 hunk 起点；
        - 在 hunk 内：context=保留+推进、add=插入、remove=推进（丢弃竖行）。
      最后把游标剩余的行补上。
    """

    def apply(self, document: PatchDocument, root: str,
              expected_hashes: dict[str, str] | None = None) -> list[str]:
        changed: list[str] = []
        root_resolved = Path(root).resolve()
        for fp in document.files:
            # 第二道防线：parser 已拒 `..`/绝对路径，这里再用 resolve 后的**包含校验**兜底——
            # resolve() 会跟随符号链接，所以"仓库内有个指向外部的软链"这种绕法也会被挡下。
            abs_path = (root_resolved / fp.target_path).resolve()
            if not abs_path.is_relative_to(root_resolved):
                raise PatchConflict(
                    f"{fp.target_path}: 路径越出工作区根", path=fp.target_path)
            if fp.deleted:
                if abs_path.exists():
                    abs_path.unlink()
                    changed.append(fp.target_path)
                continue
            old_text = abs_path.read_text(encoding="utf-8") if abs_path.exists() else ""
            if expected_hashes and fp.target_path in expected_hashes:
                actual = hashlib.sha256(old_text.encode()).hexdigest()
                if actual != expected_hashes[fp.target_path]:
                    raise PatchConflict(
                        f"{fp.target_path}: 当前内容 hash 与预期不符",
                        path=fp.target_path)
            new_lines = self._apply_file(fp, old_text)
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_text("\n".join(new_lines), encoding="utf-8")
            changed.append(fp.target_path)
        return changed

    @staticmethod
    def _apply_file(fp: FilePatch, old_text: str) -> list[str]:
        old = old_text.split("\n") if old_text else []
        result: list[str] = []
        pos = 0  # 游标指向 old 的下一行
        pending_hunks = sorted(fp.hunks, key=lambda h: h.old_start)

        while pending_hunks:
            h = pending_hunks.pop(0)
            target = h.old_start - 1  # 1-based -> 0-based 起点
            # 先用上下文/未涉及行把 pos 推进到 target
            while pos < target and pos < len(old):
                result.append(old[pos])
                pos += 1
            if pos < target:
                raise PatchConflict(f"{fp.target_path}: hunk 起点越界超出了文件末尾")
            # 应用 hunk 的行
            for ln in h.lines:
                if ln.kind == "+":
                    result.append(ln.text)
                elif ln.kind == "-":
                    pos += 1  # 丢弃 old 的该行
                else:  # context
                    if pos < len(old):
                        result.append(old[pos])
                        pos += 1
                    else:
                        result.append(ln.text)
        # 补齐剩余
        while pos < len(old):
            result.append(old[pos])
            pos += 1
        return result

