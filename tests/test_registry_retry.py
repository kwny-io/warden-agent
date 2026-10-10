"""`scripts/registry_retry.sh` 的用例：不需要 docker、不需要 registry、离线可跑。

手法（与 `tests/test_release_scripts.py` 同一套 idiom，别另创）：
    - 用 **BASH_ENV** 往子 bash 里注入替身函数（`docker` / `sleep`）。Windows 上
      造不出「无扩展名 + 带执行位」的假 docker，所以「假 PATH 目录」那条路走不通。
    - **不能用 `text=True`**：它按系统默认编码解码（中文 Windows 上是 GBK），
      而 bash 输出 UTF-8 中文 ⇒ `UnicodeDecodeError`。统一 `encoding="utf-8", errors="replace"`。
    - 脚本路径传**相对 cwd 的 POSIX 形式**：`C:\\...` 进 bash 会被当转义序列。

为什么值得这些用例（真失败模式，不是凑数）：
    2026-10-09 提交 `f5901ac` 的 CI 首轮红，同一棵树的另一个提交全绿。两处现场都死在
    Docker Hub 匿名拉取限流（429）⇒ `failed to solve`：
      ① `sbom` 作业的 `docker build --pull`（`.github/workflows/ci.yml`）；
      ② `container` 作业真正跑构建的那一处 —— 在 `scripts/container_smoke.sh` 里。
         （`ci.yml` 里还有一处 `docker build --pull -t "$image"`，它属于 `image-signing` 作业；
           本批的注释与用例文案一度把它写成「container 作业」，是假话，已改。）
    所以这个脚本必须**能被证明**：抖动能被吃掉、真错**不许可**被重试（否则红会被拖慢、
    真原因被刷成一行行「第 N 次」）、退出码能把两种失败分开、stdout 不给上游下毒。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT_REL = "scripts/registry_retry.sh"
TIMEOUT_S = 120

# 本次事故的**原文**（取自提交 f5901ac 的 CI 日志，见 CHANGELOG「第七十七批」）：
# 它必须被判成「可重试」，否则这个脚本对本批要治的失败模式毫无作用。
INCIDENT_429 = (
    "#5 [internal] load metadata for docker.io/library/node:20-slim\n"
    "unexpected status from HEAD request to registry-1.docker.io/v2/library/node/manifests/"
    "20-slim: 429 Too Many Requests\n"
    "failed to solve: error getting credentials - err: exit status 1"
)
# 构造错了：绝不能被重试（重试只会让红更慢、把真原因刷成一行行「第 N 次」）
NOT_RETRYABLE = (
    "failed to solve: failed to read dockerfile: open Dockerfile: no such file or directory"
)

# 下面三条是**故意挑的会误伤的形状**。第一版判据拿 `429`、`EOF`、`no such host` 做全文匹配，
# 这三条全都会被误判成抖动 —— 于是构造错被重试三遍，日志还打印「这不是代码问题」，
# 把人的注意力从真原因上引开（归因反了）。它们必须 rc=2、且只跑一次。
PARSE_ERROR_ON_LINE_429 = (
    "failed to solve: dockerfile parse error on line 429: unknown instruction: RUNN"
)
TRUNCATED_DOCKERFILE = "failed to solve: failed to read dockerfile: unexpected EOF"
WRONG_REGISTRY_HOST = (
    'Error response from daemon: Get "https://registry.example.invalid/v2/": dial tcp: '
    "lookup registry.example.invalid: no such host"
)


def _usable_bash(candidate: str | None) -> bool:
    """必须排除 `C:\\Windows\\System32\\bash.exe`——那是 WSL 转发器，报
    `execvpe(/bin/bash) failed`，看着像被测脚本失败，其实根本没跑。"""
    if not candidate:
        return False
    p = Path(candidate)
    system32 = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32"
    try:
        return system32 not in p.resolve().parents
    except OSError:  # pragma: no cover - 路径解析失败就当不可用
        return False


def _bash() -> str:
    for cand in (shutil.which("bash"), r"C:\Program Files\Git\bin\bash.exe"):
        if candidate := cand if _usable_bash(cand) else None:
            return candidate
    raise AssertionError("找不到可用的 Git Bash（不能是 C:\\Windows\\System32\\bash.exe）")


STUB_ENV = r"""
# 替身 docker：按 MODE 造出我们要的失败形态，并把调用次数写进 $CNT
docker() {
  n=$(cat "$CNT" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$CNT"
  case "$MODE" in
    429_once)
      if [ "$n" -eq 1 ]; then printf '%s\n' "$ERR_TEXT" >&2; return 1; fi
      echo "build ok"; return 0 ;;
    always_429) printf '%s\n' "$ERR_TEXT" >&2; return 1 ;;
    not_retryable) printf '%s\n' "$ERR_TEXT" >&2; return 1 ;;
    digest) printf 'sha256:'; printf 'a%.0s' $(seq 64); printf '\n'; return 0 ;;
    # 第一次先往 stdout 吐半截「digest」再失败：失败尝试的 stdout 不许出现在最终输出里
    digest_after_junk)
      if [ "$n" -eq 1 ]; then
        printf 'sha256:deadbeef\n'; printf '%s\n' "$ERR_TEXT" >&2; return 1
      fi
      printf 'sha256:'; printf 'a%.0s' $(seq 64); printf '\n'; return 0 ;;
  esac
}
# 替身 sleep：把等待秒数记下来（真等 20s 会让用例变慢，也让退避无法被断言）
sleep() { printf '%s\n' "$*" >> "$SLEEP_LOG"; return 0; }
"""


class Run:
    """一次调用的全部可观测量。"""

    def __init__(self, proc: subprocess.CompletedProcess[str], tmp: Path) -> None:
        self.rc = proc.returncode
        self.out = proc.stdout
        self.err = proc.stderr
        cnt = tmp / "cnt"
        self.calls = int(cnt.read_text().strip() or 0) if cnt.exists() else 0
        sleep_log = tmp / "sleep.log"
        self.sleeps = sleep_log.read_text().split() if sleep_log.exists() else []


def _run(tmp_path: Path, mode: str, *, err_text: str, env_extra: dict[str, str] | None = None,
         argv: list[str] | None = None) -> Run:
    tmp_path.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(
        {
            "BASH_ENV": (tmp_path / "_bashenv.sh").as_posix(),
            "CNT": (tmp_path / "cnt").as_posix(),
            "SLEEP_LOG": (tmp_path / "sleep.log").as_posix(),
            "MODE": mode,
            "ERR_TEXT": err_text,
            "REGISTRY_RETRY_SLEEPS": "0 0",  # 真实等待交给替身 sleep 记录
            "PYTHONIOENCODING": "utf-8",
        }
    )
    if env_extra:
        env.update(env_extra)
    (tmp_path / "_bashenv.sh").write_text(STUB_ENV, encoding="utf-8", newline="\n")
    args = argv or ["docker", "build", "--pull", "-t", "x", "."]
    # **不用 shell=True**：Windows 上它走 cmd.exe，会把 POSIX 引号当字面量
    # （实测报「文件名、目录名或卷标语法不正确」），于是测的就不是被测脚本了。
    proc = subprocess.run(
        [_bash(), SCRIPT_REL, "--", *args],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=TIMEOUT_S,
    )
    return Run(proc, tmp_path)


# --- 1. 顺利路径 --------------------------------------------------------------


def test_一次成功_只调用一次_且stdout只含命令自己的输出(tmp_path: Path) -> None:
    """stdout 必须干净：上游会写 `digest=$(bash scripts/registry_retry.sh -- docker … inspect)`，
    一旦把「第 N 次尝试」这类日志写进 stdout，digest 就被污染了。"""
    r = _run(
        tmp_path, "digest", err_text="", argv=["docker", "buildx", "imagetools", "inspect", "x"]
    )
    assert r.rc == 0, r.err
    assert r.calls == 1
    assert r.out.strip() == "sha256:" + "a" * 64, r.out
    assert "registry_retry" not in r.out, "日志污染了 stdout"
    assert "[registry_retry] OK attempt=1/3" in r.err, "日志必须走 stderr，且带 ASCII 机器标记"


def test_失败尝试的stdout不许累积到最终输出里(tmp_path: Path) -> None:
    """第一次往 stdout 吐了半截 `sha256:deadbeef` 才失败，第二次成功 —— 最终 stdout
    只能是第二次那串。因为 `ci.yml` 是用 `grep -qE '^sha256:[0-9a-f]{64}$'` **逐行**校验
    digest 的：脏值混进去不会报错，只会带着一个假 digest 往下走。"""
    r = _run(tmp_path, "digest_after_junk", err_text=INCIDENT_429,
             argv=["docker", "buildx", "imagetools", "inspect", "x"])
    assert r.rc == 0, r.err
    assert r.calls == 2, f"第一次是可重试错误，必须重试；实际调用 {r.calls} 次"
    assert r.out.strip() == "sha256:" + "a" * 64, f"stdout 被失败尝试污染了：{r.out!r}"
    assert "deadbeef" not in r.out, "失败尝试的半截 stdout 不许出现在最终输出里"


# --- 2. 本次事故的原文必须被判成「可重试」 ------------------------------------


def test_事故原文的429被吃掉_第二次成功_且日志写明两次尝试(tmp_path: Path) -> None:
    r = _run(tmp_path, "429_once", err_text=INCIDENT_429)
    assert r.rc == 0, r.err
    assert r.calls == 2, f"429 必须被重试，实际调用 {r.calls} 次"
    # 红了能一眼看出是「抖动」还是「真错」：ASCII 机器标记 + 错误原文都要在日志里
    assert "[registry_retry] RETRY attempt=1/3 sleep=0" in r.err, r.err
    assert "[registry_retry] OK attempt=2/3" in r.err, r.err
    assert "429 Too Many Requests" in r.err, "错误原文必须留在日志里（否则没法归因）"
    assert "attempt=1/3" in r.err and "attempt=2/3" in r.err


def test_五次尝试都对429无效时用尽并报rc3(tmp_path: Path) -> None:
    r = _run(tmp_path, "always_429", err_text=INCIDENT_429,
             env_extra={"REGISTRY_RETRY_ATTEMPTS": "3"})
    assert r.rc == 3, f"用尽尝试应 rc=3（区别于不可重试的 2），实际 {r.rc}\n{r.err}"
    assert r.calls == 3
    assert "429 Too Many Requests" in r.err
    assert "[registry_retry] EXHAUSTED attempts=3 rc=1" in r.err, r.err
    # 用尽时要把**每一次**的首行都摆出来：否则前几次的现场只剩一行「第 N 次」，没法归因
    assert "各次错误首行" in r.err, r.err
    assert "第1次(rc=1)" in r.err and "第3次(rc=1)" in r.err, r.err


# --- 3. 真错不许被重试（这条与上一条同样重要） --------------------------------


def test_不可重试的错立刻失败_rc2_且只调用一次(tmp_path: Path) -> None:
    r = _run(tmp_path, "not_retryable", err_text=NOT_RETRYABLE)
    assert r.rc == 2, f"不可重试应 rc=2，实际 {r.rc}\n{r.err}"
    assert r.calls == 1, "构造错了不许重试（重试只会让红更慢、真原因被刷掉）"
    assert "[registry_retry] NONRETRYABLE rc=1 attempts_used=1" in r.err, r.err
    assert NOT_RETRYABLE.splitlines()[0] in r.err, "真错误的首行必须原样留在日志里"


def test_构造错与配置错都不许被重试(tmp_path: Path) -> None:
    """判据不许拿短词做全文匹配。这三条都是「里面恰好含 429 / EOF / no such host」的真错，
    必须 rc=2 且只跑一次；判据一旦退化回裸短词，它们就会被当成抖动重试三遍。"""
    for label, text in (
        ("parse_error_on_line_429", PARSE_ERROR_ON_LINE_429),
        ("truncated_dockerfile_eof", TRUNCATED_DOCKERFILE),
        ("wrong_registry_host", WRONG_REGISTRY_HOST),
    ):
        r = _run(tmp_path / label, "not_retryable", err_text=text)
        assert r.rc == 2, f"{label} 应 rc=2（不可重试），实际 {r.rc}\n{r.err}"
        assert r.calls == 1, f"{label} 只许跑一次（这是真错，不是抖动）"


# --- 4. 退避本身可断言 --------------------------------------------------------


def test_退避秒数按环境变量走_且最后一次失败后不再等待(tmp_path: Path) -> None:
    r = _run(tmp_path, "always_429", err_text=INCIDENT_429,
             env_extra={"REGISTRY_RETRY_ATTEMPTS": "3", "REGISTRY_RETRY_SLEEPS": "7 9"})
    assert r.rc == 3, r.err
    assert r.sleeps == ["7", "9"], f"两次失败后应分别等 7s、9s；实际 {r.sleeps}"


def test_档数不够时沿用最后一档_不退化成立刻重试(tmp_path: Path) -> None:
    r = _run(tmp_path, "always_429", err_text=INCIDENT_429,
             env_extra={"REGISTRY_RETRY_ATTEMPTS": "4", "REGISTRY_RETRY_SLEEPS": "5"})
    assert r.rc == 3, r.err
    assert r.sleeps == ["5", "5", "5"], f"三档都该等到 5s；实际 {r.sleeps}"


# --- 5. 给人看的处置结论（rc 2 与 rc 3 在 UI 上只差一个数字） ------------------


def test_给github_step_summary写明该重跑还是别重跑(tmp_path: Path) -> None:
    """不可重试 ⇒ 「别重跑」；抖动到用尽 ⇒ 「重跑这一步就行」。写反了比不写更坏。"""
    sm_no = tmp_path / "no_retry.md"
    r_no = _run(tmp_path / "a", "not_retryable", err_text=NOT_RETRYABLE,
                env_extra={"GITHUB_STEP_SUMMARY": sm_no.as_posix()})
    assert r_no.rc == 2, r_no.err
    text_no = sm_no.read_text(encoding="utf-8")
    assert "别重跑" in text_no, text_no
    assert "重跑这一步就行" not in text_no, text_no

    sm_yes = tmp_path / "yes_retry.md"
    r_yes = _run(tmp_path / "b", "always_429", err_text=INCIDENT_429,
                 env_extra={"GITHUB_STEP_SUMMARY": sm_yes.as_posix(),
                            "REGISTRY_RETRY_ATTEMPTS": "2"})
    assert r_yes.rc == 3, r_yes.err
    text_yes = sm_yes.read_text(encoding="utf-8")
    assert "重跑这一步就行" in text_yes, text_yes
    assert "别重跑" not in text_yes, text_yes


# --- 6. 用法错 / 参数错：立刻失败，别把参数错当成抖动 --------------------------


def test_缺分隔符时rc1(tmp_path: Path) -> None:
    env = dict(os.environ)
    env["BASH_ENV"] = (tmp_path / "_bashenv.sh").as_posix()
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "_bashenv.sh").write_text(STUB_ENV, encoding="utf-8", newline="\n")
    proc = subprocess.run(
        [_bash(), SCRIPT_REL, "docker", "build", "-t", "x", "."],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=TIMEOUT_S,
    )
    assert proc.returncode == 1, proc.stderr
    assert "--" in proc.stderr, "报错要告诉用户怎么改（命令前必须有 --）"


def test_尝试次数非法时rc1_且不跑命令(tmp_path: Path) -> None:
    r = _run(tmp_path, "digest", err_text="", env_extra={"REGISTRY_RETRY_ATTEMPTS": "abc"})
    assert r.rc == 1, r.err
    assert r.calls == 0, "参数非法时不许真去跑命令"
    assert "REGISTRY_RETRY_ATTEMPTS" in r.err

# --- 7. 自接线：脚本必须真被 CI 用上（否则等于没写） ---------------------------


def _docker_build_lines() -> list[tuple[str, int, str]]:
    """仓里所有「真的要构建镜像」的行：`docker build ` 后面必须跟空格，
    这样 `docker buildx imagetools inspect`（那不是构建）不会被算进来。"""
    targets = [REPO / ".github" / "workflows" / "ci.yml"] + sorted((REPO / "scripts").glob("*.sh"))
    hits: list[tuple[str, int, str]] = []
    for path in targets:
        rel = path.relative_to(REPO).as_posix()
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            # 整行注释不算：注释里提到 `docker build` 是散文，不是构建
            # （这条断言第一版就被自己脚本头的注释咬到了，正是本批反复在治的那类误伤）
            if line.lstrip().startswith("#"):
                continue
            if re.search(r"\bdocker build\s", line):
                hits.append((rel, i, line))
    return hits


def test_每一处docker构建都走这个脚本() -> None:
    """本仓的老毛病之一：守卫写好了却没人调用（`test_每个守卫脚本都必须被_ci_真的调用`
    治的是 `check_*.py`）。这条把同一把尺子对准本脚本。

    **第一版这条是假的**：它 `break` 在第一处匹配上，只钉住了 `ci.yml` 里的 `sbom` 那行，
    把 `image-signing` 或 `container_smoke.sh` 退回裸 `docker build` 它照样绿。
    现在改成逐行断言，并且把 `scripts/*.sh` 也纳入扫描范围。
    """
    hits = _docker_build_lines()
    assert hits, "一处 docker build 都没扫到 —— 断言本身失效了（路径/写法变了？）"
    unwrapped = [(rel, i, line.strip()) for rel, i, line in hits if "registry_retry.sh" not in line]
    assert not unwrapped, f"这些 docker build 没走 registry_retry.sh：{unwrapped}"
    ci_sites = [h for h in hits if h[0].endswith("ci.yml")]
    assert len(ci_sites) >= 2, f"ci.yml 里至少要有两处（本批是 sbom 与 image-signing）：{ci_sites}"
    # ci.yml 的 `run:` 行要写成 `bash scripts/…`（与 test_script_exec_bits.py 的可执行位规则兼容）
    for rel, i, line in ci_sites:
        stripped = line.strip()
        if stripped.startswith("run:"):
            stripped = stripped[len("run:") :].strip()
        assert stripped.startswith("bash scripts/registry_retry.sh --"), (
            f"{rel}:{i} 要写成 `bash scripts/registry_retry.sh -- <docker 命令…>`：{line!r}"
        )
    # 脚本里用相对路径调同级脚本时，必须先算好它所在目录（否则 cwd 一变就找不到）
    smoke = (REPO / "scripts" / "container_smoke.sh").read_text(encoding="utf-8")
    if "registry_retry.sh" in smoke:
        assert re.search(r'^HERE="\$\(cd "\$\(dirname ', smoke, re.M), (
            "container_smoke.sh 用了 $HERE 就必须先定义 HERE=（照 run_local.sh 的写法）"
        )
