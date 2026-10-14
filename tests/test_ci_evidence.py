"""CI 逐 job 结论的**仓内**证据守卫（第八十二批 洞2）。

要治的缺口（第八十批真出过，不是假想）：

  * 台账里的 `run:` 是**自由文本**，守卫只核「是不是纯数字」
    （`grep -c '"run"' tests/test_delivery_receipts.py` ⇒ 0）。第八十批 Z 行写成 Y 的 run 号
    —— 于是「这个提交的 CI 全绿」这句话指向了**别人的**一次运行，而没有任何东西能发现它；
  * 「逐 job success」的结论只活在 GitHub 页面与本机 `waNN/watch-ci-*.py` 的 stdout 里
    ⇒ 换台机器、过一段时间就无法复核（第七十九批撞过「回执已被覆盖、不可复核」）。

判据（全部由 git 与仓内字节推导；证据里不许写人工结论）：

  1. **绑定**：每行的 `ci_evidence` 必须是 `evidence/ci/<sha12>.json`，且文件真在 git 里
     （不是「盘上有」——`.gitignore` 那种静默吞掉的坑第八十二批 F2 已经撞过一次）；
  2. **同源提交**：`evidence.ci.<sha>.branches.<b>.head_sha == sha`（**这一条直接杀掉串号**）、
     `event=push`、`status=completed`、`conclusion=success`、`head_branch` 对得上；
  3. **台账的 run 号必须等于证据里的 dev run 号**（第八十批的串号就是这一条没核）；
     行里若用散文写了 `master run = <数字>`，那个数字也必须等于证据里的 master run 号
     （写了就必须是真的 —— 空着不核，写了才核）；
  4. **逐 job**：核心 job 必须都在且 `success`；只有登记过的那两个 job 允许 `skipped`；
     出现**未登记的新 job** 也要红（否则 workflow 里新加一个失败的 job，证据照样「全绿」）；
  5. **载体同源**：CI 证据必须与同一提交的回执 `receipts/<sha12>.yaml` 由**同一个提交**带进来。

切点（`CUT`）：第八十二批 **F2 的提交** = 落地这条判据的那次提交。自此（含）之后的每一行台账
都必须带 `ci_evidence`；F1 及更早的行是**历史**，按历史放行、**不回填** —— 回执的「下一个提交
携带」拓扑决定了没法给它们补证据（证据必须与回执同一个载体，而那些载体早就推上去了）。
切点写成常量，不从「哪一行有字段」反推：反推有逃生口 —— 把唯一那份带字段的行的字段删掉，
切点就消失了。

本文件是**自包含**的：不 import `scripts/ci_evidence.py`（那是被守卫者）。schema 在这里重写一遍，
并且**拒绝静默**：任何一项抽不到都算红。
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
LEDGER = ROOT / "tests" / "DELIVERY_LEDGER.yaml"
RECEIPTS = ROOT / "receipts"
CI_DIR = ROOT / "evidence" / "ci"

#: 第八十二批 F2 的提交（落地这条判据的那次提交）。见模块开头「切点」。
CUT = "17cd5e5c26c2de6b7a80c610ca9b30966c9ac8d1"

SCHEMA = "warden-ci-evidence/1"
REPO = "kwny-io/warden-agent"
BRANCHES = ("dev", "master")
CORE = {"container", "evals-real", "migration", "quality", "sbom", "security"}
EXPECT_SKIP = ("变异测试", "image-signing")
REQUIRED_TOP = {"schema", "repo", "sha", "fetched_at", "source", "branches"}
REQUIRED_BRANCH = {"run_id", "url", "event", "head_branch", "head_sha", "status",
                   "conclusion", "created_at", "jobs"}
HEX40 = re.compile(r"^[0-9a-f]{40}$")
MASTER_RUN_IN_PROSE = re.compile(r"master run\s*=\s*(\d+)")


def _git(*args: str) -> str:
    r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 0, f"git {' '.join(args)} 失败：{r.stderr.strip()}"
    return r.stdout.strip()


def _added_by(path: str) -> str:
    return _git("log", "--diff-filter=A", "--format=%H", "-1", "--", path)


def _after_cut(sha: str) -> bool:
    r = subprocess.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", CUT, sha],
                       capture_output=True)
    return r.returncode == 0


def ledger_rows() -> list[dict]:
    return list((yaml.safe_load(LEDGER.read_text(encoding="utf-8")) or {}).get("rows") or [])


def rows_with_ci() -> list[dict]:
    return [r for r in ledger_rows() if str(r.get("ci_evidence") or "").strip()]


def row_block(sha: str) -> str:
    """台账里那一行的原文（用来核散文里写的 run 号 —— 写了就必须是真的）。"""
    lines = LEDGER.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if ln.strip() == f"- sha: {sha}"), None)
    assert start is not None, f"台账里找不到 sha={sha[:12]} 的行"
    out = [lines[start]]
    for ln in lines[start + 1:]:
        if ln.lstrip().startswith("- sha:"):
            break
        out.append(ln)
    return "".join(out)


def load_ci(row: dict) -> dict:
    return json.loads((ROOT / str(row["ci_evidence"])).read_text(encoding="utf-8"))


# ---------- 切点与「不许漏」 ----------

def test_切点在本仓历史里():
    assert _git("rev-parse", "--verify", f"{CUT}^{{commit}}"), f"切点 {CUT[:12]} 不在本仓"
    r = subprocess.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", CUT, "HEAD"],
                       capture_output=True)
    assert r.returncode == 0, (
        f"切点 {CUT[:12]} 不是 HEAD 的祖先 ⇒ 这条守卫在浅检出/别的分支上是查不动而不是没问题"
        f"（CI 的 quality job 要 fetch-depth: 0）")


def test_切点之后每一行都必须带_ci_证据():
    rows = ledger_rows()
    assert rows, "台账一行都没有"
    missing = [str(r.get("sha"))[:12] for r in rows
               if _after_cut(str(r.get("sha"))) and not str(r.get("ci_evidence") or "").strip()]
    assert not missing, (
        f"切点 {CUT[:12]} 之后的台账行没有 ci_evidence：{missing} ⇒ "
        f"「CI 逐 job 全绿」又变回自由文本了（切点之前的历史行按历史放行，不回填）")


def test_切点那一行必须在台账里_且带证据():
    """正面存在判据：没有它，「切点之后每一行」可以靠**删行**空转成绿。

    （删行 + 删证据 + 删回执 = 彻底抹掉一个提交的证明；回执与证据的存在分别由
     `tests/test_delivery_receipts.py` 与 test_没有孤儿_ci_证据 顶着，这一条把
     「切点自己那一行」钉住。）
    """
    hit = [r for r in ledger_rows() if str(r.get("sha")) == CUT]
    assert hit, (
        f"台账里没有切点 {CUT[:12]} 的行 ⇒ 切点那条判据的锚点被删了（删行 = 让判据空转）")
    assert str(hit[0].get("ci_evidence") or "").strip(), (
        f"切点 {CUT[:12]} 那一行没有 ci_evidence（它就是「第一份带证据的行」）")


# ---------- 判据 1：绑定到仓内文件，且真入库 ----------

@pytest.mark.parametrize("row", rows_with_ci(), ids=lambda r: str(r.get("sha"))[:12])
def test_证据在仓里_且真入了_git(row: dict):
    sha = str(row["sha"])
    path = str(row["ci_evidence"])
    assert path == f"evidence/ci/{sha[:12]}.json", (
        f"{sha[:12]} 的 ci_evidence 是 {path}，必须是 evidence/ci/{sha[:12]}.json —— "
        f"文件名自己就是被交付的提交")
    f = ROOT / path
    assert f.is_file(), f"{sha[:12]} 说证据是 {path}，仓里没有这个文件"
    r = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--error-unmatch", path],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, (
        f"{sha[:12]} 的 CI 证据 {path} 不在 git 索引里 ⇒ 它只是盘上的文件，"
        f"换台机器/CI 检出后就没有了（被忽略规则吞掉的证据等于不存在）")


def test_没有孤儿_ci_证据():
    used = {str(r["ci_evidence"]) for r in rows_with_ci()}
    orphan = [p.relative_to(ROOT).as_posix() for p in sorted(CI_DIR.glob("*.json"))
              if p.relative_to(ROOT).as_posix() not in used]
    assert not orphan, f"这些 CI 证据没有任何台账行引用（谁交付的？）：{orphan}"


# ---------- 判据 2/3：证据说的提交、分支、run 号 ----------

@pytest.mark.parametrize("row", rows_with_ci(), ids=lambda r: str(r.get("sha"))[:12])
def test_证据自述的被交付提交就是那一行(row: dict):
    sha = str(row["sha"])
    d = load_ci(row)
    assert str(row["ci_evidence"]).endswith(f"{sha[:12]}.json")
    assert d.get("schema") == SCHEMA, f"{sha[:12]}：schema={d.get('schema')!r}"
    assert d.get("repo") == REPO, f"{sha[:12]}：repo={d.get('repo')!r}"
    assert d.get("sha") == sha, (
        f"{sha[:12]} 的 CI 证据自称被交付的是 {str(d.get('sha'))[:12]}")
    assert HEX40.match(sha), f"{sha[:12]} 不是 40 位小写 hex（台账里写全 sha）"
    missing = REQUIRED_TOP - set(d)
    assert not missing, f"{sha[:12]} 的 CI 证据缺顶层字段 {sorted(missing)}"


@pytest.mark.parametrize("row", rows_with_ci(), ids=lambda r: str(r.get("sha"))[:12])
def test_两条分支的_run_跑的都必须是这个提交(row: dict):
    sha = str(row["sha"])
    d = load_ci(row)
    branches = d.get("branches") or {}
    for b in BRANCHES:
        info = branches.get(b)
        assert isinstance(info, dict), f"{sha[:12]}：缺 {b} 分支的 run 记录"
        miss = REQUIRED_BRANCH - set(info)
        assert not miss, f"{sha[:12]}：{b} 的记录缺字段 {sorted(miss)}"
        assert info["head_sha"] == sha, (
            f"{sha[:12]}：{b} 的 run（{info['run_id']}）跑的是 {str(info['head_sha'])[:12]} ⇒ "
            f"**串号**（第八十批就是这条没核）")
        assert info["head_branch"] == b, (
            f"{sha[:12]}：{b} 的记录里 head_branch={info['head_branch']!r}")
        assert info["event"] == "push", f"{sha[:12]}：{b} 的 run event={info['event']!r}，期望 push"
        assert info["status"] == "completed", f"{sha[:12]}：{b} 的 run 还没跑完"
        assert info["conclusion"] == "success", (
            f"{sha[:12]}：{b} 的 run conclusion={info['conclusion']!r}，期望 success")


@pytest.mark.parametrize("row", rows_with_ci(), ids=lambda r: str(r.get("sha"))[:12])
def test_台账的_run_号必须等于证据里的_dev_run(row: dict):
    sha = str(row["sha"])
    d = load_ci(row)
    dev = d["branches"]["dev"]
    assert str(row.get("run") or "").strip().isdigit(), (
        f"{sha[:12]} 的台账 run={row.get('run')!r} 不是纯数字（老规则）")
    assert int(str(row["run"]).strip()) == dev["run_id"], (
        f"{sha[:12]} 的台账写 run={row['run']}，而 CI 证据里 dev 的 run 是 {dev['run_id']}"
        f" ⇒ 台账指向了另一次运行（这就是「逐 job 全绿」变自由文本的入口）")


@pytest.mark.parametrize("row", rows_with_ci(), ids=lambda r: str(r.get("sha"))[:12])
def test_散文里写的_master_run_号也必须是真的(row: dict):
    """写了就必须真：行里出现 `master run = <数字>` 时，它必须等于证据里的 master run。"""
    sha = str(row["sha"])
    m = MASTER_RUN_IN_PROSE.search(row_block(sha))
    if not m:
        pytest.skip("这一行的 note 没写 master run 号（空着不核，写了才核）")
    got = load_ci(row)["branches"]["master"]["run_id"]
    assert int(m.group(1)) == got, (
        f"{sha[:12]} 的 note 里写 master run = {m.group(1)}，而 CI 证据里是 {got}")


# ---------- 判据 4：逐 job ----------

@pytest.mark.parametrize("row", rows_with_ci(), ids=lambda r: str(r.get("sha"))[:12])
def test_每个_job_都_success_或登记为_skip_且没有未登记的_job(row: dict):
    sha = str(row["sha"])
    d = load_ci(row)
    for b in BRANCHES:
        jobs = d["branches"][b].get("jobs")
        assert isinstance(jobs, list) and jobs, f"{sha[:12]}：{b} 的 jobs 是空的 ⇒ 没核到任何 job"
        names = {j.get("name") for j in jobs}
        for name, concl in ((j.get("name"), j.get("conclusion")) for j in jobs):
            if any(str(name).startswith(pre) for pre in EXPECT_SKIP):
                assert concl == "skipped", (
                    f"{sha[:12]}：{b} 的 {name} conclusion={concl!r}，"
                    f"push 上它本来就该是 skipped（登记见 EXPECT_SKIP）")
            else:
                assert concl == "success", (
                    f"{sha[:12]}：{b} 的 {name} conclusion={concl!r}，期望 success")
        missing = sorted(c for c in CORE if c not in names)
        assert not missing, (
            f"{sha[:12]}：{b} 的 jobs 里缺核心 job {missing} ⇒ workflow 改了，或摘录漏了")
        unreg = sorted(str(n) for n in names
                       if n not in CORE and not any(str(n).startswith(pre) for pre in EXPECT_SKIP))
        assert not unreg, (
            f"{sha[:12]}：{b} 出现未登记的新 job {unreg} ⇒ 先明确它该是什么结论，"
            f"再把它登记进 CORE/EXPECT_SKIP（否则新加一个失败的 job，证据照样'全绿'）")


# ---------- 判据 5：载体同源 ----------

@pytest.mark.parametrize("row", rows_with_ci(), ids=lambda r: str(r.get("sha"))[:12])
def test_ci_证据与回执由同一个提交带进来(row: dict):
    sha = str(row["sha"])
    rel = str(row["ci_evidence"])
    rec = f"receipts/{sha[:12]}.yaml"
    assert (ROOT / rec).is_file(), f"{sha[:12]} 没有回执 {rec}（CI 证据必须与回执同一个载体）"
    a_ci = _added_by(rel)
    a_rec = _added_by(rec)
    assert a_ci, f"{sha[:12]} 的 CI 证据不在历史里（还没提交？）"
    assert a_ci == a_rec, (
        f"{sha[:12]} 的 CI 证据由 {a_ci[:12]} 带进来，回执由 {a_rec[:12]} 带进来 ⇒ 不同源："
        f"证据可以在事后换成另一份（载体必须跟着回执走）")
