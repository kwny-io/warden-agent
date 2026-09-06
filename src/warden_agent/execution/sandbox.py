"""执行沙箱（Sandbox）—— 在 ExecutionBroker 之上加一层"隔离语义"。

ExecutionBroker 已经管住"怎么跑、能跑多久、输出多大、最多并发几个"，
但它是 Agent 侧的执行治理，没有"隔离"语义。本模块补齐 T1 的这一档：

  1. 只读工作区（readonly workspace）：
     在临时目录里放一份只读拷贝，让命令跑在临时副本上——改了也回不到宿主，
     跑完即弃。对应"git worktree isolation"的轻量等价物。
  2. 网络策略 NetworkPolicy（默认禁网）：
     默认拒绝看起来会访问网络的命令（curl / wget / ping / nc / http...）。
     语义层的把关——真·网络命名空间隔离依赖平台(Windows 无,Linux 可 unshare)，
     这里先把"默认不给网络"做成本项目可直接讲解/测试的产品行为。
  3. 资源限制（复用 ExecutionBudget：超时 / 输出字节 / 并发进程）。
  4. 超时强杀（ExecutionBroker 已有）。

设计取舍（诚实标注）：
  - 这是"隔离语义"档，不是操作系统级沙箱（bubblewrap / Seatbelt / Job Object）。
    真·文件系统只读、网络命名空间、rlimit 都依赖平台能力，这里做的是：
    * 只读工作区 = 跑在临时副本上（改不到宿主）→ 跨平台可用
    * 禁网 = NetworkPolicy 语义层拒绝 + 不给网络特权 → 跨平台可用
    * 资源/超时 = ExecutionBudget + broker 强杀 → 已有
  - 因此在 Windows / 无沙箱工具的机器上都能跑通，并如实说明边界。
"""

from __future__ import annotations

import contextlib
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from warden_agent.execution.broker import ExecutionBroker, ExecutionBudget, ExecutionResult

# 常见的"会碰网络"的命令/参数片断（小写匹配）。用于 NetworkPolicy 语义层判断。
_NETWORK_TOKENS = re.compile(
    r"(^|[\/\s])(curl|wget|ping|nc|ncat|ssh|scp|ftp|telnet|http|https|urllib|requests)"
    r"([\s:]|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SandboxSpec:
    """一次沙箱执行的配置。

    资源限制经 `budget` 传入（ExecutionBudget 支持 max_memory_mb / max_cpu_seconds /
    max_files）。broker 会用 budget 构造平台限流器（POSIX rlimit / Windows Job Object）。
    例：SandboxSpec(budget=ExecutionBudget(max_memory_mb=256, max_cpu_seconds=10))
    """

    allow_network: bool = False          # 默认禁网
    readonly_workspace: bool = True      # 跑在临时只读副本上
    budget: ExecutionBudget = field(default_factory=ExecutionBudget)


class NetworkPolicy:
    """网络策略：默认拒绝。判断一条命令是否可放行。"""

    def __init__(self, allow_network: bool = False) -> None:
        self.allow_network = allow_network

    def allows(self, command: list[str]) -> bool:
        """允许即 True。禁网模式(default)下包含网络工具则拒绝。"""
        if self.allow_network:
            return True
        haystack = " ".join(command)
        return _NETWORK_TOKENS.search(haystack) is None


class SandboxedExecutionBroker:
    """在 ExecutionBroker 上套一层沙箱隔离。

    用法（尽量贴近原 ExecutionBroker，好上手）：
        broker = SandboxedExecutionBroker(spec=SandboxSpec(), inner=ExecutionBroker())
        result = broker.execute(["ls"], workspace_input="/some/readonly/src")
    """

    def __init__(
        self,
        spec: SandboxSpec | None = None,
        inner: ExecutionBroker | None = None,
    ) -> None:
        self.spec = spec or SandboxSpec()
        self.inner = inner or ExecutionBroker(self.spec.budget)
        self.policy = NetworkPolicy(allow_network=self.spec.allow_network)

    def execute(
        self,
        command: list[str],
        *,
        workspace_input: str | Path | None = None,
    ) -> ExecutionResult:
        """在沙箱里执行一条命令。

        workspace_input：要"只读拷进临时工作区"的目录（可为空=None 表示空工作区）。
        """
        if not command:
            raise ValueError("command 不能为空")

        # 1) 网络策略（语义层）：默认禁网
        if not self.policy.allows(command):
            return ExecutionResult(
                command=" ".join(command),
                stdout="",
                stderr="[沙箱拒绝] 默认禁网：该命令疑似访问网络（NetworkPolicy）。",
                exit_code=None,
            )

        # 2) 只读工作区：把输入目录拷进临时目录
        workdir: str | None = None
        _tmp: tempfile.TemporaryDirectory[str] | None = None
        if self.spec.readonly_workspace:
            _tmp = tempfile.TemporaryDirectory(prefix="warden-sandbox-")
            workdir = _tmp.name
            if workspace_input is not None:
                src = Path(workspace_input)
                if src.is_dir():
                    _copy_tree_readonly(src, Path(workdir))
                elif src.is_file():
                    (Path(workdir) / src.name).write_bytes(src.read_bytes())

        try:
            return self.inner.execute(command, cwd=workdir)
        finally:
            if _tmp is not None:
                _tmp.cleanup()


def _copy_tree_readonly(src: Path, dst: Path) -> None:
    """把 src 目录拷进 dst，并把所有文件设为只读（ReadOnly）。"""
    for item in src.rglob("*"):
        if item.is_file():
            rel = item.relative_to(src)
            target = dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            # Windows 上设为只读属性；POSIX 上去掉写权限
            with contextlib.suppress(OSError):
                target.chmod(target.stat().st_mode & 0o444)
