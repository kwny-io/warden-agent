"""Git 控制平面：只读 revision 探测 + unified-diff 解析/应用 + 合并门禁。

  - revision   ：GitRevision 只读探测（commit/branch/detached/submodule），走受控执行跑系统 git。
  - patch      ：UnifiedPatchParser（统一 diff）+ PatchApplier（逐 hunk 应用，带 hash 校验）。
  - coordinator：GitWorktreeCoordinator 合并门禁（先校验基准 revision 再应用 patch）。
  - tools      ：给 build_agent / HTTP 层暴露的 git.apply_patch 工具。

设计边界：
本模块不是 Git SDK，不注册 git.* 模型工具；只提供"只读事实读取 + 干净的 patch 落地 + 门禁"。
"""

from warden_agent.git.coordinator import (
    GitWorktreeCoordinator,
    PatchApplyResult,
    PatchConflictCode,
    WorktreeMergeRequest,
    hash_file,
)
from warden_agent.git.patch import (
    FilePatch,
    PatchApplier,
    PatchConflict,
    PatchDocument,
    PatchHunk,
    PatchLine,
    UnifiedPatchParser,
)
from warden_agent.git.revision import (
    DirectGitProbe,
    ExecutionBrokerGitRevisionProbe,
    GitCommandContext,
    GitCommandContextError,
    GitRepositoryRef,
    GitRevision,
    GitRevisionProbe,
    make_git_context,
)
from warden_agent.git.tools import make_git_tools

__all__ = [
    "DirectGitProbe",
    "ExecutionBrokerGitRevisionProbe",
    "GitCommandContext",
    "GitCommandContextError",
    "GitRepositoryRef",
    "GitRevision",
    "GitRevisionProbe",
    "make_git_context",
    "FilePatch",
    "PatchApplier",
    "PatchConflict",
    "PatchDocument",
    "PatchHunk",
    "PatchLine",
    "UnifiedPatchParser",
    "GitWorktreeCoordinator",
    "PatchApplyResult",
    "PatchConflictCode",
    "WorktreeMergeRequest",
    "hash_file",
    "make_git_tools",
]
