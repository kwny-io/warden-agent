"""Git patch 合并门禁：先校验基准 revision/commit，再应用已校验的 patch。

  - 不执行 fetch/commit/push/reset/clean/auto-merge。
  - Worktree 合并前必须先验证"父仓库当前 revision == 期望 revision" 且
    "HEAD commit == 期望 base commit"，再显式应用已校验的 patch。
  - PatchApplyResult：成功 / REVISION_CONFLICT / 应用冲突。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path

from warden_agent.git.patch import PatchApplier, PatchConflict, PatchDocument
from warden_agent.git.revision import GitRepositoryRef, GitRevisionProbe, make_git_context


class PatchConflictCode(Enum):
    OK = auto()
    REVISION_CONFLICT = auto()   # 基准 revision/commit 与期望不符
    PATCH_CONFLICT = auto()      # patch 应用冲突
    NOT_A_REPOSITORY = auto()    # 目标目录不是 git 仓库（无法校验基准）


@dataclass
class WorktreeMergeRequest:
    """一次合并操作：父仓库 + 期望基准 + patch 文档。"""

    repo: GitRepositoryRef
    expected_parent_revision: str | None   # 期望的父 revision（commit hash）
    patch: PatchDocument


@dataclass
class PatchApplyResult:
    code: PatchConflictCode
    sha256: str = ""
    changed: list[str] | None = None
    reason: str = ""


class GitWorktreeCoordinator:
    """合并门禁：先校验基准，再应用 patch。"""

    def __init__(self) -> None:
        self.applier = PatchApplier()

    def merge(self, request: WorktreeMergeRequest, *, probe: GitRevisionProbe) -> PatchApplyResult:
        """合并：先校验父仓库 HEAD == 期望 revision，再对工作区应用 patch。

        probe：GitRevisionProbe（inspect_head(context, repo)）。
        """
        # 1) 校验基准 revision
        ctx = make_git_context()
        rev = probe.inspect_head(ctx, request.repo)
        if not rev.repository:
            return PatchApplyResult(PatchConflictCode.NOT_A_REPOSITORY,
                                   reason="目标目录不是 git 仓库")
        if request.expected_parent_revision is not None and \
                rev.commit != request.expected_parent_revision:
            return PatchApplyResult(
                PatchConflictCode.REVISION_CONFLICT,
                reason=f"期望基准 {request.expected_parent_revision}，实际 HEAD {rev.commit}",
            )

        # 2) 应用 patch
        try:
            changed = self.applier.apply(request.patch, request.repo.root)
        except PatchConflict as e:
            return PatchApplyResult(
                PatchConflictCode.PATCH_CONFLICT,
                sha256=request.patch.sha256,
                reason=e.reason,
            )
        return PatchApplyResult(PatchConflictCode.OK,
                                sha256=request.patch.sha256,
                                changed=changed)


def hash_file(path: str) -> str:
    """算一个文件的 sha256（用于 expected_hashes 校验）。"""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
