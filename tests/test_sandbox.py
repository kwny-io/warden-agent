"""T1 执行沙箱测试：只读工作区 + 禁网 NetworkPolicy + 超时。

用真实 subprocess 跑命令（跨平台：Windows 用 shell 命令的可执行形式）。
"""

from __future__ import annotations

import sys
from pathlib import Path

from warden_agent.execution.broker import ExecutionBudget
from warden_agent.execution.sandbox import (
    NetworkPolicy,
    SandboxedExecutionBroker,
    SandboxSpec,
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

    spec = SandboxSpec(readonly_workspace=True)
    broker = SandboxedExecutionBroker(spec=spec)
    # 在沙箱工作区里改写 data.txt（但宿主那份不能被改）
    cmd = [sys.executable, "-c",
           "from pathlib import Path; Path('data.txt').write_text('HACKED')"]
    r = broker.execute(cmd, workspace_input=input_dir)
    assert r.exit_code in (0, 1)  # 命令本身跑没跑成不重要
    # 关键断言：宿主原始文件没被改
    assert target.read_text(encoding="utf-8") == "ORIGINAL"


def test_超时强杀() -> None:
    spec = SandboxSpec(budget=ExecutionBudget(timeout_seconds=1))
    broker = SandboxedExecutionBroker(spec=spec)
    cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
    r = broker.execute(cmd, workspace_input=None)
    assert r.timed_out is True
    # 跑完即可，不要求特定 exit_code（超时被强行终止）

def test_network_policy_对象判断() -> None:
    p = NetworkPolicy(allow_network=False)
    assert not p.allows(["wget", "x"])
    assert not p.allows(["python", "-c", "import urllib"])
    assert p.allows(["ls", "-la"])


def test_带资源限制_普通命令可执行() -> None:
    """给 budget 设内存/CPU 限制，普通命令仍能正常跑（Windows 走 Job Object）。"""
    from warden_agent.execution.broker import ExecutionBudget

    spec = SandboxSpec(budget=ExecutionBudget(
        timeout_seconds=10, max_memory_mb=512, max_cpu_seconds=30))
    broker = SandboxedExecutionBroker(spec=spec)
    cmd = [sys.executable, "-c", "print('limited-ok')"]
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
