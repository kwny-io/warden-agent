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
from datetime import UTC, date, datetime

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
LEDGER = ROOT / "tests" / "DELIVERY_LEDGER.yaml"
RECEIPTS = ROOT / "receipts"
CI_DIR = ROOT / "evidence" / "ci"
RECEIPTS_DIR = ROOT / "receipts"

#: 第八十二批 F2 的提交（落地这条判据的那次提交）。见模块开头「切点」。
CUT = "17cd5e5c26c2de6b7a80c610ca9b30966c9ac8d1"

SCHEMA = "warden-ci-evidence/1"
REPO = "kwny-io/warden-agent"
BRANCHES = ("dev", "master")
CORE = {"container", "evals-real", "migration", "quality", "sbom", "security"}
EXPECT_SKIP = ("变异测试", "image-signing")
REQUIRED_TOP = {"schema", "repo", "sha", "fetched_at", "source", "branches"}
REQUIRED_BRANCH = {"run_id", "picked_run_id", "runs", "url", "event", "head_branch",
                   "head_sha", "status", "conclusion", "created_at", "jobs"}
HEX40 = re.compile(r"^[0-9a-f]{40}$")
def _utc_today() -> date:
    """「今天」一律取 UTC（第八十九批的教训：拿本地日期判会造出「本地绿、CI 红」）。"""
    return datetime.now(UTC).date()


def _is_iso_date(s: str) -> bool:
    if len(s) != 10:
        return False
    try:
        date.fromisoformat(s)
    except ValueError:
        return False
    return True


MASTER_RUN_IN_PROSE = re.compile(r"master run\s*=\s*(\d+)")


def _git(*args: str) -> str:
    r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 0, f"git {' '.join(args)} 失败：{r.stderr.strip()}"
    return r.stdout.strip()


def _added_by(path: str) -> str:
    return _git("log", "--diff-filter=A", "--format=%H", "-1", "--", path)


def _introduced_by(needle: str) -> str:
    """引入这段文本的那个提交（`-S` 按增删计数；取最早的一次＝第一次引入）。"""
    out = _git("log", "--format=%H", "-S", needle, "--", "tests/DELIVERY_LEDGER.yaml")
    lines = out.splitlines()
    return lines[-1] if lines else ""


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

# 例外清单：已交付、但 **GitHub 上取不到能当证据的 run** 的历史提交（sha -> 理由）。
#
# 为什么必须有它：台账的回填规则（`tests/DELIVERY_LEDGER.yaml` 头部，第七十九批定下）要求
# 「每个已交付提交由下一个提交背书」——也就是这些提交**必须有台账行**；而下面那条判据要求
# 切点之后每一行都要带 `ci_evidence`。对它们两条要求互斥，且**不是「懒得补」而是补不出来**
# （`gh api "repos/kwny-io/warden-agent/actions/runs?head_sha=<40 位全 sha>"` 的实测结果）：
#
#   * `904071e2` / `7cf9b9f7`：**一次 run 都没有**（返回空数组）。它们是多提交推送里的中间
#     提交，Actions 只在对被推的 tip 跑 ⇒ 没有 run 可引用；
#   * `5c0faa53`：`dev` 上有 run（`38781636407`）但 `conclusion=failure`（job `quality` 红）。
#     拿它当证据等于自报「CI 逐 job 全绿」，判据 3 也会拒。
#
# 这份清单是**冻结**的：`UNPROVEN_MAX` 只许减不许增。要加一行，必须同时把 `UNPROVEN_MAX` 改大、
# 并在理由里写清「GitHub 上为什么取不到 run」—— 也就是新交付的提交想躲证据，
# 必须改这份代码的历史，而不是悄悄少写一个字段。防滥用靠两道已有的枪：
# ①`test_没有孤儿_ci_证据`、②`tests/test_delivery_receipts.py::test_没有孤儿回执`
# （拿 `git-only` 藏起一份回执或证据，会先被那两条点名）。
UNPROVEN_ALLOWED = {
    "904071e219f839917ebfcd1b280abe6f23b1bc9a": (
        "多提交推送的中间提交：GitHub 上没有任何 run（只对被推的 tip 跑）"
    ),
    "7cf9b9f76ccaa5e11e5745fd388238db78a603e5": (
        "同上：段尾探针入库，随多提交推送上去，取不到 run"
    ),
    "5c0faa53d5000c3464bc3e523313f7dfa0338584": (
        "dev 的 run 38781636407 是 failure（job quality 红）⇒ 不能当「全绿」证据"
    ),
}
UNPROVEN_MAX = 3
# 红账上限：同一时刻最多允许几行 `verdict: red`。
# 为什么要有上限：`verdict: red` 是诚实记录，也是一个**便宜的出口**——允许它无限增长，
# 就等于允许「下一个提交把它修绿」永久往后推，交付面会变成一串红账。上限 1 ⇒ 红账是「在途」，
# 不是「常态」。
RED_MAX = 1
# 第九十八批补：上限只管**在途**的红账。已被后续提交结清的红账（`fixed_by` 指向真提交，且
# 那个提交把这一行接走了）是历史记录，不再占额度——否则上限的实际含义会变成
# 「本仓历史上只允许红一次」，与「红要记在账上」自相矛盾（第九十七批就撞上了这个矛盾：
# `f6bcd50` 的红已由 `c299ef6` 结清，却还占着唯一的名额）。


def unproven_violations(
    entries: list[dict],
    *,
    after_cut: set[str],
    allowed: dict[str, str],
    max_entries: int,
) -> list[str]:
    """「切点之后没有 CI 证据」的行，只许是清单里点名过的历史提交。

    `entries` 每项形如 `{"sha", "has_evidence", "has_receipt", "attestation"}`（由调用方算，
    因为算它们要读 git / 读文件）；这里是**纯函数** ⇒ 能被单元测试喂合成输入，
    所以下面每条子规则都有牙（变异核对里有对应登记）。
    """
    out: list[str] = []
    by_sha = {str(e["sha"]): e for e in entries}
    for sha, e in by_sha.items():
        is_red = e.get("verdict") == "red"
        if sha in after_cut and is_red:
            # 红账：结构上可能真的取不到证据（本提交只在 dev 上跑过、master 上没有它的 run），
            # 所以允许没证据——但必须点名为什么红、由谁结清。这是本仓唯一允许「红着落地」的口子，
            # 所以它自己的前提也要被检查：不许同时有证据、不许有回执、不许堆成一串。
            if e["has_evidence"]:
                out.append(
                    f"{sha[:12]} 是 verdict: red 却有 ci_evidence ⇒ 自相矛盾："
                    f"那份证据本身要求 dev 与 master 两条 push run 全绿"
                )
            if not str(e.get("red_why") or "").strip():
                out.append(
                    f"{sha[:12]} 是 verdict: red，但没写 red_why（红要说出原因，不是形容词）"
                )
            if e.get("has_receipt"):
                out.append(
                    f"{sha[:12]} 是 verdict: red，却有回执 receipts/{sha[:12]}.yaml ⇒ "
                    f"红的交付不能留交付回执"
                )
            fx = str(e.get("fixed_by") or "")
            if not fx or fx == "未修":
                if not e.get("is_last"):
                    out.append(
                        f"{sha[:12]} 是 verdict: red 且 fixed_by=未修，但它不是台账最后一行 ⇒ "
                        f"红账只能挂在最新一处（旧红账必须已经被下一个提交结清）"
                    )
            elif not e.get("fixed_by_exists"):
                out.append(
                    f"{sha[:12]} 的 fixed_by={fx[:12]} 在台账里没有行（结清者必须是真提交）"
                )
            elif not e.get("fixed_by_carries_row"):
                out.append(
                    f"{sha[:12]} 的 fixed_by={fx[:12]} 没有把这一行接走"
                    f"（它的块里没有这个 sha 的 ledger_row）⇒ 结清要落在台账上，不是口头提一句"
                )
            # 空洞必须带期限：本仓对「允许留的洞」一律要求 review_by（跳过签字、触发器台账都是），
            # 红账是**最后一个**没期限的口子 ⇒ 不约束期限，它就会从「在途」变成「永久」。
            rb = str(e.get("review_by") or "").strip()
            if not rb:
                out.append(
                    f"{sha[:12]} 是 verdict: red，但没写 review_by ⇒ 红账没有期限"
                    f"（其它空洞都要期限，这里不能例外）"
                )
            elif not _is_iso_date(rb):
                out.append(f"{sha[:12]} 的 review_by 不是 YYYY-MM-DD：{rb!r}")
            elif rb < _utc_today().isoformat():
                out.append(
                    f"{sha[:12]} 的 review_by={rb} 已过期 ⇒ 这一行红账必须重新表态"
                    f"（要么结清 fixed_by，要么续期并写清新理由）"
                )
        elif sha in after_cut and not e["has_evidence"] and sha not in allowed:
            out.append(f"{sha[:12]} 在切点之后却没有 ci_evidence，也不在 UNPROVEN_ALLOWED 里")
    for sha, reason in allowed.items():
        e = by_sha.get(sha)
        if e is None:
            out.append(f"UNPROVEN_ALLOWED 里的 {sha[:12]} 在台账里没有行（例外指着一个不存在的行）")
            continue
        if e["has_evidence"]:
            out.append(
                f"{sha[:12]} 既在 UNPROVEN_ALLOWED 里又有 ci_evidence ⇒ 例外被滥用（撤掉它）"
            )
        if e.get("attestation") != "git-only":
            out.append(
                f"{sha[:12]} 在例外清单里，但 attestation={e.get('attestation')!r}"
                f"（必须是 git-only）"
            )
        if e["has_receipt"]:
            out.append(
                f"{sha[:12]} 在例外清单里，却存在回执 receipts/{sha[:12]}.yaml ⇒ "
                f"拿 git-only 藏回执（有回执的行必须带证据）"
            )
        if len(str(reason).strip()) < 20:
            out.append(f"{sha[:12]} 的例外理由太短（要写清 GitHub 上为什么取不到 run）")
    if len(allowed) > max_entries:
        out.append(
            f"例外清单涨到 {len(allowed)} 条（上限 {max_entries}）⇒ 只许减不许增："
            f"新增等于让「切点之后必须有证据」失效"
        )
    reds = [
        e
        for e in entries
        if e.get("verdict") == "red" and str(e.get("fixed_by") or "") in ("", "未修")
    ]
    if len(reds) > RED_MAX:
        out.append(
            f"同一时刻最多 {RED_MAX} 行红账，现在 {len(reds)} 行 ⇒ 红账成了常态："
            f"说明「下一个提交把它修绿」一直没发生"
        )
    return out


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
    all_shas = {str(r.get("sha")) for r in rows}
    entries = [
        {
            "sha": str(r.get("sha")),
            "has_evidence": bool(str(r.get("ci_evidence") or "").strip()),
            "has_receipt": (RECEIPTS_DIR / f"{str(r.get('sha'))[:12]}.yaml").is_file(),
            "attestation": r.get("attestation"),
            # 红账三件套（判据见 unproven_violations 的 is_red 分支）
            "verdict": r.get("verdict"),
            "red_why": r.get("red_why"),
            "fixed_by": r.get("fixed_by"),
            "review_by": r.get("review_by"),
            "is_last": i == len(rows) - 1,
            "fixed_by_exists": str(r.get("fixed_by") or "") in all_shas,
            "fixed_by_carries_row": (
                str(r.get("sha"))[:12] in row_block(str(r.get("fixed_by")))
                if str(r.get("fixed_by") or "") in all_shas
                else False
            ),
        }
        for i, r in enumerate(rows)
    ]
    after_cut = {e["sha"] for e in entries if _after_cut(e["sha"])}
    bad = unproven_violations(
        entries, after_cut=after_cut, allowed=UNPROVEN_ALLOWED, max_entries=UNPROVEN_MAX
    )
    assert not bad, (
        "；".join(bad) + " ⇒ 「CI 逐 job 全绿」又变回自由文本了"
        "（切点之前的历史行按历史放行，不回填；取不到 run 的历史提交只能进 UNPROVEN_ALLOWED）"
    )


# ---------- 判据自己的牙：用合成输入把每条子规则分别撞一遍 ----------
# （没有这几条，上面那条判据在本仓是「恒绿」的：现实台账里恰好没有反例 ⇒ 逻辑被改坏也没人知道。）

def _entry(sha: str, *, ev: bool = False, rec: bool = False, **extra: object) -> dict:
    d = {
        "sha": sha,
        "has_evidence": ev,
        "has_receipt": rec,
        "attestation": "git-only",
    }
    d.update(extra)
    if d.get("verdict") == "red" and "review_by" not in d:
        # 合成输入默认给一个未来期限：期限规则有自己的牙（`test_牙_红账必须带期限`），
        # 否则每条红账合成用例都要复述一遍 review_by。
        d["review_by"] = "2999-01-01"
    return d


def test_牙_切点之后没证据又不在清单里_必须被点名():
    sha = "b" * 40
    bad = unproven_violations([_entry(sha)], after_cut={sha}, allowed={}, max_entries=3)
    assert bad and sha[:12] in bad[0], bad


def test_牙_切点之前的历史行没证据_放行():
    sha = "c" * 40
    assert unproven_violations([_entry(sha)], after_cut=set(), allowed={}, max_entries=3) == []


def test_牙_清单里的行有了证据_必须被点名():
    sha = "d" * 40
    bad = unproven_violations(
        [_entry(sha, ev=True)], after_cut={sha}, allowed={sha: "x" * 30}, max_entries=3
    )
    assert bad and any("例外被滥用" in item for item in bad), bad


def test_牙_清单里的行藏着回执_必须被点名():
    sha = "e" * 40
    bad = unproven_violations(
        [_entry(sha, rec=True)], after_cut={sha}, allowed={sha: "x" * 30}, max_entries=3
    )
    assert bad and any("藏回执" in item for item in bad), bad


def test_牙_清单指着一个不存在的行_必须被点名():
    bad = unproven_violations([], after_cut=set(), allowed={"f" * 40: "x" * 30}, max_entries=3)
    assert bad and any("没有行" in item for item in bad), bad


def test_牙_清单里的行不是_git_only_必须被点名():
    sha = "1" * 40
    e = _entry(sha)
    e["attestation"] = "git+frozen"
    bad = unproven_violations([e], after_cut=set(), allowed={sha: "x" * 30}, max_entries=3)
    assert bad and any("必须是 git-only" in item for item in bad), bad


def test_牙_例外理由太短_必须被点名():
    sha = "2" * 40
    bad = unproven_violations([_entry(sha)], after_cut=set(), allowed={sha: "短"}, max_entries=3)
    assert bad and any("理由太短" in item for item in bad), bad


def test_牙_清单涨过上限_必须被点名():
    sha = "3" * 40
    bad = unproven_violations(
        [_entry(sha)], after_cut=set(), allowed={sha: "x" * 30}, max_entries=0
    )
    assert bad and any("只许减不许增" in item for item in bad), bad


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
    """载体同源（第八十九批按**交付等级**分开写）。

    此前这里只写死了「必须有回执」，于是 `git-only` 的行永远红——而仓库同时又要求
    「切点之后每一行都必须带 CI 证据」。两条判据对 `git-only` 的行**结构上互斥**
    （没做本地冻结就不可能有回执）。分别是：

    * `git+frozen`：回执必须存在，且回执与 CI 证据由**同一个**提交带进来；
    * `git-only`：**不许**有回执（有就说明冻结等级是伪造的）；CI 证据必须与「台账里那行
      `ci_evidence:` 文本」由同一个提交引入 ⇒ 证据与它的主张仍然同车。
    """
    sha = str(row["sha"])
    rel = str(row["ci_evidence"])
    rec = f"receipts/{sha[:12]}.yaml"
    att = str(row.get("attestation") or "")
    a_ci = _added_by(rel)
    assert a_ci, f"{sha[:12]} 的 CI 证据不在历史里（还没提交？）"
    if att == "git-only":
        assert not (ROOT / rec).is_file(), (
            f"{sha[:12]} 声明 git-only（没做本地冻结），却有回执 {rec} ⇒ 等级自相矛盾："
            f"回执里那些 shape/keys/pytest 数字不可能来自一次没发生的冻结")
        a_row = _introduced_by(f"ci_evidence: {rel}")
        assert a_row, f"{sha[:12]} 的 ci_evidence 这一行不在历史里"
        assert a_row == a_ci, (
            f"{sha[:12]} 的 CI 证据由 {a_ci[:12]} 带进来，而台账那一行由 {a_row[:12]} 引入 ⇒ "
            f"证据与它的主张不同车（可以事后换一份证据再指过去）")
        return
    assert (ROOT / rec).is_file(), f"{sha[:12]} 没有回执 {rec}（CI 证据必须与回执同一个载体）"
    a_rec = _added_by(rec)
    assert a_ci == a_rec, (
        f"{sha[:12]} 的 CI 证据由 {a_ci[:12]} 带进来，回执由 {a_rec[:12]} 带进来 ⇒ 不同源："
        f"证据可以在事后换成另一份（载体必须跟着回执走）")


# ---------- 判据 6：同 sha 的红 run 不许被绿 run 盖掉 ----------

def superseded_unmentioned(doc: dict, prose: str) -> list[int]:
    """同 sha 里**非 success** 的 run，必须在台账散文里被点名。

    第八十九批实测口子：`10738df` 的 master 上有两条跑完的 push run——`38957513542`（红，
    推前形状守卫判的几何）与重推后的 `38958907685`（绿）。生产端此前只摘最新一条，
    产物里搜不到那条红（`grep -c 38957513542 evidence/ci/*.json` ⇒ 0）⇒ 「拿一条绿盖掉
    同 sha 的红」在审计看来与真的全绿**同形**。现在：全部 run 落进 `runs`，且红的那条
    必须在台账散文里被点名（点名 = 有人对它负过责）。
    """
    out: list[int] = []
    for info in (doc.get("branches") or {}).values():
        for r in info.get("runs") or []:
            if r.get("run_id") == info.get("run_id"):
                continue
            if str(r.get("conclusion") or "") == "success":
                continue
            if str(r.get("run_id")) not in prose:
                out.append(int(r["run_id"]))
    return out


def test_牙_没说出口的红_run_必须被抓住():
    """正/反向对照：判据得真能分辨「点了名」与「被盖掉」。"""
    doc = {"branches": {"master": {"run_id": 2, "runs": [
        {"run_id": 1, "conclusion": "failure"},
        {"run_id": 2, "conclusion": "success"}]}}}
    assert superseded_unmentioned(doc, "只有绿的那条") == [1]
    assert superseded_unmentioned(doc, "master 的 run 1 因几何红，重推后 2 绿") == []
    all_green = {"branches": {"dev": {"run_id": 3, "runs": [
        {"run_id": 3, "conclusion": "success"}]}}}
    assert superseded_unmentioned(all_green, "") == []


@pytest.mark.parametrize("row", rows_with_ci(), ids=lambda r: str(r.get("sha"))[:12])
def test_同_sha_的红_run_必须在台账里点名(row: dict):
    sha = str(row["sha"])
    unseen = superseded_unmentioned(load_ci(row), row_block(sha))
    assert not unseen, (
        f"{sha[:12]} 的 CI 证据记着同 sha 的非 success run {unseen}，而台账那一行一个字都没提 ⇒ "
        f"拿绿盖红（删 run 是洗证据，不点名也是）")


# ---------- 红账（verdict: red）的四条子规则，各自单独撞一遍 ----------

def test_牙_红账没写原因_必须被点名():
    sha = "a1" * 20
    bad = unproven_violations([_entry(sha, verdict="red", is_last=True, fixed_by="未修")],
                              after_cut={sha}, allowed={}, max_entries=3)
    assert bad and any("没写 red_why" in i for i in bad), bad


def test_牙_红账带证据或带回执_都必须被点名():
    sha = "a2" * 20
    bad = unproven_violations(
        [_entry(sha, ev=True, verdict="red", red_why="x", is_last=True, fixed_by="未修")],
        after_cut={sha}, allowed={}, max_entries=3)
    assert bad and any("自相矛盾" in i for i in bad), bad
    bad2 = unproven_violations(
        [_entry(sha, rec=True, verdict="red", red_why="x", is_last=True, fixed_by="未修")],
        after_cut={sha}, allowed={}, max_entries=3)
    assert bad2 and any("不能留交付回执" in i for i in bad2), bad2


def test_牙_旧红账不在最后一行_必须被点名():
    a, b = "a3" * 20, "a4" * 20
    bad = unproven_violations(
        [_entry(a, verdict="red", red_why="x", fixed_by="未修", is_last=False),
         _entry(b, ev=True)],
        after_cut={a, b}, allowed={}, max_entries=3)
    assert bad and any("不是台账最后一行" in i for i in bad), bad


def test_牙_结清者必须是真提交且真的接走这一行():
    a = "a5" * 20
    bad = unproven_violations(
        [_entry(a, verdict="red", red_why="x", fixed_by="f" * 40, fixed_by_exists=False)],
        after_cut={a}, allowed={}, max_entries=3)
    assert bad and any("结清者必须是真提交" in i for i in bad), bad
    bad2 = unproven_violations(
        [_entry(a, verdict="red", red_why="x", fixed_by="f" * 40,
                fixed_by_exists=True, fixed_by_carries_row=False)],
        after_cut={a}, allowed={}, max_entries=3)
    assert bad2 and any("没有把这一行接走" in i for i in bad2), bad2
    ok = unproven_violations(
        [_entry(a, verdict="red", red_why="x", fixed_by="f" * 40,
                fixed_by_exists=True, fixed_by_carries_row=True)],
        after_cut={a}, allowed={}, max_entries=3)
    assert ok == [], ok


def test_牙_红账必须带期限_且期限过期就要重新表态():
    a = "d7" * 20
    # 1) 没写 review_by：红账就成了没有期限的洞
    bad = unproven_violations(
        [_entry(a, verdict="red", red_why="x", fixed_by="未修", is_last=True, review_by=None)],
        after_cut={a}, allowed={}, max_entries=3)
    assert bad and any("没写 review_by" in i for i in bad), bad
    # 2) 不是日期：写「下周再看」这种话不算期限
    bad2 = unproven_violations(
        [_entry(a, verdict="red", red_why="x", fixed_by="未修", is_last=True, review_by="下周")],
        after_cut={a}, allowed={}, max_entries=3)
    assert bad2 and any("不是 YYYY-MM-DD" in i for i in bad2), bad2
    # 3) 日期已过期：红账变永久，必须重新表态（结清或续期）
    bad3 = unproven_violations(
        [_entry(a, verdict="red", red_why="x", fixed_by="未修", is_last=True,
                review_by="2000-01-01")],
        after_cut={a}, allowed={}, max_entries=3)
    assert bad3 and any("已过期" in i for i in bad3), bad3
    # 4) 未来日期：不报
    ok = unproven_violations(
        [_entry(a, verdict="red", red_why="x", fixed_by="未修", is_last=True,
                review_by="2999-01-01")],
        after_cut={a}, allowed={}, max_entries=3)
    assert ok == [], ok


def test_牙_红账成常态_必须被点名():
    rows = [_entry(f"c{i}" + "0" * 38, verdict="red", red_why="x", fixed_by="未修",
                   is_last=(i == 2)) for i in (1, 2)]
    bad = unproven_violations(rows, after_cut={r["sha"] for r in rows}, allowed={},
                              max_entries=3)
    assert bad and any("红账成了常态" in i for i in bad), bad


def test_牙_已结清的红账不占额度_但在途的仍然占():
    """上限的语义是「在途的红账最多一行」。

    两个方向都要锁：
    ① 一行红账已结清（`fixed_by` 是真提交、且那个提交把它接走了）+ 一行在途 ⇒ **不**该报「常态」；
    ② 两行都在途 ⇒ 必须报（防止有人靠把 `fixed_by` 写成一串 40 个 0 来蹭这条宽松）。
    """
    a, b, fix = "a" * 40, "b" * 40, "f" * 40
    closed = _entry(a, verdict="red", red_why="x", fixed_by=fix, is_last=False,
                    fixed_by_exists=True, fixed_by_carries_row=True)
    fix_row = _entry(fix, ev=True, is_last=False)
    open_one = _entry(b, verdict="red", red_why="x", fixed_by="未修", is_last=True)
    ok = unproven_violations([closed, fix_row, open_one], after_cut={a, b}, allowed={},
                             max_entries=3)
    assert not any("红账成了常态" in i for i in ok), ok

    # 反向对照：两行都在途 ⇒ 仍然要红（否则「不占额度」就成了绕过上限的后门）
    open_two = _entry(a, verdict="red", red_why="x", fixed_by="未修", is_last=False)
    bad = unproven_violations([open_two, open_one], after_cut={a, b}, allowed={},
                              max_entries=3)
    assert any("红账成了常态" in i for i in bad), bad
