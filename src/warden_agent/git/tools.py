"""给 build_agent / HTTP 层暴露的 git.apply_patch 工具。

模型侧 git 操作统一走受控执行/由 Gate 决定——这里暴露一个受门禁约束的
patch 应用入口：Agent 可提交一个 unified diff，先校验目标目录是 git 仓库 + 基准 hash，
通过才落地。不自动 commit/push。
"""

from __future__ import annotations

from warden_agent.git.coordinator import GitWorktreeCoordinator, WorktreeMergeRequest
from warden_agent.git.patch import UnifiedPatchParser
from warden_agent.git.revision import DirectGitProbe, GitRepositoryRef
from warden_agent.tool.catalog import ToolSpec, function_tool


def make_git_tools(
    workdir: str = ".",
    *,
    expected_base_commit: str | None = None,
) -> list[ToolSpec]:
    """造 git.apply_patch 技能卡：Agent 提交 unified diff，门禁校验后落地。

    - workdir              ：git 仓库根目录（默认当前目录）。
    - expected_base_commit ：期望的 HEAD commit（None=跳过基准校验，只校验是仓库）。
    """
    parser = UnifiedPatchParser()
    coordinator = GitWorktreeCoordinator()
    probe = DirectGitProbe()  # 直连版（避免 execution 沙箱限制）；真实场景可换 ExecutionBroker 版

    @function_tool(
        "git.apply_patch",
        "把一段 unified diff 应用到当前 git 仓库的工作区。必须先校验仓库存在且基准一致才落地。",
        {"type": "object",
         "properties": {"diff": {"type": "string", "description": "unified diff 文本"}},
         "required": ["diff"]},
        pure=False,
    )
    def apply_patch(diff: str) -> str:
        doc = parser.parse(diff)  # PatchConflict 会向上抛
        req = WorktreeMergeRequest(
            repo=GitRepositoryRef(workdir),
            expected_parent_revision=expected_base_commit,
            patch=doc,
        )
        result = coordinator.merge(req, probe=probe)
        if result.code.name == "OK":
            return f"已应用 {len(result.changed or [])} 个文件: {', '.join(result.changed or [])}"
        return f"[拒绝] {result.code.name}: {result.reason}"

    return [apply_patch]
