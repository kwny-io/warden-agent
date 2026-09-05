"""Git 集成测试：revision 探测、unified-diff 应用、合并门禁、git 工具。"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

from warden_agent.git.coordinator import (
    GitWorktreeCoordinator,
    PatchConflictCode,
    WorktreeMergeRequest,
)
from warden_agent.git.patch import PatchDocument, UnifiedPatchParser
from warden_agent.git.revision import (
    DirectGitProbe,
    GitCommandContextError,
    GitRepositoryRef,
    GitRevision,
    make_git_context,
)
from warden_agent.tool.catalog import ToolCatalog


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True,
                       timeout=10, check=True)
        return True
    except Exception:
        return False


_NEEDS_GIT = pytest.mark.skipif(not _git_available(), reason="需要 git")


def _make_repo(tmp: Path) -> Path:
    """造一个临时 git 仓库，含一个已提交文件。"""
    subprocess.run(["git", "init", "-q"], cwd=tmp, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp, check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp, check=True,
                   capture_output=True)
    (tmp / "greet.txt").write_text("hello\nworld\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp, check=True,
                   capture_output=True)
    return tmp


# ---- GitRevision 快照 ----
def test_git_revision_detached() -> None:
    rev = GitRevision(repository=True, commit="abc", branch="")
    assert rev.detached
    assert rev.commit == "abc"


def test_git_revision_requires_commit_if_repo() -> None:
    with pytest.raises(ValueError):
        GitRevision(repository=True, commit="")


# ---- revision 探测（直连版，读真实仓库）----
@_NEEDS_GIT
def test_probe_inspects_head() -> None:
    with tempfile.TemporaryDirectory() as td:
        repo = _make_repo(Path(td))
        probe = DirectGitProbe()
        ctx = make_git_context()
        rev = probe.inspect_head(ctx, GitRepositoryRef(str(repo)))
        assert rev.repository
        assert len(rev.commit) == 40  # sha1
        assert rev.branch  # 应在某个分支上（默认 main/master）


@_NEEDS_GIT
def test_probe_non_repo() -> None:
    with tempfile.TemporaryDirectory() as td:
        probe = DirectGitProbe()
        rev = probe.inspect_head(make_git_context(), GitRepositoryRef(td))
        assert not rev.repository


@_NEEDS_GIT
def test_probe_requires_git_read_capability() -> None:
    from warden_agent.git.revision import GitCommandContext
    ctx_bad = GitCommandContext(capabilities={"other"})
    probe = DirectGitProbe()
    with pytest.raises(GitCommandContextError):
        probe.inspect_head(ctx_bad, GitRepositoryRef("."))


# ---- unified diff 解析 ----
def test_parse_basic_add() -> None:
    diff = (
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -0,0 +1,2 @@\n"
        "+line1\n"
        "+line2\n"
    )
    doc: PatchDocument = UnifiedPatchParser().parse(diff)
    assert len(doc.files) == 1
    assert doc.files[0].target_path == "README.md"
    assert len(doc.files[0].hunks[0].lines) == 2


def test_parse_modify_with_context() -> None:
    diff = (
        "--- a/a.txt\n"
        "+++ b/a.txt\n"
        "@@ -1,3 +1,3 @@\n"
        " context\n"
        "-old\n"
        "+new\n"
        " context\n"
    )
    doc = UnifiedPatchParser().parse(diff)
    kinds = [ln.kind for ln in doc.files[0].hunks[0].lines]
    assert kinds == [" ", "-", "+", " "]


def test_parse_document_sha256() -> None:
    diff = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-1\n+2\n"
    doc = UnifiedPatchParser().parse(diff)
    assert doc.sha256.startswith("sha256:")
    assert len(doc.sha256) == len("sha256:") + 64


# ---- patch 应用 ----
def test_apply_add_new_file(tmp_path: Path) -> None:
    diff = (
        "--- /dev/null\n"
        "+++ b/new.txt\n"
        "@@ -0,0 +1,2 @@\n"
        "+a\n"
        "+b\n"
    )
    doc = UnifiedPatchParser().parse(diff)
    from warden_agent.git.patch import PatchApplier
    PatchApplier().apply(doc, str(tmp_path))
    content = (tmp_path / "new.txt").read_text(encoding="utf-8")
    assert content == "a\nb"


def test_apply_modify(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("line1\nOLD\nline3\n", encoding="utf-8")
    diff = (
        "--- a/a.txt\n+++ b/a.txt\n"
        "@@ -1,3 +1,3 @@\n"
        " line1\n-O\n+NEW\n"
        " line3\n"
    )
    doc = UnifiedPatchParser().parse(diff)
    from warden_agent.git.patch import PatchApplier
    PatchApplier().apply(doc, str(tmp_path))
    content = (tmp_path / "a.txt").read_text(encoding="utf-8")
    assert "NEW" in content
    assert "OLD" not in content


def test_apply_delete(tmp_path: Path) -> None:
    (tmp_path / "gone.txt").write_text("x\n", encoding="utf-8")
    diff = (
        "--- a/gone.txt\n+++ /dev/null\n"
        "@@ -1 +0,0 @@\n-x\n"
    )
    doc = UnifiedPatchParser().parse(diff)
    from warden_agent.git.patch import PatchApplier
    PatchApplier().apply(doc, str(tmp_path))
    assert not (tmp_path / "gone.txt").exists()


# ---- 合并门禁（GitWorktreeCoordinator）----
@_NEEDS_GIT
def test_coordinator_revision_gate_ok(tdir) -> None:
    tmp, commit = tdir
    doc = UnifiedPatchParser().parse(
        "--- a/greet.txt\n+++ b/greet.txt\n@@ -1,2 +1,2 @@\n hello\n-world\n+earth\n"
    )
    req = WorktreeMergeRequest(GitRepositoryRef(tmp), commit, doc)
    result = GitWorktreeCoordinator().merge(req, probe=DirectGitProbe())
    assert result.code == PatchConflictCode.OK
    assert "earth" in (tmp / "greet.txt").read_text(encoding="utf-8")


@_NEEDS_GIT
def test_coordinator_revision_conflict(tdir) -> None:
    tmp, _commit = tdir
    doc = UnifiedPatchParser().parse(
        "--- a/greet.txt\n+++ b/greet.txt\n@@ -1,2 +1,2 @@\n hello\n-world\n+earth\n"
    )
    req = WorktreeMergeRequest(GitRepositoryRef(tmp), "deadbeef", doc)
    result = GitWorktreeCoordinator().merge(req, probe=DirectGitProbe())
    assert result.code == PatchConflictCode.REVISION_CONFLICT


def test_coordinator_not_a_repo(tmp_path: Path) -> None:
    doc = UnifiedPatchParser().parse(
        "--- a/x\n+++ b/x\n@@ -0,0 +1 @@\n+a\n"
    )
    req = WorktreeMergeRequest(GitRepositoryRef(str(tmp_path)), None, doc)
    result = GitWorktreeCoordinator().merge(req, probe=DirectGitProbe())
    assert result.code == PatchConflictCode.NOT_A_REPOSITORY


# ---- git 工具（build_agent 集成）----
@_NEEDS_GIT
def test_git_tool_apply_patch(tdir) -> None:
    tmp, _commit = tdir
    from warden_agent.git import make_git_tools
    catalog = ToolCatalog()
    for spec in make_git_tools(str(tmp)):
        catalog.register(spec)
    diff = "--- a/greet.txt\n+++ b/greet.txt\n@@ -1,2 +1,2 @@\n hello\n-world\n+earth\n"
    out = str(catalog.execute("git.apply_patch", {"diff": diff}))
    assert "已应用" in out
    assert "earth" in (tmp / "greet.txt").read_text(encoding="utf-8")


@pytest.fixture
def tdir():
    """返回一个已初始化 git 仓库的 (路径, HEAD commit)。"""
    if not _git_available():
        pytest.skip("需要 git")
    with tempfile.TemporaryDirectory() as td:
        tmp = _make_repo(Path(td))
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp,
                                capture_output=True, text=True, check=True
                                ).stdout.strip()
        yield tmp, commit


@_NEEDS_GIT
def test_build_agent_git_能力式集成(tdir) -> None:
    """build_agent(git_workdir=...) 应把 git.apply_patch 工具装配进 Agent。"""
    tmp, _commit = tdir
    from warden_agent.agent import build_agent
    agent = build_agent(git_workdir=str(tmp))
    catalog = agent._new_session().catalog
    assert "git.apply_patch" in {t.name for t in catalog.all()}
    diff = "--- a/greet.txt\n+++ b/greet.txt\n@@ -1,2 +1,2 @@\n hello\n-world\n+earth\n"
    out = str(catalog.execute("git.apply_patch", {"diff": diff}))
    assert "已应用" in out
    assert "earth" in (tmp / "greet.txt").read_text(encoding="utf-8")
