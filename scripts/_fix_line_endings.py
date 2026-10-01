"""把工作区里的文本文件从 CRLF 归一成 LF（一次性修复脚本）。

为什么需要它：仓库没有 `.gitattributes`，而本机的 `core.autocrlf=true` 会在 git
"碰过"文件时把索引里（LF）的内容按平台重新检出成 CRLF。Windows 上没人注意，
但 **CI 与容器是 Linux**：带 CRLF 的 `*.sh` 会以 `$'\\r': command not found` 直接失败，
带 CRLF 的 Python 文件也会让基于行号/字节的工具（变异测试、覆盖率基线、快照对比）错位。

用法：`python scripts/_fix_line_endings.py [--check]`
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# 只处理明确的文本后缀；二进制（图片、字体、压缩包）一律跳过
TEXT_SUFFIXES = {
    ".py", ".sh", ".bash", ".md", ".json", ".yml", ".yaml", ".toml", ".cfg", ".ini",
    ".ts", ".tsx", ".js", ".mjs", ".cjs", ".css", ".html", ".sql", ".txt", ".lock",
    ".dockerignore", ".gitignore", ".dockerfile", "",
}
TEXT_NAMES = {"Dockerfile", "Makefile", ".gitignore", ".dockerignore"}


def _is_text(path: Path) -> bool:
    return path.name in TEXT_NAMES or path.suffix.lower() in TEXT_SUFFIXES


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], check=True, capture_output=True
    ).stdout
    return [Path(p.decode("utf-8")) for p in out.split(b"\0") if p]


def main() -> int:
    check_only = "--check" in sys.argv[1:]
    dirty: list[Path] = []
    fixed = 0
    for path in _tracked_files():
        if not _is_text(path) or not path.is_file():
            continue
        data = path.read_bytes()
        if b"\r\n" not in data:
            continue
        dirty.append(path)
        if not check_only:
            path.write_bytes(data.replace(b"\r\n", b"\n"))
            fixed += 1

    if check_only:
        for path in dirty[:10]:
            print(f"CRLF: {path}")
        print(f"共 {len(dirty)} 个文本文件带 CRLF" + ("（--check 未改动）" if dirty else ""))
        return 1 if dirty else 0

    print(f"已把 {fixed} 个文件归一成 LF")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
