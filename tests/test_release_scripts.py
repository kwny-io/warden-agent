"""发布/回滚脚本的行为测试（`scripts/pin_image_digests.sh`、`scripts/rollback.sh`）。

为什么这些测试值得存在：
  这两个脚本是**出事时才会被运行**的代码。它们平时不参与任何路径，所以一旦写错，
  不会有任何红灯提醒你——直到某次发布或某次回滚真的要用它。
  而且这两个脚本的**核心价值恰恰是失败时的行为**：解析不出 digest 时必须停、
  没有可回滚版本时必须什么都不做。所以下面测试的重点不是「成功路径能跑通」，
  而是「该失败的时候真的失败，且不留下半成品」。

测试手法（不需要 docker / 不需要 k8s 集群 / 离线可跑）：
  - 用 BASH_ENV 往子 bash 里注入替身函数（`docker` / `kubectl` / `timeout`）。
  - 假命令把每次调用追加到 STUB_LOG，于是「到底调了什么」可断言 —— 例如
    「没有可回滚版本时绝不能调用 rollout undo」这种要求，光看退出码是验不出来的。
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PIN_SCRIPT = REPO_ROOT / "scripts" / "pin_image_digests.sh"
ROLLBACK_SCRIPT = REPO_ROOT / "scripts" / "rollback.sh"

BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(
    BASH is None,
    reason="需要 bash 才能测这两个脚本（Windows 上装 Git Bash / WSL 即可）",
)

# 假命令吐出的确定性 digest：sha256:<63 个 0>7 —— 合法（64 位小写十六进制）且好断言。
FAKE_DIGEST = "sha256:" + ("0" * 63) + "7"

SESSION_TIMEOUT = 120


def _make_repo(tmp_path: Path, dockerfile_from: str = "FROM python:3.12-slim AS runtime") -> Path:
    """造一个最小的「仓库布局」。

    脚本靠自身路径推断 REPO_ROOT（`dirname $0/..`），所以必须真的放在 scripts/ 下，
    否则测试测的就不是它实际运行时的形态。
    """
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "deploy" / "k8s").mkdir(parents=True)
    shutil.copy2(PIN_SCRIPT, root / "scripts" / "pin_image_digests.sh")
    shutil.copy2(ROLLBACK_SCRIPT, root / "scripts" / "rollback.sh")
    (root / "Dockerfile").write_text(dockerfile_from + "\n", encoding="utf-8")
    (root / "deploy" / "k8s" / "deployment.yaml").write_text(
        "spec:\n  template:\n    spec:\n      containers:\n"
        "        - name: app\n          image: REPLACE_ME/warden-agent:0.1.0\n",
        encoding="utf-8",
    )
    return root


def _run(
    root: Path,
    script: str,
    *,
    prelude: str = "",
    args: Sequence[str] = (),
    env_extra: Mapping[str, str] | None = None,
    path: str | None = None,
) -> subprocess.CompletedProcess:
    """在临时仓库里跑 `scripts/<script>`，可注入替身函数。

    三个必须留意的细节（全都是踩过的）：
      - 替身用 **BASH_ENV** 注入（非交互 bash 启动时 source 它）。Windows 上没法造
        一个无扩展名、带执行位的假 docker，所以「假 PATH 目录」那条路走不通。
      - 脚本路径用**相对 cwd 的 POSIX 形式**：`C:\\Users\\...` 传进 bash 会被当转义序列。
      - 不能用 `text=True`：它按系统默认编码解码（中文 Windows 上是 GBK），
        而 bash 输出 UTF-8 中文 → UnicodeDecodeError。
    """
    tmp_path = root.parent
    env = dict(os.environ)
    # POSIX 形式：这个值会被写进 bash 重定向，反斜杠在 bash 里是转义符
    env["STUB_LOG"] = (tmp_path / "stub.log").as_posix()
    if path is not None:
        env["PATH"] = path
    if env_extra:
        env.update(env_extra)
    if prelude:
        env_file = tmp_path / "_bashenv.sh"
        env_file.write_text(prelude, encoding="utf-8", newline="\n")
        env["BASH_ENV"] = env_file.as_posix()
    script_arg = f"scripts/{script}"
    # 用 BASH 的**绝对路径**：有的用例把 PATH 收窄到"确定没有 docker/kubectl"，
    # 若这里靠 PATH 找 bash，`exec bash` 会以 127 挂掉——那测的就不是被测脚本了。
    body = (
        "exec "
        + shlex.quote(BASH)
        + " "
        + " ".join([shlex.quote(script_arg), *[shlex.quote(a) for a in args]])
    )
    return subprocess.run(
        [BASH, "-c", body],
        cwd=str(root),
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=SESSION_TIMEOUT,
    )


def _calls(root: Path) -> list[str]:
    """替身命令被调用的记录（一行一次）。"""
    log = root.parent / "stub.log"
    if not log.exists():
        return []
    return [line for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# "这台机器没装 docker/kubectl"怎么造出来才不会骗自己
#
# 旧写法是 path="/usr/bin:/bin"，注释写着"只留 Git Bash 自带的工具，确保没有真 docker"。
# 它在开发机（Windows）上碰巧是对的，在 GitHub runner 上完全不对：那里的 docker 就在
# /usr/bin、kubectl 也在系统 PATH 里。于是脚本根本没走到"找不到工具"那条分支，用例红了
# （实测 ci.yml：assert 0 == 2 / assert 2 == 1）。
# 教训：不要用**宿主 PATH**去表达"这台机器没装 X"——它表达的是"这台机器的 PATH 长这样"，
# 两者的差别恰好只会在 CI 上暴露。
# 现在改成：PATH 指向一个**空目录**（确定没有任何可执行文件），脚本真正需要的外部命令
# 用 bash 函数顶掉（函数在命令查找中优先于 PATH，所以不依赖宿主装了什么）。
# --------------------------------------------------------------------------- #
NO_TOOLS_PRELUDE = r"""
dirname() {
  local p="${1%/*}"
  [ "$p" = "$1" ] && p="."
  [ -z "$p" ] && p="/"
  printf '%s\n' "$p"
}
"""


def _no_tools_path(tmp_path: Path) -> str:
    """返回一个确定找不到 docker/kubectl 的 PATH 值（空目录），并当场自检。"""
    empty = tmp_path / "path-with-no-tools"
    empty.mkdir(exist_ok=True)
    path = str(empty)
    for tool in ("docker", "kubectl"):
        assert shutil.which(tool, path=path) is None, (
            f"脚手架自检失败：这条 PATH 里居然能找到 {tool} —— \"没有工具\"的前提不成立"
        )
    return path


# --------------------------------------------------------------------------- #
# 替身：docker（pin_image_digests.sh 只用它做两件事：查 buildx 版本、解析 digest）
#
# ⚠️ 必须连 `timeout` 一起替掉：脚本用 `timeout 60 docker …` 包了一层（防 registry
#    不可达时无限挂住），而 `timeout` 是**外部命令**——它在自己的子进程里按 PATH
#    找 docker，**绕过 bash 函数**。只替 docker 不替 timeout，测试就会静默地跑真
#    docker（真打 registry、真下载 manifest），而且几个用例会「靠错误的原因通过」。
#    替身里的 `"$@"` 是在 bash 内解析的，所以 docker 仍然会命中函数。
# --------------------------------------------------------------------------- #
DOCKER_STUB = r"""
timeout() {
  if [ "$1" = "--version" ]; then echo "timeout (GNU coreutils) 9.4"; return 0; fi
  shift
  "$@"
}
gtimeout() { shift; "$@"; }

docker() {
  printf 'docker %s\n' "$*" >> "${STUB_LOG:?}"
  if [ "$1" = "buildx" ] && [ "$2" = "version" ]; then
    echo "github.com/docker/buildx v0.17.0"
    return 0
  fi
  if [ "$1" = "buildx" ] && [ "$2" = "imagetools" ] && [ "$3" = "inspect" ]; then
    ref="$4"
    case "$ref" in
      *MISSING*) echo "ERROR: ${ref}: not found" >&2; return 1 ;;
      *WEIRD*)   echo "Warning: something odd happened" ; return 0 ;;
    esac
    echo "__DIGEST__"
    return 0
  fi
  echo "docker: unknown invocation: $*" >&2
  return 1
}
""".replace("__DIGEST__", FAKE_DIGEST)


# --------------------------------------------------------------------------- #
# 替身：kubectl（把每次调用记进日志，于是「到底调了什么」可断言）
# --------------------------------------------------------------------------- #
KUBECTL_STUB = r"""
kubectl() {
  printf 'kubectl %s\n' "$*" >> "${STUB_LOG:?}"
  case "$*" in
    *"rollout history"*)
      printf '%s\n' "REVISION  CHANGE-CAUSE" "${HISTORY_ROWS:?}"
      return 0 ;;
    *"rollout undo"*)      return "${UNDO_RC:-0}" ;;
    *"rollout status"*)    return "${STATUS_RC:-0}" ;;
    *revision*)            echo "${CURRENT_REVISION:-3}"; return 0 ;;
    *"spec.replicas"*)     echo "${SPEC_REPLICAS:-2}"; return 0 ;;
    *"readyReplicas"*)     echo "${READY_REPLICAS-2}"; return 0 ;;
  esac
  echo "kubectl: unhandled: $*" >&2
  return 1
}
"""


# =========================================================================== #
# pin_image_digests.sh
# =========================================================================== #
def test_pin_help_exits_zero(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    proc = _run(root, "pin_image_digests.sh", args=["--help"])
    assert proc.returncode == 0
    # 帮助里必须写明失败语义，否则没人知道退出码 3 是什么意思
    assert "退出码" in proc.stdout


def test_pin_writes_digest_into_dockerfile(tmp_path: Path) -> None:
    root = _make_repo(tmp_path, "FROM node:20-slim AS web-build")
    proc = _run(root, "pin_image_digests.sh", prelude=DOCKER_STUB)
    assert proc.returncode == 0, proc.stderr
    text = (root / "Dockerfile").read_text(encoding="utf-8")
    assert f"FROM node:20-slim@{FAKE_DIGEST} AS web-build" in text


def test_pin_is_idempotent(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    assert _run(root, "pin_image_digests.sh", prelude=DOCKER_STUB).returncode == 0
    first = (root / "Dockerfile").read_text(encoding="utf-8")
    assert _run(root, "pin_image_digests.sh", prelude=DOCKER_STUB).returncode == 0
    assert (root / "Dockerfile").read_text(encoding="utf-8") == first
    # 幂等的关键：不能出现 @sha256:...@sha256:... 这种叠加
    assert first.count(FAKE_DIGEST) == 1
    # 替身确实被用上了（防「真 docker 跑通也算过」的假绿灯）
    assert _calls(root)[0].startswith("docker buildx")


def test_pin_dry_run_changes_nothing(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    before = (root / "Dockerfile").read_text(encoding="utf-8")
    proc = _run(root, "pin_image_digests.sh", prelude=DOCKER_STUB, args=["--dry-run"])
    assert proc.returncode == 0, proc.stderr
    assert (root / "Dockerfile").read_text(encoding="utf-8") == before
    assert "dry-run" in proc.stdout


def test_pin_fails_closed_when_digest_unresolvable(tmp_path: Path) -> None:
    """解析不出来 → 退出码 3，且**一个字节都不能改**。

    这是整个脚本最重要的行为：「以为钉过了、其实没钉」比「没钉」危险得多——
    前者让流水线一路绿灯直到 Pod 起不来。
    """
    root = _make_repo(tmp_path, "FROM python:3.12-slim-MISSING AS runtime")
    before = (root / "Dockerfile").read_text(encoding="utf-8")
    proc = _run(root, "pin_image_digests.sh", prelude=DOCKER_STUB)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert (root / "Dockerfile").read_text(encoding="utf-8") == before
    # 报错要给出可执行的下一步，而不是只说「失败了」
    assert "补救方向" in proc.stderr


def test_pin_rejects_output_that_is_not_a_digest(tmp_path: Path) -> None:
    """`inspect` 成功但输出不是 digest（例如只吐了警告）→ 也必须停下。

    这条防的是最阴的一类 bug：把警告文本当 digest 写进 Dockerfile，
    语法上「有 @sha256:」，实际是垃圾。
    """
    root = _make_repo(tmp_path, "FROM python:3.12-slim-WEIRD AS runtime")
    before = (root / "Dockerfile").read_text(encoding="utf-8")
    proc = _run(root, "pin_image_digests.sh", prelude=DOCKER_STUB)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert (root / "Dockerfile").read_text(encoding="utf-8") == before
    # 报错必须是「不是合法 digest」，而不是碰巧别的错
    assert "合法 digest" in proc.stderr


def test_pin_app_image_replaces_placeholder(tmp_path: Path) -> None:
    """`--app-image` 把清单里的 REPLACE_ME 占位换成 真实引用@digest（同一个字符串）。"""
    root = _make_repo(tmp_path)
    proc = _run(
        root,
        "pin_image_digests.sh",
        prelude=DOCKER_STUB,
        args=["--app-image", "ghcr.io/acme/warden-agent:0.1.0"],
    )
    assert proc.returncode == 0, proc.stderr
    text = (root / "deploy" / "k8s" / "deployment.yaml").read_text(encoding="utf-8")
    assert f"image: ghcr.io/acme/warden-agent:0.1.0@{FAKE_DIGEST}" in text
    assert "REPLACE_ME" not in text


def test_pin_needs_docker_and_says_so(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    # PATH 指向空目录（而不是某个"看起来干净"的宿主路径）→ 必然走到"找不到 docker"分支
    proc = _run(
        root,
        "pin_image_digests.sh",
        path=_no_tools_path(tmp_path),
        prelude=NO_TOOLS_PRELUDE,
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "docker" in proc.stderr


# =========================================================================== #
# rollback.sh
# =========================================================================== #
def test_rollback_no_previous_revision_exits_2_and_does_nothing(tmp_path: Path) -> None:
    """只有当前这一版（没有历史）时 → 退出码 2，且**绝不能**调用 rollout undo。

    「没有历史却下发了一条 undo」是最糟的结果：它可能把当前版本也弄坏，
    而操作者以为自己在回滚。
    """
    root = _make_repo(tmp_path)
    proc = _run(
        root,
        "rollback.sh",
        prelude=KUBECTL_STUB,
        env_extra={"HISTORY_ROWS": "1         <none>", "CURRENT_REVISION": "1"},
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert not [c for c in _calls(root) if "rollout undo" in c], _calls(root)


def test_rollback_picks_previous_revision_and_verifies(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    proc = _run(
        root,
        "rollback.sh",
        prelude=KUBECTL_STUB,
        env_extra={
            "HISTORY_ROWS": "1         <none>\n2         <none>\n3         <none>",
            "CURRENT_REVISION": "3",
        },
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    undo = [c for c in _calls(root) if "rollout undo" in c]
    assert len(undo) == 1, undo
    assert "--to-revision=2" in undo[0]
    # 回滚后必须真的去查健康，而不是「下发完就当成功」
    assert [c for c in _calls(root) if "rollout status" in c]


def test_rollback_honours_explicit_revision(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    proc = _run(
        root,
        "rollback.sh",
        prelude=KUBECTL_STUB,
        env_extra={
            "HISTORY_ROWS": "1         <none>\n2         <none>\n3         <none>",
            "CURRENT_REVISION": "3",
            "REVISION": "1",
        },
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert any("--to-revision=1" in c for c in _calls(root))


def test_rollback_skips_revision_gaps(tmp_path: Path) -> None:
    """revision 号会跳号（历史被裁剪过），所以「上一个版本」不能算 current-1。"""
    root = _make_repo(tmp_path)
    proc = _run(
        root,
        "rollback.sh",
        prelude=KUBECTL_STUB,
        env_extra={"HISTORY_ROWS": "4         <none>\n9         <none>", "CURRENT_REVISION": "9"},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert any("--to-revision=4" in c for c in _calls(root))


def test_rollback_unhealthy_exits_3(tmp_path: Path) -> None:
    """回滚已下发但没起来 → 退出码 3（要求人介入），不是 0。

    这里返回 0 会让自动化流程继续往下走，而集群其实是坏的。
    """
    root = _make_repo(tmp_path)
    proc = _run(
        root,
        "rollback.sh",
        prelude=KUBECTL_STUB,
        env_extra={
            "HISTORY_ROWS": "1         <none>\n2         <none>",
            "CURRENT_REVISION": "2",
            "STATUS_RC": "1",
        },
    )
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "人工" in proc.stderr


def test_rollback_replicas_short_of_desired_exits_3(tmp_path: Path) -> None:
    """readyReplicas 在「没有 Pod ready」时会**被省略**（空串），不能当成 0==0 误判成功。"""
    root = _make_repo(tmp_path)
    proc = _run(
        root,
        "rollback.sh",
        prelude=KUBECTL_STUB,
        env_extra={
            "HISTORY_ROWS": "1         <none>\n2         <none>",
            "CURRENT_REVISION": "2",
            "SPEC_REPLICAS": "2",
            "READY_REPLICAS": "",
        },
    )
    assert proc.returncode == 3, proc.stdout + proc.stderr


def test_rollback_dry_run_does_not_undo(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    proc = _run(
        root,
        "rollback.sh",
        prelude=KUBECTL_STUB,
        env_extra={
            "HISTORY_ROWS": "1         <none>\n2         <none>",
            "CURRENT_REVISION": "2",
            "DRY_RUN": "1",
        },
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not [c for c in _calls(root) if "rollout undo" in c], _calls(root)
    assert "dry-run" in proc.stdout


def test_rollback_needs_kubectl_and_says_so(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    proc = _run(
        root,
        "rollback.sh",
        path=_no_tools_path(tmp_path),
        prelude=NO_TOOLS_PRELUDE,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "kubectl" in proc.stderr


# =========================================================================== #
# 静态约束：两个脚本都要能过 `bash -n`，且不许有 CRLF 行尾
# =========================================================================== #
@pytest.mark.parametrize("script", [PIN_SCRIPT, ROLLBACK_SCRIPT])
def test_script_syntax_and_line_endings(script: Path) -> None:
    assert script.exists(), f"脚本不存在: {script}"
    raw = script.read_bytes()
    assert b"\r\n" not in raw, f"{script.name} 含 CRLF 行尾——流水线 Linux 上会 shebang 失效"
    proc = subprocess.run(
        [BASH, "-n", str(script)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=SESSION_TIMEOUT,
    )
    assert proc.returncode == 0, proc.stderr
