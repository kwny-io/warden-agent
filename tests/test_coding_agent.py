"""Coding Agent 测试：代码浏览工具的边界安全 + 离线跑通。

重点是工具层的"受限读取"（防止读 workdir 之外的文件）——这是 Coding Agent
安全性的关键，也是可确定性测试的部分。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from warden_agent.coding_agent.coding_agent import _make_code_tools, _resolve_within


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    (tmp_path / "hello.py").write_text("def greet():\n    return 'hi'\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "lib.py").write_text("x = 1\n", encoding="utf-8")
    return tmp_path


def _tool_by_name(tools, name):
    for t in tools:
        if t.name == name:
            return t
    raise AssertionError(f"缺工具 {name}")


def test_resolve_within_拒绝越界(workdir: Path) -> None:
    assert _resolve_within(str(workdir), "..") is None
    assert _resolve_within(str(workdir), "../etc/passwd") is None
    assert _resolve_within(str(workdir), "/abs/path") is None
    assert _resolve_within(str(workdir), "hello.py") is not None


def test_code_read_读文件(workdir: Path) -> None:
    tools = _make_code_tools(str(workdir))
    read = _tool_by_name(tools, "code.read")
    out = read.function("hello.py")
    assert "def greet" in out


def test_code_read_拒绝越界文件(workdir: Path, tmp_path: Path) -> None:
    tools = _make_code_tools(str(workdir))
    read = _tool_by_name(tools, "code.read")
    out = read.function("../outside.txt")  # workdir 之外
    assert out.startswith("[拒绝]")


def test_code_read_不存在文件(workdir: Path) -> None:
    tools = _make_code_tools(str(workdir))
    read = _tool_by_name(tools, "code.read")
    out = read.function("nope.py")
    assert out.startswith("[拒绝]")


def test_code_list_列目录(workdir: Path) -> None:
    tools = _make_code_tools(str(workdir))
    lst = _tool_by_name(tools, "code.list")
    out = lst.function(".")
    assert "hello.py" in out
    assert "sub" in out
