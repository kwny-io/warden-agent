"""T1 执行沙箱测试：只读工作区 + 禁网 NetworkPolicy + 超时。

用真实 subprocess 跑命令（跨平台：Windows 用 shell 命令的可执行形式）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from warden_agent.execution.broker import ExecutionBudget
from warden_agent.execution.sandbox import (
    NetworkPolicy,
    SandboxedExecutionBroker,
    SandboxSpec,
    _copy_tree_readonly,
)


def test_禁网模式下拒绝网络命令() -> None:
    spec = SandboxSpec(allow_network=False)
    broker = SandboxedExecutionBroker(spec=spec)
    r = broker.execute(["curl", "https://example.com"])
    assert "沙箱拒绝" in r.stderr
    assert "禁网" in r.stderr


def test_禁网但非网络命令可执行() -> None:
    spec = SandboxSpec(allow_network=False)
    broker = SandboxedExecutionBroker(spec=spec)
    cmd = [sys.executable, "-c", "print('hi from sandbox')"]
    r = broker.execute(cmd)
    assert r.exit_code == 0
    assert "hi from sandbox" in r.stdout


def test_允许网络时放行() -> None:
    spec = SandboxSpec(allow_network=True)
    broker = SandboxedExecutionBroker(spec=spec)
    r = broker.execute(["curl", "https://example.com"])
    # 放行了才会尝试真的跑；这里只看"不再因禁网被拒"
    assert "沙箱拒绝" not in r.stderr


def test_只读工作区_改动不到宿主(tmp_path: Path) -> None:
    input_dir = tmp_path / "src"
    input_dir.mkdir()
    target = input_dir / "data.txt"
    target.write_text("ORIGINAL", encoding="utf-8")

    spec = SandboxSpec(readonly_workspace=True, workspace_root=str(tmp_path))
    broker = SandboxedExecutionBroker(spec=spec)
    # 在沙箱工作区里改写 data.txt（但宿主那份不能被改）
    cmd = [sys.executable, "-c",
           "from pathlib import Path; Path('data.txt').write_text('HACKED')"]
    r = broker.execute(cmd, workspace_input=input_dir)
    assert r.exit_code in (0, 1)  # 命令本身跑没跑成不重要
    # 关键断言：宿主原始文件没被改
    assert target.read_text(encoding="utf-8") == "ORIGINAL"


def test_未配workspace_root时拒绝拷入任意路径() -> None:
    """workspace_input 常来自模型参数（不可信）：不配根目录就必须 fail-closed 拒绝。"""
    broker = SandboxedExecutionBroker(spec=SandboxSpec(readonly_workspace=True))
    run = broker.execute
    cmd = [sys.executable, "-c", "print(1)"]
    outside = str(Path.home())
    with pytest.raises(ValueError, match="workspace_root"):
        run(cmd, workspace_input=outside)


def test_workspace_input越出根目录被拒(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    spec = SandboxSpec(readonly_workspace=True, workspace_root=str(root))
    broker = SandboxedExecutionBroker(spec=spec)
    run = broker.execute
    cmd = [sys.executable, "-c", "print(1)"]
    with pytest.raises(ValueError, match="不在允许的根目录内"):
        run(cmd, workspace_input=str(outside))


def test_只读工作区_不拷贝指向外部的符号链接(tmp_path: Path) -> None:
    """工作区内的软链不能把宿主外部文件带进沙箱（_copy_tree_readonly 须跳过软链）。"""
    src = tmp_path / "src"
    src.mkdir()
    (src / "ok.txt").write_text("OK", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("SECRET", encoding="utf-8")
    link = src / "leak.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不支持创建符号链接")

    dst = tmp_path / "dst"
    _copy_tree_readonly(src, dst)
    assert (dst / "ok.txt").read_text(encoding="utf-8") == "OK"
    assert not (dst / "leak.txt").exists(), "指向外部的软链绝不应被拷入沙箱"


def test_超时强杀() -> None:
    spec = SandboxSpec(budget=ExecutionBudget(timeout_seconds=1))
    broker = SandboxedExecutionBroker(spec=spec)
    run = broker.execute
    cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
    r = run(cmd, workspace_input=None)
    assert r.timed_out is True
    # 跑完即可，不要求特定 exit_code（超时被强行终止）

def test_network_policy_对象判断() -> None:
    p = NetworkPolicy(allow_network=False)
    assert not p.allows(["wget", "x"])
    assert not p.allows(["python", "-c", "import urllib"])
    assert p.allows(["ls", "-la"])


def test_带资源限制_普通命令可执行() -> None:
    """给 budget 设内存/CPU 限制，普通命令仍能正常跑（Windows 走 Job Object）。

    ⚠️ 这里刻意用 `sys._base_executable`（venv 背后的**真**解释器），而不是 `sys.executable`：
    Windows 上 venv 里的 python.exe 只是个**转发器**，它还要自己再拉起真解释器；而一旦转发器
    被放进 Job Object（本测试正是要验证的路径），这个子进程创建就会失败，报
    `Unable to create process using ...`（exit 101）。已实测：真解释器与 cmd 都正常，只有转发器
    不行，且与"设了哪个具体限制"无关 —— 只要进程被放进 Job Object 就会中。这是 Windows 的既有
    行为（应用层沙箱的已知边界，已写进 `execution/_platform.py` 的说明），不是本项目的 bug；
    但测试不该被它误伤，所以取真解释器路径来测"带限制的普通命令仍可执行"这件事本身。
    """
    from warden_agent.execution.broker import ExecutionBudget

    spec = SandboxSpec(budget=ExecutionBudget(
        timeout_seconds=10, max_memory_mb=512, max_cpu_seconds=30))
    broker = SandboxedExecutionBroker(spec=spec)
    cmd = [getattr(sys, "_base_executable", sys.executable), "-c", "print('limited-ok')"]
    r = broker.execute(cmd)
    assert r.exit_code == 0
    assert "limited-ok" in r.stdout


def test_make_limiter_无资源限制返回None() -> None:
    """无内存/CPU/文件限制时，不构造平台 limiter（普通执行不背 Job/rlimit 开销）。"""
    from warden_agent.execution._platform import make_limiter
    from warden_agent.execution.broker import ExecutionBudget

    assert make_limiter(ExecutionBudget()) is None


def test_make_limiter_有内存限制则构造() -> None:
    from warden_agent.execution._platform import make_limiter
    from warden_agent.execution.broker import ExecutionBudget

    limiter = make_limiter(ExecutionBudget(max_memory_mb=256))
    assert limiter is not None


def test_每个托管进程各持有自己的limiter_不互相覆盖() -> None:
    """回归：limiter（Windows 上是 Job Object 句柄）原先只存在一个 `_last_limiter` 槽位里，
    并发执行时后一个覆盖前一个 → 前一个失去引用可能被 GC、限制提前失效。
    现在 limiter 跟着**进程条目**一起持有。这里用内部接口直接验证"不互相覆盖"。
    """
    from warden_agent.execution.broker import ExecutionBroker

    class _FakeProc:
        pid = 1

        def poll(self) -> None:
            return None

    broker = ExecutionBroker(ExecutionBudget(max_processes=10))
    limiter_a, limiter_b = object(), object()
    broker._track(_FakeProc(), limiter=limiter_a)  # type: ignore[arg-type]
    broker._track(_FakeProc(), limiter=limiter_b)  # type: ignore[arg-type]
    held = [m.limiter for m in broker._active]
    assert held == [limiter_a, limiter_b], f"两个进程的 limiter 必须都在，实际 {held}"

