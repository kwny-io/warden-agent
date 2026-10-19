"""`pre-push` 闸门必须真的会拦 —— 用假守卫证明它有牙。

为什么这条判据重要（2026-10-19 的事故）：一次推 3 个提交，GitHub 只为 tip 建 run，
中间两个提交永远拿不到自己的 CI run ⇒ 台账永远取证不了，只能改写历史。
`scripts/check_delivery_push.py` 早就能判出这件事，但它只在**人主动跑**时才说话；
这次把闸门挪到 git 的必经之路上（`.githooks/pre-push`）。

判据怎么算「有牙」：不是检查 hook 里有没有某段文字，而是**注入一个必红的假守卫**，
看 git 会不会真的被拦住；再注入一个必绿的假守卫，看它会不会误杀。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / ".githooks" / "pre-push"

ZERO = "0" * 40
SHA_A = "a" * 40
SHA_B = "b" * 40


def _shell() -> str:
    r"""找一个真能跑 POSIX 脚本的 sh/bash。

    为什么不能只用 `shutil.which("sh"|"bash")`（2026-10-19 实测）：在 **cmd.exe** 环境下，
    `C:\Windows\System32\bash.exe` 排在 PATH 前面，它是 **WSL 的转接桩**——直接跑只会报
    `WSL ... execvpe(/bin/bash) failed: No such file or directory`。后果不是跳过，而是三条用例
    全红，而且红在「钩子拦了但没印出『推送被拦住』」这种**误导**的地方：
    本机从 git-bash 跑全绿、从 cmd 跑全红 —— 判据变成了「你从哪个 shell 启动 pytest」的函数。

    所以：优先用 Git for Windows 自带的那份（由 `git --exec-path` 反推），再用 PATH 里的，
    但**排除 System32 下的 WSL 桩**。
    """
    candidates: list[str] = []
    if os.environ.get("WARDEN_SH"):
        candidates.append(os.environ["WARDEN_SH"])
    try:
        exec_path = subprocess.run(
            ["git", "--exec-path"], capture_output=True, text=True, encoding="utf-8", timeout=30
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        exec_path = ""
    if exec_path:
        # <Git>\mingw64\libexec\git-core → <Git>\bin\bash.exe / <Git>\usr\bin\sh.exe
        git_root = Path(exec_path).parents[2]
        candidates += [
            str(git_root / "bin" / "bash.exe"),
            str(git_root / "usr" / "bin" / "sh.exe"),
            str(git_root / "usr" / "bin" / "bash.exe"),
        ]
    candidates += [r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\sh.exe"]
    for name in ("sh", "bash"):
        found = shutil.which(name)
        if found:
            candidates.append(found)

    for cand in candidates:
        if not cand or not Path(cand).exists():
            continue
        # System32 下的是 WSL 转接桩，不是 Git 的 bash：跑任何脚本都会报 execvpe(/bin/bash)
        normalized = cand.replace("/", "\\").lower()
        if "\\windows\\system32\\" in normalized:
            continue
        probe = subprocess.run(
            [cand, "-c", "echo ok"], capture_output=True, text=True, timeout=60
        )
        if probe.returncode == 0 and "ok" in probe.stdout:
            return cand
    pytest.skip("没有能跑 POSIX 脚本的 sh/bash（System32\\bash.exe 是 WSL 桩，不算）")


def _run_hook(tmp_path: Path, stdin: str, guard: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["WARDEN_PUSH_GUARD"] = str(guard)
    env["PYTHONIOENCODING"] = "utf-8"
    env["GIT_DIR"] = str(REPO / ".git")  # 让钩子里的 git rev-parse 找到仓库
    return subprocess.run(
        [_shell(), str(HOOK)],
        cwd=REPO,
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def _fake_guard(tmp_path: Path, code: int, note: str) -> Path:
    """假守卫：把收到的参数写进文件，然后按要求的 rc 退出。"""
    log = tmp_path / "guard-args.txt"
    script = tmp_path / "guard.py"
    script.write_text(
        "import pathlib, sys\n"
        f"pathlib.Path({str(log)!r}).write_text(' '.join(sys.argv[1:]), encoding='utf-8')\n"
        f"print({note!r})\n"
        f"raise SystemExit({code})\n",
        encoding="utf-8",
    )
    return script


def test_钩子文件存在且交给判据脚本() -> None:
    assert HOOK.is_file(), "缺少 .githooks/pre-push：闸门没接到 git 上"
    text = HOOK.read_text(encoding="utf-8")
    assert "check_delivery_push.py" in text, "钩子没有调用形状判据"
    assert "--no-verify" in text, "钩子没有写清绕过方式（绕过必须是被记录的决定，不是静默）"


def test_判据说不行时真的拦住推送(tmp_path: Path) -> None:
    guard = _fake_guard(tmp_path, 3, "这次 push 会留下不可取证的提交")
    result = _run_hook(tmp_path, f"refs/heads/dev {SHA_B} refs/heads/dev {SHA_A}\n", guard)

    assert result.returncode == 1, (
        f"守卫说不行，钩子却放行了：rc={result.returncode} err={result.stderr!r}"
    )
    assert "推送被拦住" in result.stderr
    args = (tmp_path / "guard-args.txt").read_text(encoding="utf-8")
    assert f"--base={SHA_A}" in args and f"--tip={SHA_B}" in args, (
        f"钩子没有把 push 的真实范围交给判据：{args!r}"
    )


def test_判据说行时不要误杀(tmp_path: Path) -> None:
    guard = _fake_guard(tmp_path, 0, "")
    result = _run_hook(tmp_path, f"refs/heads/dev {SHA_B} refs/heads/dev {SHA_A}\n", guard)

    assert result.returncode == 0, f"守卫没报错，钩子却拦了：err={result.stderr!r}"


def test_删除分支与新建分支不该误报(tmp_path: Path) -> None:
    guard = _fake_guard(tmp_path, 0, "")
    # 第一行=删远端分支（本地 sha 全 0），第二行=新分支（远端 sha 全 0）
    stdin = (
        f"(delete) {ZERO} refs/heads/old {SHA_A}\n"
        f"refs/heads/feat {SHA_B} refs/heads/feat {ZERO}\n"
    )
    result = _run_hook(tmp_path, stdin, guard)

    assert result.returncode == 0, f"删分支/新分支被误杀：err={result.stderr!r}"
    assert "跳过推送形状校验" in result.stderr, "新分支要明说「没校验过」，不能假装校验过"
    assert not (tmp_path / "guard-args.txt").exists(), "删分支/新分支不该去调形状判据"
