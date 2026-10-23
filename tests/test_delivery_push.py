"""`scripts/check_delivery_push.py` 的守卫测试（第八十三批）。

为什么需要这份文件（现场 = 2026-10-15）：

`dev` 一次推上 `904071e`/`7cf9b9f`/`5c0faa5` 三个提交之后才发现 —— GitHub 只为 push 的
**tip** 建 run（`gh api …?head_sha=<sha>` 实测 `total_count = 0 / 0 / 1`），而台账的
两条判据要求「链上每个提交都有行」+「每行都带 `evidence/ci/<sha12>.json`」，于是两个中间提交
**永远**拿不到证据，台账结构上不再可满足，只能改写历史。**那一刻已经不可逆**。

这条装置的价值全在「提前说出来」，所以它的判据必须自己先有牙：

- **构造数据**（`check_push` 是纯函数）：正对照（单提交携带父行、根提交、空范围）+ 各类坏形状，
  尤其是「**一次推两个、而且两个都是交付提交**」—— 这条最容易漏：形状个个合法，但多推一个
  就是不可取的。「只抓非交付提交」是不够的。
- **真 git 仓**（临时目录里 `git init`）：走一遍真代码路径（`git show`/`rev-list` + 读 YAML），
  并把**中文台账**喂进去 —— Windows 上 `subprocess` 默认按本机编码（GBK）解码，实测把中文台账
  解炸（`UnicodeDecodeError: 'gbk' codec …`），而失败时 `stdout` 变成 `None`，看起来像
  「命令成功了但台账是空的」。这条用例就是它的回归。
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_delivery_push as push  # noqa: E402

A = "a" * 40
B = "b" * 40
C = "c" * 40
D = "d" * 40


# ---------- 构造数据：判据本身 ----------


def test_正对照_单提交携带父行_不说一句():
    got = push.check_push(commits=[push.Commit(B, A)], rows_at={B: {A}})
    assert got == []


def test_正对照_根提交_不说一句():
    """没有 first parent 就没什么可携带的：它只能是「起点」，不能因此被红。"""
    assert push.check_push(commits=[push.Commit(A, None)], rows_at={A: set()}) == []


def test_正对照_空范围_不说一句():
    assert push.check_push(commits=[], rows_at={}) == []


def test_一次推两个提交就报_即使两个都是交付提交():
    """**本文件最重要的一条**：形状个个合法（每个都携带了自己的父行）也不行。

    因为 GitHub 只为 tip 建 run ⇒ 前一个提交的 run 不存在 ⇒ 它那一行的证据永远渲染不出来。
    """
    got = push.check_push(commits=[push.Commit(B, A), push.Commit(C, B)], rows_at={B: {A}, C: {B}})
    assert len(got) == 1, got
    assert "带了 2 个提交" in got[0]
    assert "只推一个" in got[0]


def test_tip没携带父行要报_并说清它自己那棵树在_ci_里就是红的():
    got = push.check_push(commits=[push.Commit(B, A)], rows_at={B: set()})
    assert len(got) == 1, got
    assert "（tip）没有携带它 first parent" in got[0]
    assert "全绿" in got[0]


def test_一次推三个提交_也只报一条多提交():
    got = push.check_push(
        commits=[push.Commit(B, A), push.Commit(C, B), push.Commit(D, C)],
        rows_at={B: {A}, C: {B}, D: {C}},
    )
    assert len(got) == 1, got
    assert "带了 3 个提交" in got[0]


def test_中间提交缺行的单独一条要说出_永远与不可逆():
    """把「多提交」那一条排掉，只看**中间提交缺行**这一条自己的话（C 在中间，D 是 tip）。"""
    got = push.check_push(
        commits=[push.Commit(B, A), push.Commit(C, B), push.Commit(D, C)],
        rows_at={B: {A}, C: set(), D: set()},
    )
    middle = [item for item in got if item.startswith(f"{C[:7]} 没有携带")]
    assert middle, got
    assert "永远" in middle[0]
    assert "不可逆" in middle[0]


# ---------- 真 git 仓：走过真代码路径（含编码） ----------


def _git(repo: pathlib.Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=repo,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    if done.returncode != 0:  # 把 stderr 带出来：不然只能看到「rc=1」
        raise AssertionError(f"git {' '.join(args)} rc={done.returncode}: {done.stderr.strip()}")
    return done.stdout


def _commit(repo: pathlib.Path, message: str, rows: list[str]) -> str:
    ledger = repo / "tests" / "DELIVERY_LEDGER.yaml"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    head = ["# 台账：注释里有中文，用来钉住解码（Windows 上默认是 GBK）。"]
    rows_yaml = ["rows:", *[f"  - sha: {sha}" for sha in rows]] if rows else ["rows: []"]
    ledger.write_text("\n".join([*head, *rows_yaml]) + "\n", encoding="utf-8")
    # 每个提交都要有实际改动（否则 git 报「nothing to commit」；这条踩过一次）。
    (repo / "note.txt").write_text(message + "\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture()
def repo(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    _git(tmp_path, "init", "-q", "-b", "dev")
    monkeypatch.setattr(push, "REPO", tmp_path)
    return tmp_path


def test_真仓_单提交携带父行_退出码0(repo: pathlib.Path):
    a = _commit(repo, "起点：台账空", [])
    b = _commit(repo, "交付提交：携带 a 的行", [a])
    assert push.main(["--base", a, "--tip", b]) == 0


def test_真仓_两个提交一次推_退出码1(repo: pathlib.Path):
    a = _commit(repo, "起点：台账空", [])
    b = _commit(repo, "交付提交：携带 a 的行", [a])
    c = _commit(repo, "第二个交付提交：携带 b 的行", [a, b])
    assert push.main(["--base", a, "--tip", b]) == 0
    assert push.main(["--base", b, "--tip", c]) == 0
    # 一次推两个：两个都是交付提交，仍然红（中间那个拿不到 run）
    assert push.main(["--base", a, "--tip", c]) == 1


def test_真仓_非交付提交_退出码1(repo: pathlib.Path):
    a = _commit(repo, "起点：台账空", [])
    b = _commit(repo, "探针提交：只改脚本，没携带 a 的行", [])
    assert push.main(["--base", a, "--tip", b]) == 1


def test_真仓_中文台账能读_不是2(repo: pathlib.Path):
    """2 = 核不动。Windows 的 GBK 解码炸过这里（台账全中文）⇒ 这条是回归。"""
    a = _commit(repo, "起点：台账空", [])
    b = _commit(repo, "交付提交：携带 a 的行", [a])
    assert push.main(["--base", a, "--tip", b]) != 2


def test_真仓_起点不在树里_退回核父提交_并说出没核到什么(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """改写历史后 CI 的检出里没有旧 tip ⇒ 起点核不动（第八十六批实录）。

    判据 1 自己推荐的出路就是「改写历史把提交压成一个」——若判据在改写后必然报红，
    那条出路就被它自己封死了。所以退一步核 `tip^..tip`，并且**必须说出**没核到什么。
    """
    a = _commit(repo, "起点：台账空", [])
    b = _commit(repo, "交付提交：携带 a 的行", [a])
    gone = "deadbeef" * 5  # 任何 ref 都不可达（真实的改写历史/浅检出与它同形）
    assert push.main(["--base", gone, "--tip", b]) == 0
    out = capsys.readouterr().out
    assert "不在树里" in out
    assert "没核到的是：这次 push 到底推了几个提交" in out
    assert "形状对（按可核的部分）" in out


def test_真仓_起点在树里时_不许说退回(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """反向对照：起点真在树里就不许印那句「退回」——否则它退化成一句恒真话术。"""
    a = _commit(repo, "起点：台账空", [])
    b = _commit(repo, "交付提交：携带 a 的行", [a])
    assert push.main(["--base", a, "--tip", b]) == 0
    out = capsys.readouterr().out
    assert "不在树里" not in out and "退回" not in out
    assert "形状对：本次 push 只推一个交付提交" in out


def test_真仓_退回不等于放行_父行缺席照样红(repo: pathlib.Path) -> None:
    """起点核不动时，判据 2（tip 携带上一提交的行）依然要报红。"""
    _commit(repo, "起点：台账空", [])
    b = _commit(repo, "探针提交：没携带 a 的行", [])
    assert push.main(["--base", "deadbeef" * 5, "--tip", b]) == 1


def test_真仓_连父提交都定位不到_才是真的核不动(
    repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """根提交上起点核不动、`tip^` 也不存在 ⇒ 不许静默放过，rc=2。"""
    root = _commit(repo, "根提交", [])
    assert push.main(["--base", "deadbeef" * 5, "--tip", root]) == 2
    assert "核不动" in capsys.readouterr().err


def _run_check_delivery_push(args: list[str]) -> subprocess.CompletedProcess[str]:
    """对本仓跑一次推前形状守卫（`args` 决定 base 怎么取）。"""
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts/check_delivery_push.py"), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )


def test_本仓_能核动_不是2():
    """对本仓跑一次：0/1 都可以，2（核不动）不行 —— 它在真仓上也得读得动。

    **CI 里 HEAD 是分离的**（`@{u}` 取不到，`fatal: HEAD does not point to a branch`），而默认 base
    就是 `@{u}` ⇒ 不传 `--base` 那条路径在 CI 上必然给 2。那不是故障，是环境：没有上游可比。
    实测（PR #19 的两次 run）：本用例是这条守卫**第一次进 CI 时**才红的 —— `origin/dev` 上根本没有
    这两个文件，所以依赖 PR 的 quality 是绿的，只有把这套装置推上 CI 才看得见。

    所以这里把两件事分开断言：

    1. **与环境无关的牙**：显式给 `--base HEAD^` 时，真仓必须真的核得动（0/1）；
    2. **默认路径**：rc=2 只接受「拿不到上游」这一种（stderr 里出现 `@{u}`）——别的原因
       （台账读不动、yaml 坏了）仍然是红。
    """
    # 1) 显式 base：与环境无关的那一半。浅克隆里 `HEAD^` 拿不到就不强求（那是台账那条用例的事）。
    parent = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "HEAD^"],
        cwd=REPO_ROOT,
        capture_output=True,
        encoding="utf-8",
        check=False,
    ).stdout.strip()
    if parent:
        done = _run_check_delivery_push(["--base", parent])
        assert done.returncode in (0, 1), (done.returncode, done.stdout, done.stderr)
    # 2) 默认 base = @{u}：本仓没有上游（CI 分离头 / 本地分支没 -u）时才允许 2，且要是这一种 2
    done = _run_check_delivery_push([])
    if done.returncode == 2:
        assert "@{u}" in done.stderr, (done.returncode, done.stdout, done.stderr)
    else:
        assert done.returncode in (0, 1), (done.returncode, done.stdout, done.stderr)


# ---------- 空范围：no-op 与「改写/回退」必须分开（2026-10-19 实测的静默放行） ----------


def test_空范围判据_同提交是noop_起点比tip新是改写():
    """纯判据：空范围有两种含义，混成一种就是一次静默放行。"""
    assert push.empty_range_verdict(A, A) == "noop"
    # 起点比 tip 新（非快进/回退/force push 的极性）
    assert push.empty_range_verdict(B, A) == "force"


def test_真仓_起点比tip新时不许谎称只推了一个(repo: pathlib.Path):
    """现场（2026-10-19）：`--base <新> --tip <旧>` 时 rev-list 返回 0 个提交，
    它却打印「本次 push 只推一个交付提交」并返回 0 —— **0 个提交却说推了一个**。

    注意这条和「起点不在树里」是**两种不同**的空范围：`_upstream()` 取不到起点时会退回核
    父提交（第四种结局，有测试覆盖）；而这里起点**在**树里、只是比 tip 新 ⇒ 必须核不动。
    """
    a = _commit(repo, "起点：台账空", [])
    b = _commit(repo, "交付提交：携带 a 的行", [a])
    # a..a：本地与远端同一提交 ⇒ 真没东西推，放行但要说实话
    assert push.main(["--base", a, "--tip", a]) == 0
    # b 做起点、a 做 tip：起点比 tip 新 ⇒ 丢弃远端已有提交，几何无从评估
    assert push.main(["--base", b, "--tip", a]) == 2
