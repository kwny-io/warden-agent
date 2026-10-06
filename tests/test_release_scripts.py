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
import re
import shlex
import shutil
import socket
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PIN_SCRIPT = REPO_ROOT / "scripts" / "pin_image_digests.sh"
ROLLBACK_SCRIPT = REPO_ROOT / "scripts" / "rollback.sh"
# 真集群演练脚本（第五十八批）：它自己不是被测对象，但必须能过 `bash -n` 且不许有 CRLF，
# 也不许用反引号（见 test_脚本里不许用反引号）。
DRILL_SCRIPT = REPO_ROOT / "scripts" / "k8s_rollback_drill.sh"

# `cat <<'EOF'` / `<<EOF` / `<<-EOF` —— 引号 heredoc 里的反引号不会被展开（帮助文本里常写）
_HEREDOC_RE = re.compile(r"<<-?[ \t]*([\"']?)([A-Za-z_][A-Za-z0-9_]*)\1")
K8S_NAMESPACE_MANIFEST = REPO_ROOT / "deploy" / "k8s" / "namespace.yaml"

# Windows 上 Git Bash 的常见安装位置。
# 为什么需要这张表：`shutil.which("bash")` 在 Windows 上可能先命中
# `C:\Windows\System32\bash.exe` —— 那是 **WSL 的转发器**，文件名对、which 找得到，
# 但只要没装 WSL 发行版，任何调用都会以
# `execvpe(/bin/bash) failed: No such file or directory` 失败（退出码 1）。
_WINDOWS_BASH_FALLBACKS = (
    Path(r"C:\Program Files\Git\usr\bin\bash.exe"),
    Path(r"C:\Program Files\Git\bin\bash.exe"),
    Path(r"C:\Program Files (x86)\Git\usr\bin\bash.exe"),
    Path(r"C:\Program Files (x86)\Git\bin\bash.exe"),
)


def _usable_bash(candidate: str | None) -> bool:
    """这个 bash 是**真的能跑**，还是只"文件存在"？"""
    if not candidate:
        return False
    try:
        probe = subprocess.run([candidate, "-c", "exit 0"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


def _coreutils_dir(bash: str | None) -> str | None:
    """返回一个确实有 `sed` 的目录（就是那个 bash 自带 coreutils 的地方）。

    Git for Windows 有两层：`Git\\bin\\bash.exe`（包装）与 `Git\\usr\\bin\\`（真身 + coreutils）。
    两个候选都**查文件**而不是猜路径：猜错了会以「脚本里的 `sed` 是 127」的形式回来。
    """
    if not bash:
        return None
    parent = Path(bash).resolve().parent
    for candidate in (parent, parent.parent / "usr" / "bin"):
        if any((candidate / name).is_file() for name in ("sed", "sed.exe")):
            return str(candidate)
    return None


def _child_path(narrowed: str | None) -> str:
    """给子 bash 构造 PATH：**自己把工具链放进去**，不继承宿主的路。

    为什么不能直接继承 `os.environ["PATH"]`：从 cmd.exe / PowerShell 启动 pytest 时，
    PATH 里通常只有 `Git\\cmd`，没有 `Git\\usr\\bin` —— bash 能跑，脚本里的 `sed`
    却是 127（command not found）。那种失败看起来像"发布脚本坏了"，
    实际上只是测试没把工具链给它（而 CI 的 ubuntu 上看不到，因为 `/bin` 本来就在 PATH 里）。
    """
    base = narrowed if narrowed is not None else os.environ.get("PATH", "")
    coreutils = _coreutils_dir(BASH)
    return f"{coreutils}{os.pathsep}{base}" if coreutils else base


def _find_bash() -> str | None:
    """找一个真能跑的 bash；找不到就返回 None（调用方据此整体 skip）。

    只用 `shutil.which("bash")` 是不够的，那会造出一个
    **"结论取决于你怎么启动 pytest"** 的测试：同一次提交、同一台机器，
    从 cmd/PowerShell 启动是 18 failed，从 Git Bash 启动是 18 passed，
    差别只来自 PATH 里 `System32` 是否排在 Git 前面。

    测试的结论必须是**代码的函数**，不能是"你从哪个 shell 启动"的函数。
    而且被影响的这 18 个用例正是"只在发布/回滚真出事时才跑到的代码"的用例，
    无论假红灯还是静默 skip，代价都是让人从此不再信这个文件。

    "能跑"包含两件事：bash 本体能执行、它的 coreutils 能找到（见 `_coreutils_dir`）。
    """
    found = shutil.which("bash")
    candidates = (found, *(str(p) for p in _WINDOWS_BASH_FALLBACKS))
    for candidate in candidates:
        if candidate and _usable_bash(candidate) and _coreutils_dir(candidate):
            return candidate
    return None


BASH = _find_bash()

pytestmark = pytest.mark.skipif(
    BASH is None,
    reason=(
        "需要能跑的 bash（外加它的 coreutils）才能测这两个脚本：Windows 上装 Git Bash / WSL 即可。"
        "注意 System32\\bash.exe 是 WSL 转发器：没装发行版时它只是「存在」、跑不起来"
    ),
)

# 假命令吐出的确定性 digest：sha256:<63 个 0>7 —— 合法（64 位小写十六进制）且好断言。
FAKE_DIGEST = "sha256:" + ("0" * 63) + "7"

SESSION_TIMEOUT = 120


def _manifest_namespace(path: Path) -> str | None:
    """从清单里读出 `kind: Namespace` 那个对象的 `name`。

    这里**故意不复用脚本里的 awk**：守卫如果用被测代码自己的解析器去得出"期望值"，
    解析器写错时会两边一起错，测试照样绿。
    同一文件里还有 `warden-quota` / `warden-limits`（ResourceQuota / LimitRange），
    所以不能直接取"第一个 name:"，要先等 `kind: Namespace`。
    """
    in_namespace = False
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.rstrip() == "kind: Namespace":
            in_namespace = True
            continue
        if in_namespace and raw.strip() == "---":
            in_namespace = False
            continue
        if in_namespace and raw.startswith("  name:"):
            return raw.split(":", 1)[1].strip().strip("'\"")
    return None


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
    # 第六十批：演练脚本也要搬进来。它以前只被 `bash -n` 这一类用例用到，
    # 所以没进过 _make_repo；现在它的安全守卫要用假 docker 跑真脚本，
    # 漏了这一行会以「No such file or directory」的形式失败（而不是报一条测试逻辑错）。
    shutil.copy2(DRILL_SCRIPT, root / "scripts" / "k8s_rollback_drill.sh")
    # 清单也搬进副本：第五十四批之后 rollback.sh 的默认命名空间**来自清单**，
    # 所以一个没有 namespace.yaml 的假仓库测的是一条真实运行时不存在的路径。
    shutil.copy2(K8S_NAMESPACE_MANIFEST, root / "deploy" / "k8s" / "namespace.yaml")
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
    # 即使某条用例把 PATH 收窄，也先把这个 bash 的 coreutils 放回去：
    # 收窄 PATH 想表达的是「没有 docker/kubectl」，不是「没有 sed」。
    env["PATH"] = _child_path(path)
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


def _shim_wins_on_path(tool: str, path: str) -> tuple[bool, str]:
    """在**子 bash 实际拿到的 PATH** 上，函数替身是否仍然压过同名可执行文件。

    这是那批替身用例真正需要的前提。原来自检断言的是「有效 PATH 里 `which(tool)` 找不到」，
    而它**在 GitHub 的 ubuntu runner 上恒假**：`/usr/bin/docker` 存在，coreutils（`sed`）也在
    `/usr/bin` —— 「PATH 里没有 docker」与「PATH 里有 sed」在那里不可能同时成立
    （第六十五批实录：这条自检让 `quality` job 在 CI 里连红两次，而本机 Windows 全绿）。
    函数在命令查找中优先于 PATH，所以正确的判据是「`command -v` 打印的是**函数名**、不是路径」，
    它与宿主装了什么无关。
    """
    probe = subprocess.run(
        [BASH, "-c", f"{tool}() {{ :; }}\ncommand -v {tool}"],
        env={**os.environ, "PATH": path},
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=SESSION_TIMEOUT,
    )
    out = (probe.stdout or "").strip()
    return (probe.returncode == 0 and out == tool), out


def _no_tools_path(tmp_path: Path) -> str:
    """返回一个确定找不到 docker/kubectl 的 PATH 值（空目录），并当场自检。

    自检要查**子 bash 实际拿到的那个 PATH**（`_child_path` 会在前面塞 coreutils），
    否则「一个什么都没有的空目录」这句话可能已经不成立了，
    而症状会是那几个用例「靠错误的原因通过」。
    """
    empty = tmp_path / "path-with-no-tools"
    empty.mkdir(exist_ok=True)
    path = str(empty)
    effective = _child_path(path)
    for tool in ("docker", "kubectl"):
        assert shutil.which(tool, path=path) is None, (
            f"脚手架自检失败：这条 PATH 里居然能找到 {tool} —— \"没有工具\"的前提不成立"
        )
        ok, out = _shim_wins_on_path(tool, effective)
        assert ok, (
            f"脚手架自检失败：子 bash 的 PATH 上，函数替身没能压过真实 {tool}"
            f"（`command -v {tool}` 打出 {out!r}，应当是函数名 {tool!r}）"
            " —— 那批替身用例会去调真工具，结论就不可信了"
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
    *"get deployment"*"-o name"*)
      # 存在性探测（第五十四批新增）。脚本原来从不问"这个 Deployment 在不在"，
      # 于是把 kubectl 的失败和"确实没有更早的版本"说成了同一句话。
      rc="${GET_RC-0}"
      if [ "$rc" != "0" ]; then
        printf '%s\n' \
          "${GET_ERR-Error from server (NotFound): deployments.apps \"warden-agent\" not found}" >&2
        return "$rc"
      fi
      printf 'deployment.apps/%s\n' "${DEPLOYMENT-warden-agent}"
      return 0 ;;
    *"rollout history"*)
      printf '%s\n' "REVISION  CHANGE-CAUSE" "${HISTORY_ROWS:?}"
      return 0 ;;
    *"rollout undo"*)      return "${UNDO_RC:-0}" ;;
    *"rollout status"*)    return "${STATUS_RC:-0}" ;;
    # 注意用 `${CURRENT_REVISION-3}` 而不是 `${CURRENT_REVISION:-3}`：
    # "空串"在本用例里是一个**有意义的状态**（存在但注解读不到），`:-` 会把它换成 3。
    *revision*)            echo "${CURRENT_REVISION-3}"; return 0 ;;
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


# --------------------------------------------------------------------------- #
# 第五十四批：出事时唯一那条命令，指向了一个不存在的命名空间
#
# 事实链：`scripts/rollback.sh` 的默认 NAMESPACE 曾是 `warden-agent`，
# 而 `deploy/k8s/namespace.yaml` 声明的是 `warden`（发布手册 deploy/k8s/README.md
# 的 11 条 apply 和手工回滚步骤也通篇是 `-n warden`）。这件事故意不好发现：
# 它只在"真出事"时被走到，而表现不是"脚本报错"，是**一句假话** ——
# 「没有可回滚的历史版本」，配上契约里写着"不做任何改动"的退出码 2。
#
# 为什么原来的 8 条 rollback 用例全绿都没拦住：它们都用替身 kubectl 并显式
# 给出 NAMESPACE/REVISION，**从来没人问过"不传 NAMESPACE 时会用哪个"**。
# 下面四条守的就是这个空白：默认值必须来自清单、清单变了要跟着变、
# 且三种"看不到"的情形都不能说成"没有历史版本"。
# --------------------------------------------------------------------------- #
# 旧版把"连当前版本都没看到"说成的那一句话（退出码 2 + 这句话 = 值班的人会放弃回滚）。
# 断言"这句话不许出现"而不是断言"某个词不许出现"：现在的错误文案会**引述**这句话
# 告诉使用者"这不是那个意思"（引述是好事），所以只能钉在旧的肯定句式上。
_MISLEADING_DIAGNOSIS = "没有可回滚的历史版本（"


def test_rollback默认命名空间必须等于清单里声明的那个(tmp_path: Path) -> None:
    manifest_ns = _manifest_namespace(K8S_NAMESPACE_MANIFEST)
    assert manifest_ns, "清单里读不出 kind: Namespace 的 name"

    root = _make_repo(tmp_path)
    proc = _run(
        root,
        "rollback.sh",
        prelude=KUBECTL_STUB,
        env_extra={"HISTORY_ROWS": "1         <none>\n2         <none>", "CURRENT_REVISION": "2"},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    gets = [c for c in _calls(root) if "get deployment" in c]
    assert gets, _calls(root)
    wrong = [c for c in gets if f"-n {manifest_ns} " not in c]
    assert not wrong, (
        f"不传 NAMESPACE 时脚本没在清单声明的命名空间（{manifest_ns}）里操作：{wrong}\n"
        f"这就是第五十四批：清单/发布手册是一种事实，脚本里的默认值是另一种，"
        f"两者不一致时脚本会在出事时说「没有可回滚的历史版本」。"
    )


def test_rollback读清单而不是把命名空间写死(tmp_path: Path) -> None:
    """只断言"默认值等于 warden"的守卫，遇到"把读清单那段删掉、写死当前值"会照样绿。

    所以这条把副本里的清单改掉，看脚本跟不跟 —— 它才真正锁住"清单是事实源"。
    """
    root = _make_repo(tmp_path)
    manifest = root / "deploy" / "k8s" / "namespace.yaml"
    text = manifest.read_text(encoding="utf-8")
    assert "  name: warden\n" in text, "清单的形状变了，这条用例的前提要重新确认"
    patched = text.replace("  name: warden\n", "  name: staging-ns\n", 1)
    manifest.write_text(patched, encoding="utf-8")

    proc = _run(
        root,
        "rollback.sh",
        prelude=KUBECTL_STUB,
        env_extra={"HISTORY_ROWS": "1         <none>\n2         <none>", "CURRENT_REVISION": "2"},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert any("-n staging-ns " in c for c in _calls(root)), _calls(root)


def test_rollback清单缺失时不再猜一个命名空间(tmp_path: Path) -> None:
    """读不出事实源时**必须停下要值**，不能回退到一个"大概对"的默认值。

    猜出来的默认值在平时永远是绿的，在出事时才会骗人——这正是上一轮的形状。
    """
    root = _make_repo(tmp_path)
    (root / "deploy" / "k8s" / "namespace.yaml").unlink()
    proc = _run(root, "rollback.sh", prelude=KUBECTL_STUB)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "NAMESPACE=" in proc.stderr, proc.stderr
    assert "清单" in proc.stderr, proc.stderr
    assert not [c for c in _calls(root) if "rollout undo" in c], _calls(root)


def test_rollback看不到deployment时不说没有历史版本(tmp_path: Path) -> None:
    """命名空间/名字/权限任何一种不对 → 说"连当前版本都没看到"，并带上 kubectl 的原话。

    旧行为是统统说成"没有可回滚的历史版本"（退出码 2），而 2 的契约是"不做任何改动"——
    值班的人会得出"这版回不了、只能往前修"的结论，而历史其实就在隔壁命名空间里。
    """
    root = _make_repo(tmp_path)
    proc = _run(
        root,
        "rollback.sh",
        prelude=KUBECTL_STUB,
        env_extra={
            "GET_RC": "1",
            "GET_ERR": 'Error from server (NotFound): deployments.apps "warden-agent" not found',
        },
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "拿不到 Deployment" in proc.stderr
    # 把 kubectl 的原话带上：出事时人需要的是错误原文，不是我的转述
    assert "NotFound" in proc.stderr
    # 本人在这条命令里最需要看到的东西：日志里出现过的命名空间
    assert "namespace=warden" in proc.stderr, proc.stderr
    # 第五十八批真集群演练抳到的那条：错误路径里写了文档式的 `$manifest_ns`，
    # 反引号在 shell 里是**命令替换** → 屏幕上多一行 `warden: command not found`，
    # 而「清单里声明的是 ___」后面是空的 —— 出事时最该看到的值消失了。
    assert "清单里声明的是 warden" in proc.stderr, proc.stderr
    assert "command not found" not in proc.stderr, proc.stderr
    assert _MISLEADING_DIAGNOSIS not in proc.stderr, (
        "看不到 Deployment 时做出了「没有历史版本」这个诊断 —— 这就是把人带偏的那句话"
    )
    assert not [c for c in _calls(root) if "rollout undo" in c], _calls(root)


def test_rollback读不到revision注解时也不说没有历史版本(tmp_path: Path) -> None:
    """"存在，但注解读不到"与"确实没有更早的版本"要分开说（退出码 1 vs 2）。"""
    root = _make_repo(tmp_path)
    proc = _run(
        root,
        "rollback.sh",
        prelude=KUBECTL_STUB,
        env_extra={"CURRENT_REVISION": ""},
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "revision 注解" in proc.stderr, proc.stderr
    assert _MISLEADING_DIAGNOSIS not in proc.stderr, proc.stderr
    assert not [c for c in _calls(root) if "rollout undo" in c], _calls(root)


# =========================================================================== #
# 静态约束：两个脚本都要能过 `bash -n`，且不许有 CRLF 行尾
# =========================================================================== #
@pytest.mark.parametrize("script", [PIN_SCRIPT, ROLLBACK_SCRIPT, DRILL_SCRIPT])
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


@pytest.mark.parametrize("script", [PIN_SCRIPT, ROLLBACK_SCRIPT, DRILL_SCRIPT])
def _backtick_offenders(name: str, text: str) -> list[str]:
    """**未转义**的反引号在 shell 里是命令替换，不是 Markdown 引号（注释里随便写）。

    第五十八批在真集群上跑回滚脚本时，屏幕里冒出一行 `warden: command not found`，
    根因就是一条给值班人看的提示写了 `` `$manifest_ns` ``：变量被当成命令执行，
    提示里最该看到的那个值变成空白。它不会让脚本失败（退出码仍对），
    只是在那句最需要准确的话里把关键信息抹掉——所以它需要一条静态约束。

    三种合法写法（都不算违规）：注释行（没被执行过）；`\\`` 转义（双引号里故意要一个
    字面反引号）；**带引号的** heredoc（`cat <<'EOF'` 不做展开，那里写反引号是给人看的）。
    仓库里全部用 `$(...)` 做命令替换，所以剩下任何一种裸反引号都是手滑。

    ⚠️ **不带引号的 heredoc 不在此列**：`cat <<EOF` 里的 `` ` `` 照样会被命令替换执行。
    第六十批才发现：上一版把两种 heredoc 当成一回事（`_HEREDOC_RE` 里引号是可选的），
    于是下面这段逻辑对 `<<EOF` 的形式**完全聋**——实测那份文件进守卫得 `offenders == []`，
    而 bash 跑它真的会执行反引号里的东西（退出码还是 0）。
    现在引号是**必需捕获到**的（`([\"']?)` + 反向引用），无引号 heredoc 里的裸反引号继续受检。

    抽成函数是为了让它**自己也能被测**：守卫的判据如果只长在一条 `parametrize` 里，
    它聋了也没人知道（见 `test_反引号守卫自己不许聋`）。
    """
    offenders: list[str] = []
    heredoc_end: str | None = None
    heredoc_quoted = True
    for num, line in enumerate(text.splitlines(), start=1):
        if heredoc_end is not None:
            if line.strip() == heredoc_end:
                heredoc_end = None
                continue
            if heredoc_quoted:
                continue
            # 无引号 heredoc：这里面的反引号**会被命令替换**，必须继续往下判
        elif not line.lstrip().startswith("#"):
            found = _HEREDOC_RE.search(line)
            if found:
                heredoc_quoted = bool(found.group(1))
                heredoc_end = found.group(2)
                continue
        if not line.lstrip().startswith("#") and "`" in line.replace(chr(92) + "`", ""):
            offenders.append(f"{name}:{num}: {line.strip()}")
    return offenders


@pytest.mark.parametrize("script", [PIN_SCRIPT, ROLLBACK_SCRIPT, DRILL_SCRIPT])
def test_脚本里不许用反引号(script: Path) -> None:
    offenders = _backtick_offenders(script.name, script.read_text(encoding="utf-8"))
    assert not offenders, (
        "这些行里的反引号会被 shell 当命令替换执行（要写变量就用 ${var}）:\n" + "\n".join(offenders)
    )


def test_反引号守卫自己不许聋() -> None:
    """守卫的守卫：四种写法里哪些真会被 shell 执行，由 **bash 自己**说了算。

    为什么需要这条：第六十批发现上一版守卫把 `<<EOF`（无引号 heredoc）与 `<<'EOF'`
    当成一回事，于是对前者完全聋——**而它正是本仓库第五十八批那个缺陷的另一种写法**。
    静态守卫一旦聋了，它比没有更坏：它给出的是"这条已经守住了"的假结论。

    这条用例的所有期望值都不靠我读代码得出，而是拿 bash 真跑一遍看它有没有执行。

    **四行都必须过同一道「仪器体检」**：如果那次跑法自己就失败了（rc 非 0），
    「没看到标记」就不能当作「反引号没被执行」的证据。这个区别本批吃过两次：
      - 第一次：全量套件里前提断言红了，却只报一句「前提不成立」，把责任推给
        「守卫的必要性」——**一次误诊**；
      - 第二次：查出它为什么红——从 cmd.exe 启动 pytest 时，子 bash 的 PATH 里
        没有 coreutils，`cat` 是 127，heredoc 展开出来的标记被丢掉（假红）。
        单跑文件时反而绢，因为它从 Git Bash 启动。
    """
    # ⚠️ bash 里造一个"被反引号执行"的证据：替身函数（**只用 builtin**）+ 真身脚本
    cases = {
        "无引号 heredoc": ("cat <<EOF\n值是 `date` 的\nEOF\n", True),
        "引号 heredoc": ("cat <<'EOF'\n值是 `date` 的\nEOF\n", False),
        "注释行": ("# 说明：这里写 `date` 是给人看的\n", False),
        "转义反引号": ('echo "值是 \\`date\\` 的"\n', False),
    }
    for name, (body, should_run) in cases.items():
        ran, why = _bash_backtick_probe(body)
        assert "跑法自己就失败了" not in why, f"{name}：{why}"
        detail = (
            "反引号确实会被执行，而守卫必须抓住它（第五十八批的形状）"
            if should_run
            else "并没有被展开，而守卫必须放过它（误报会让人把守卫删掉）"
        )
        assert ran is should_run, f"{name}：bash 的实际行为与期望不符：{detail}。诊断：{why}"

    # 守卫的判定必须与 bash 的事实**一一对应**
    assert _backtick_offenders("x.sh", cases["无引号 heredoc"][0]), (
        "无引号 heredoc 里的裸反引号被守卫放过了 —— bash 会执行它，而出事时屏幕上会出现"
        "`...: command not found` 加一句缺值的话（第五十八批的形状）"
    )
    for name in ("引号 heredoc", "注释行", "转义反引号"):
        assert not _backtick_offenders("x.sh", cases[name][0]), (
            f"{name} 被误报：它不是缺陷，报它会让下一个人把守卫删掉"
        )


def _bash_backtick_probe(body: str) -> tuple[bool, str]:
    """把这段脚本交给真 bash 跑，看反引号有没有被当命令执行，并附上判定过程。

    三个必须留意的细节（全都是踩过的）：
      - 判据标记用 ASCII（`printf` 是 builtin）：它是**内部**凭据，没必要走
        "非 ASCII 经 argv 传给 MSYS bash"那条链——那正是本仓库一路在治的编码坑。
      - **必须把 coreutils 放回 PATH**（用本文件的 `_child_path`，与其他跑 bash 的
        辅助函数同一套纪律）：从 cmd.exe 启动 pytest 时 PATH 里没有 `Git\\usr\\bin`，
        于是 `cat` 是 127、heredoc 展开出来的标记被丢掉，结论变成"反引号没被执行"
        —— 一个**假**结论（第六十批全量套件就是这么红的）。
      - 所以除"有没有标记"之外还要看 **rc**：rc 非 0 说明**这次跑法自己就坏了**，
        那反引号那一行就无证据可言，调用方不能把它当成"没被执行"。
    """
    marker = "BACKTICK_RAN"
    child_path = _child_path(None)
    script = f"date() {{ printf %s {marker}; }}\n" + body
    proc = subprocess.run(
        [BASH, "-c", script],
        env={**os.environ, "PATH": child_path},
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=SESSION_TIMEOUT,
    )
    why = (
        f"bash={BASH!r} rc={proc.returncode} stdout={proc.stdout!r} "
        f"stderr={proc.stderr[:200]!r} child_PATH={child_path[:160]!r}"
    )
    if proc.returncode != 0:
        return False, f"这次跑法自己就失败了（rc={proc.returncode}）：{why}"
    return marker in proc.stdout, why


# =========================================================================== #
# 第六十批：演练脚本自己的安全约定
#
# 先有证据，再有这几条用例。把假 docker 放进 PATH 记下 argv（不碰真集群），
# 用 `DRILL_NS=warden`（清单声明的那个命名空间）跑演练，会看到脚本真的会下发：
#     kubectl -n warden set env deploy/warden-agent WARDEN_DRILL_MARK=1   ← 改生产负载模板
#     kubectl delete ns warden --wait=false                             ← 删生产命名空间
# 而那个命名空间里正跑着 postgres 与它 11 天的数据，而且**用例失败时也会删**
# （退出码 1 的语义只是"有用例不符合契约"）。演练脚本删掉生产命名空间，
# 是这套工具能犯的最贵的错。
#
# 所以这组用例断言的不是"输出好看"，而是**那几条命令在任何情况下都不许被下发**。
# =========================================================================== #
DRILL_DOCKER_STUB = r"""
docker() {
  printf 'docker %s\n' "$*" >> "${STUB_LOG:?}"
  case "$*" in
    # 命名空间"已存在、但没有 warden-drill=true 标签"这个状态由 LABEL_VALUE 控制
    *"jsonpath={.metadata.labels.warden-drill}"*) printf '%s' "${LABEL_VALUE-}"; return 0 ;;
    # "那个命名空间存不存在"由 NS_EXISTS 控制；
    # 注意模式里那个尾空格：脚本开头那句 `K get ns`（集群通不通）不能被它挡掉
    *"kubectl "*"get ns "*) [ "${NS_EXISTS-0}" = "1" ] && return 0 || return 1 ;;
    # 节点里的清单（脚本要核对它真被改写成演练命名空间）
    *"cat "*"deploy/k8s/namespace.yaml"*)
      printf 'kind: Namespace\n  name: %s\n' "${DRILL_NS}"
      return 0
      ;;
    # 清理失败（DELETE_FAIL=1 模拟）：“清理没做成”是真失败，不能只打一行日志就报成功
    *"delete ns"*) [ "${DELETE_FAIL-0}" = "1" ] && return 1 || return 0 ;;
  esac
  return 0
}
"""


def _drill_calls(root: Path) -> list[str]:
    return [c for c in _calls(root) if c.startswith("docker ")]


def test_演练脚本拒绝在清单声明的命名空间上跑(tmp_path: Path) -> None:
    """DRILL_NS 指向清单声明的那个 → 必须拒绝，且**一条危险命令都不许下发**。

    这条守的是整个演练脚本最贵的一个后果：删掉生产命名空间。
    """
    root = _make_repo(tmp_path)
    # 把副本清单改成与 DRILL_NS 同名，才能真的走到"与清单相同"那道守卫
    # （否则先被"名字里必须有 drill"拦住，测的就是另一条分支了）。
    manifest = root / "deploy" / "k8s" / "namespace.yaml"
    rewritten = manifest.read_text(encoding="utf-8").replace(
        "  name: warden\n", "  name: warden-drill\n", 1
    )
    manifest.write_text(rewritten, encoding="utf-8")
    proc = _run(
        root,
        "k8s_rollback_drill.sh",
        prelude=DRILL_DOCKER_STUB,
        env_extra={"DRILL_NS": "warden-drill"},
    )
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "拒绝执行" in proc.stdout, proc.stdout
    calls = _drill_calls(root)

    dangerous = [c for c in calls if "delete ns" in c or "set env" in c or "rollout undo" in c]
    assert not dangerous, (
        "拒绝执行之前/之后都不许下发这几条命令（删除命名空间、改负载模板、回滚）：\n"
        + "\n".join(dangerous)
    )


def test_演练脚本的名字必须自己说清它是演练(tmp_path: Path) -> None:
    """名字里没有 `drill` 就拒绝：这个脚本会删掉那个名字指向的命名空间。"""
    root = _make_repo(tmp_path)
    proc = _run(
        root,
        "k8s_rollback_drill.sh",
        prelude=DRILL_DOCKER_STUB,
        env_extra={"DRILL_NS": "staging-ns"},
    )
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "拒绝执行" in proc.stdout and "drill" in proc.stdout, proc.stdout
    assert not _drill_calls(root), f"名字不合格时不该去碰 docker：{_drill_calls(root)}"


def test_演练脚本不删不是自己创建的命名空间(tmp_path: Path) -> None:
    """已存在、且没有 `warden-drill=true` 标签的命名空间 → 拒绝，且不删。

    现实里这就是"另一个目标任务恰好用了这个名字"的形态：那个命名空间可能是别人
    正在用的，本演练无权为了自己的方便把它删掉。
    """
    root = _make_repo(tmp_path)
    proc = _run(
        root,
        "k8s_rollback_drill.sh",
        prelude=DRILL_DOCKER_STUB,
        env_extra={"DRILL_NS": "warden-drill", "NS_EXISTS": "1", "LABEL_VALUE": ""},
    )
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "拒绝执行" in proc.stdout, proc.stdout
    calls = _drill_calls(root)
    assert any("get ns" in c for c in calls), f"应当先问过那个命名空间存不存在：{calls}"
    assert not [c for c in calls if "delete ns" in c or "set env" in c], calls


def test_演练脚本自己创建的命名空间带标记(tmp_path: Path) -> None:
    """补一个反向控制：正常路径上"创建 + 打标签"是必须的。

    没有这条，上面三条用例只需要"什么都不做"就能全绿。
    """
    root = _make_repo(tmp_path)
    _run(
        root,
        "k8s_rollback_drill.sh",
        prelude=DRILL_DOCKER_STUB,
        env_extra={"DRILL_NS": "warden-drill"},
    )
    # 这份替身答不出"节点里的清单现在声明什么"，所以脚本会在核对那一步停下（这是对的）
    calls = _drill_calls(root)
    assert any("create ns warden-drill" in c for c in calls), (
        f"正常路径上应当创建演练命名空间：{calls}"
    )
    assert any("label ns warden-drill warden-drill=true" in c for c in calls), (
        f"创建后必须打上 warden-drill=true 标签（否则下次同名演练会被自己的守卫拦住）：{calls}"
    )


def test_演练脚本复用自己的演练命名空间时不删它(tmp_path: Path) -> None:
    """已存在、且带 `warden-drill=true` 标签 → **必须放行**，而且是"复用"，不许删。

    这条是变异 C 补出来的缺口：把守卫条件改成"标签必须等于命名空间名"之后，
    上面那几条用例**都不会红**（它们只覆盖"标签为空 / 不存在"这一种情形），
    于是"演练崩一次之后就再也跑不起来"（标签是 `true` 却被拒绕）这件事没人看着。
    复用而不是删除同样重要：这个命名空间不是本次演练创建的。
    """
    root = _make_repo(tmp_path)
    proc = _run(
        root,
        "k8s_rollback_drill.sh",
        prelude=DRILL_DOCKER_STUB,
        env_extra={"DRILL_NS": "warden-drill", "NS_EXISTS": "1", "LABEL_VALUE": "true"},
    )
    assert "拒绝执行" not in proc.stdout, (
        "带 warden-drill=true 标签的演练命名空间必须能复用"
        "（否则演练崩一次之后就再也跑不起来）：\n" + proc.stdout
    )
    assert "用例 1" in proc.stdout, f"放行后应当继续往下走用例：{proc.stdout}"
    calls = _drill_calls(root)
    assert not [c for c in calls if "delete ns" in c], (
        f"这个命名空间不是本次演练创建的，不许删：{calls}"
    )


def test_演练脚本清理失败时给非零退出码(tmp_path: Path) -> None:
    """清理没做成却报成功 —— 值班的人会以为演练环境已经收拾干净，而它还在集群里。

    这条是变异 D 补出来的：以前这条路径**没有常驻用例**（我当时的说法是"写不出便宜的用例"，
    其实是偷懒）。现在用假 docker 就能写：`DELETE_FAIL=1` 让 `kubectl delete ns` 返 1，
    命名空间由 `NS_EXISTS=0` 决定为"本次演练创建"，于是清理那一段真的会跑。
    """
    root = _make_repo(tmp_path)
    proc = _run(
        root,
        "k8s_rollback_drill.sh",
        prelude=DRILL_DOCKER_STUB,
        env_extra={"DRILL_NS": "warden-drill", "NS_EXISTS": "0", "DELETE_FAIL": "1"},
    )
    assert "没删掉" in proc.stdout, f"清理失败必须说出来：{proc.stdout}"
    assert proc.returncode != 0, (
        f"清理没做成时退出码必须非 0（否则脚本在撒谎说收拾干净了）：{proc.returncode}\n"
        + proc.stdout
    )


# =========================================================================== #
# 转发脚本：「端口被占」不是「创建容器失败」
#   `bind()` 失败会让容器立刻退出，而 docker 只报一句「创建转发容器失败」。
#   值班的人会去翻 docker 日志找「脚本坏在哪」，而真正的事实是上一层：端口已经有人了。
# =========================================================================== #
_FORWARD_SCRIPT = REPO_ROOT / "scripts" / "kind_api_forward.py"


def test_转发脚本对端口已被占用说清事实() -> None:
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        proc = subprocess.run(
            [sys.executable, str(_FORWARD_SCRIPT)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            env={
                **os.environ,
                "LISTEN_PORT": str(port),
                "UPSTREAM": "127.0.0.1:1",
                # 子进程的中文输出必须固定成 UTF-8：本机控制台默认 cp936，否则这条断言
                # 比的是乱码（脚本真跑在容器里时是 UTF-8，`docker logs` 读得到）。
                "PYTHONIOENCODING": "utf-8",
            },
        )
    finally:
        listener.close()
    assert proc.returncode == 3, (
        f"端口被占时退出码应当是 3（既不是 0「正常」，也不是 1「脚本自己坏了」）："
        f"实际 {proc.returncode}\n{proc.stdout}{proc.stderr}"
    )
    assert "已被占用" in proc.stdout, f"必须把事实说出来：{proc.stdout}"
    assert "docker ps" in proc.stdout, "还要告诉人怎么确认（可能是上一次的转发容器还活着）"


# =========================================================================== #
# 元测试：bash 的可用性探测本身不许说谎
# =========================================================================== #
def test_wsl转发器不会被当成可用bash() -> None:
    """回归（第47批）：`System32\bash.exe` 存在但不可用时，探测必须继续往下找 Git Bash，
    而不是把它当 bash 交给 18 个用例去撞墙。

    这条用例故意**不依赖 BASH**：它要能在"探测写错"的那种机器上照样执行并变红，
    否则它就只是又一个会被同一个 bug 一起 skip 掉的摆设。
    """
    stub = Path(r"C:\Windows\System32\bash.exe")
    if not stub.is_file():
        pytest.skip(r"这台机器没有 System32\bash.exe（非 Windows / 无 WSL 转发器）")
    if _usable_bash(str(stub)):
        pytest.skip("这台机器的 WSL 转发器真能跑（装了发行版），验证不了这条回归")

    # 注意大小写：`shutil.which("bash")` 返回的是 `...\bash.EXE`，
    # 直接跟 `...\bash.exe` 比字符串会**比不相等而假装通过**（这正是最早写错的一版）。
    assert os.path.normcase(BASH or "") != os.path.normcase(str(stub)), (
        "把不可用的 WSL 转发器当成了 bash —— 本文件 18 个用例会以 "
        "`execvpe(/bin/bash) failed` 全红，而 CI（ubuntu）上看不到这个现象"
    )

    usable_fallbacks = [
        str(p) for p in _WINDOWS_BASH_FALLBACKS if p.is_file() and _usable_bash(str(p))
    ]
    assert BASH is not None or not usable_fallbacks, (
        f"机器上有能跑的 bash（{usable_fallbacks}）却整体 skip 了 —— "
        "等于把 18 个用例静默丢掉，这比红灯更坏"
    )
