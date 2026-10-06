"""「被直接执行的 shell 脚本，索引模式必须是 100755」守卫。

## 为什么需要（第六十九批现场）

`.github/workflows/ci.yml:524` 里签名 job 直接写：

    scripts/pin_image_digests.sh --app-image "$IMAGE"

**没有 `bash ` 前缀**，而 `git ls-files -s` 里 8 个 `scripts/*.sh` 全是 `100644`
⇒ 在 Linux runner 上这一行是 `Permission denied`，退出码 **126**。
它潜伏很久没被发现，是因为那个 job 的 `if:`（ci.yml:443-445）把 push / PR 全挡掉了
（只在 `push v* tag` 或 `workflow_dispatch` 上跑）——**只在"真要发布"那一刻炸**。

## 为什么守的是**索引模式**，不是 `os.access(X_OK)`

本仓 CI 在 Linux、开发机在 Windows：Windows 的工作区里根本没有可执行位
（`core.fileMode=false`），`os.access(..., X_OK)` 在那里是**假绿**。
git 索引里的 `100755` 才是"Git 会把什么写进树、runner checkout 出来是什么"的真值。

## 判据

凡是被"直接执行"的地方 ⇒ 那个脚本必须 `100755`：

* workflow：`run:` 行的**第一个 token** 就是 `scripts/*.sh`（`bash scripts/x.sh`、
  `uv run bash scripts/x.sh` **不算**——本仓两种写法都有，这里只钉"承诺了可执行"的那些）；
* 文档：以 `scripts/*.sh` 开头的一整行（允许 `./` 前缀）。`deploy/k8s/README.md:156`
  与 `docs/operations.md:368` 都是"照抄即用"的命令行 ⇒ 出事时手上不能只有一条
  `Permission denied`。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"

# 文档扫描要跳过的目录：依赖/产物里也有 .md，多到能拖慢整条测试
_SKIP_DIRS = frozenset(
    {
        "node_modules",
        ".git",
        ".venv",
        "dist",
        "build",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)

# workflow 的 `run:` 行里，第一个 token 就是脚本路径 ⇒ 直接执行
_DIRECT_IN_WORKFLOW = re.compile(r"^(?:\./)?(scripts/[\w./-]+\.sh)$")
# 文档里以脚本路径开头的命令行（允许 `./` 前缀、后面跟参数或行尾注释）
_DIRECT_IN_DOC = re.compile(r"^(?:\./)?(scripts/[\w./-]+\.sh)(?:\s|$)")

_EXEC_MODE = "100755"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="需要 git 才能读索引里的模式（工作区在 Windows 上没有可执行位这个概念）",
)


def _workflow_direct_scripts() -> dict[str, list[str]]:
    """{脚本路径: [调用点]}：workflow 里第一个 token 就是脚本的那些行。"""
    found: dict[str, list[str]] = {}
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        jobs = data.get("jobs") or {}
        for job_name, job in jobs.items():
            for index, step in enumerate(job.get("steps") or []):
                run = step.get("run")
                if not isinstance(run, str):
                    continue
                for line in run.splitlines():
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    match = _DIRECT_IN_WORKFLOW.match(stripped.split()[0])
                    if match:
                        where = f"{path.name}:{job_name}:steps[{index}]"
                        found.setdefault(match.group(1), []).append(where)
    return found


def _doc_direct_scripts() -> dict[str, list[str]]:
    """{脚本路径: [文档:行号]}：文档里以脚本路径开头的命令行。"""
    found: dict[str, list[str]] = {}
    for path in sorted(REPO.rglob("*.md")):
        if _SKIP_DIRS.intersection(path.relative_to(REPO).parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):  # 不是文本就别让守卫崩
            continue
        rel = path.relative_to(REPO).as_posix()
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            match = _DIRECT_IN_DOC.match(stripped)
            if match:
                found.setdefault(match.group(1), []).append(f"{rel}:{lineno}")
    return found


def _index_mode(relpath: str) -> str:
    """文件在 **git 索引**里的模式（`100755` / `100644` / …）。"""
    proc = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-s", "--", relpath],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert proc.returncode == 0, f"git ls-files 失败：{proc.stderr.strip()}"
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert lines, f"{relpath} 不在 git 索引里——没入库的脚本，CI checkout 不出来"
    return lines[0].split()[0]


def _assert_executable(where: dict[str, list[str]], what: str) -> None:
    assert where, (
        f"没找到任何「{what}」直接执行的脚本——本守卫就失去对象了。"
        "如果确实全改成 `bash scripts/x.sh` 了，请连同这条断言一起改（别让它变成永真）。"
    )
    bad: dict[str, tuple[str, list[str]]] = {}
    for relpath, callers in sorted(where.items()):
        assert (REPO / relpath).is_file(), f"{relpath} 被调用了，但文件不存在（调用点：{callers}）"
        mode = _index_mode(relpath)
        if mode != _EXEC_MODE:
            bad[relpath] = (mode, callers)
    detail = "\n".join(
        f"  {relpath} 索引模式={mode}  调用点={callers}" for relpath, (mode, callers) in bad.items()
    )
    assert not bad, (
        f"{what}里直接执行了没有可执行位的脚本（Linux runner 上 Permission denied / 126）：\n"
        + detail
        + "\n修法：git update-index --chmod=+x 路径"
        "\n（Windows 的工作区存不了 +x，索引模式才是 CI 的真值）"
    )


# --------------------------------------------------------------- 守卫本体


def test_workflow_里直接执行的脚本必须可执行() -> None:
    _assert_executable(_workflow_direct_scripts(), "workflow")


def test_文档里直接执行的脚本必须可执行() -> None:
    _assert_executable(_doc_direct_scripts(), "文档")


def test_守卫有鉴别力_分得清_644_与_755() -> None:
    """正面/负面对照各一条：证明这个测量不是在"永远返回真"。

    本仓已经吃过一次假绿的亏（security job 里那条"永真对照"），所以每条守卫都要有
    一条**会红**的对照。这里拿一个已知 644（README.md）与一个已知 755 当尺子。

    尺子**故意不拿** `scripts/pin_image_digests.sh`（它正是本守卫钉的对象）：那样一次违规
    会同时红两条，把真正的信号淹掉——这是第六十九批跑变异矩阵时实测撞到的
    （预期 1 条红、实际 2 条红，两条里有一条只是尺子被拿走了）。
    """
    # 不用守卫碰的那些脚本当 755 的尺子（它们都是 `bash scripts/x.sh` 调用，不要求 +x）
    assert _index_mode("README.md") == "100644"
    assert _index_mode("scripts/run_local.sh") == _EXEC_MODE
