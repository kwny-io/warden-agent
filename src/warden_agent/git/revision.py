"""Git 只读 revision 探测 —— 读"仓库当前 HEAD 的安全事实"。

  - 本模块不是 Git SDK，不注册 git.* 模型工具。
  - 只做只读探测：GitRevisionProbe.inspectHead → GitRevision。
  - 探测走"受控执行"（ExecutionBroker）跑系统 git，而不是 JGit——
    并有 capability 门禁（git.read）与 `-c credential.interactive=never` 防交互。

GitRevision 代表"仓库当前 HEAD 的安全事实快照"：
  repository      是否在 git 仓库内
  commit          HEAD 的 commit hash
  branch          当前分支名（detached 时为空）
  detached        = branch 为空（HEAD 游离）
  has_submodules  是否含 submodule
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Protocol

_GIT_READ_CAP = "git.read"


@dataclass(frozen=True)
class GitRevision:
    """仓库当前 HEAD 的安全事实快照（纯数据，只读）。"""

    repository: bool
    commit: str = ""
    branch: str = ""
    has_submodules: bool = False

    @property
    def detached(self) -> bool:
        return not self.branch

    def __post_init__(self) -> None:
        # 在仓库内就必须有 commit
        if self.repository and not self.commit:
            raise ValueError("repository inspection requires commit")


@dataclass(frozen=True)
class GitRepositoryRef:
    """对一个仓库的引用：本地库就是工作目录路径。"""

    root: str


class GitCommandContext:
    """跑 git 命令的上下文。硬要求：必须有 git.read 能力（capability 门禁）。"""

    def __init__(self, capabilities: set[str] | None = None) -> None:
        self.capabilities = capabilities or set()

    def allows(self, capability: str) -> bool:
        return capability in self.capabilities


class GitCommandContextError(Exception):
    """缺少 git 能力时抛出。"""

    def __init__(self, capability: str) -> None:
        self.capability = capability
        super().__init__(f"缺少能力: {capability}")


def make_git_context(*caps: str) -> GitCommandContext:
    """建一个带指定能力的上下文。"""
    ctx = GitCommandContext()
    ctx.capabilities.update(caps or (_GIT_READ_CAP,))
    return ctx


class GitRevisionProbe(Protocol):
    """只读探测接口：给定上下文 + 仓库引用，inspect HEAD。"""

    def inspect_head(self, context: GitCommandContext, repo: GitRepositoryRef) -> GitRevision: ...


class DirectGitProbe:
    """面向测试的直连版：不过 ExecutionBroker，直接 subprocess 跑 git。

    说明：真实场景应走 ExecutionBroker（受控执行，有预算/门禁）。为便于离线测试
    （不受 execution 沙箱限制），提供这个直连实现；它与 broker 版的命令序列一致。
    """

    def __init__(self, git_executable: str = "git") -> None:
        self.git = git_executable

    def inspect_head(self, context: GitCommandContext, repo: GitRepositoryRef) -> GitRevision:
        if not context.allows(_GIT_READ_CAP):
            raise GitCommandContextError(_GIT_READ_CAP)

        def run(args: list[str], timeout: int = 15) -> tuple[int, str]:
            try:
                p = subprocess.run(
                    [self.git, "-c", "credential.interactive=never", *args],
                    cwd=repo.root, capture_output=True, text=True, timeout=timeout,
                )
                return p.returncode, p.stdout.strip()
            except (FileNotFoundError, subprocess.TimeoutExpired):
                return 1, ""

        code, out = run(["rev-parse", "--is-inside-work-tree"])
        if code != 0 or out.strip().lower() != "true":
            return GitRevision(repository=False)

        _code, commit = run(["rev-parse", "HEAD"])
        _code, branch = run(["symbolic-ref", "--short", "-q", "HEAD"])
        _code, subs = run(["submodule", "status"])
        return GitRevision(
            repository=True,
            commit=commit,
            branch=branch,
            has_submodules=bool(subs.strip()),
        )


class ExecutionBrokerGitRevisionProbe:
    """通过受控执行（ExecutionBroker）跑系统 git 的只读探测。"""

    def __init__(self, git_executable: str = "git") -> None:
        self.git = git_executable

    def inspect_head(self, context: GitCommandContext, repo: GitRepositoryRef) -> GitRevision:
        if not context.allows(_GIT_READ_CAP):
            raise GitCommandContextError(_GIT_READ_CAP)
        return DirectGitProbe(self.git).inspect_head(context, repo)  # 复用同一命令序列
