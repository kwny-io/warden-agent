"""Coding Agent 测试：代码浏览工具的边界安全 + 离线跑通。

重点是工具层的"受限读取"（防止读 workdir 之外的文件）——这是 Coding Agent
安全性的关键，也是可确定性测试的部分。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from tests.conftest import ScriptedModel

from warden_agent.coding_agent.coding_agent import (
    _extract_applied_files,
    _make_code_tools,
    _protect_path_policy,
    _resolve_within,
    run_coding_task,
)
from warden_agent.model.model import ChatResponse, ToolCall
from warden_agent.policy.policy import Decision


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


# ---- applied_files：从 git.apply_patch 的产出里真正收集落地文件 ----

def test_提取已应用文件_成功() -> None:
    assert _extract_applied_files("已应用 2 个文件: a.py, b.py") == ["a.py", "b.py"]
    assert _extract_applied_files("已应用 1 个文件: only.py") == ["only.py"]


def test_提取已应用文件_拒绝时为空() -> None:
    assert _extract_applied_files("[拒绝] PATCH_CONFLICT: 基准不符") == []


def test_path门禁_默认保护主目录() -> None:
    engine = _protect_path_policy(None)
    home = Path.home()
    denied = engine.evaluate("code.read", {"path": str(home / ".ssh" / "id_rsa")})
    assert denied.decision == Decision.DENY
    allowed = engine.evaluate("code.read", {"path": "hello.py"})
    assert allowed.decision == Decision.ALLOW


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


def test_run_coding_task_回填applied_files(tmp_path: Path) -> None:
    """端到端：模型提交 diff → 落地 → CodingResult.applied_files 非空。"""
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "tester")
    (tmp_path / "hello.py").write_text(
        "def greet():\n    return 'hi'\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "init")

    diff = (
        "--- a/hello.py\n"
        "+++ b/hello.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def greet():\n"
        "-    return 'hi'\n"
        "+    return 'hello'\n"
    )
    model = ScriptedModel([
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="c1", name="git.apply_patch", arguments={"diff": diff})],
            finish_reason="tool_calls"),
        ChatResponse(content="已修改 hello.py", finish_reason="stop"),
    ])
    result = run_coding_task(
        "把 hi 改成 hello", str(tmp_path),
        provider=model, protected_paths=(),
    )
    assert result.applied_files == ["hello.py"]
    assert "return 'hello'" in (tmp_path / "hello.py").read_text(encoding="utf-8")
