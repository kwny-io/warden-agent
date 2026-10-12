"""交付回执守卫：把「回执说它验的是哪棵树」变成 **CI 自己会推** 的东西（第八十一批 m02098）。

要治的缺口（第七十九批文档承认的诚实空白）：
    冻结回执住在仓库外（`wa78/freeze78k.log` 这种），**可以在事后补写而不被任何机械判定发现**。

三条判据，全部由 git 推导，作者不能在文件里自述：
  1. 绑定：`receipts/<sha12>.yaml` 的 `tree` == `git rev-parse <sha>^{tree}`。
  2. 背书顺序（唯一的牙齿）：回执必须由 `<sha>` 的**唯一子提交**带进来
     —— 入库提交 A == 子提交 C。⇒ 回执只能在「该提交还是 HEAD」的窗口里写，
     **事后补写做不到**。判据靠 git 推，文件里不写 `carried_by:` 之类自述字段。
  3. 仍然入库的历史行：必须**显式声明**在台账 `backfill_receipts:` 里，
     否则红 ⇒ 偷偷塞回执是**可见的**，不是静默的。

白名单按**拓扑**位置，不按时间：台账里最早的 `next-commit` 回执的入库提交 = 切点，
只有切点之前的行才允许 `backfill`（第八十一批只有 X81 是 next-commit，其余 8 行是 backfill）。
"""

from __future__ import annotations

import pathlib
import re
import subprocess

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
RECEIPTS = ROOT / "receipts"
LEDGER = ROOT / "tests" / "DELIVERY_LEDGER.yaml"

REQUIRED_TOP = {"sha", "tree", "freeze", "pytest", "coverage_percent", "verdict"}
REQUIRED_FREEZE = {"log", "log_sha256", "phase", "tree_before", "tree_after",
                   "tree_same", "shape_same", "keys_same"}
REQUIRED_PYTEST = {"tests", "failures", "errors", "passed", "skipped", "rc"}
# 判据 3 的反面：回执里**不许**自述信任等级/载体 —— 那等于把判据交回给作者。
FORBIDDEN_KEYS = {"carried_by", "trust", "trust_level", "backing", "endorsed_by"}
SHA12 = re.compile(r"^[0-9a-f]{12}\.yaml$")


def git(*args: str) -> str:
    r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 0, f"git {' '.join(args)} 失败：{r.stderr.strip()}"
    return r.stdout.strip()


def receipt_paths() -> list[pathlib.Path]:
    return sorted(RECEIPTS.glob("*.yaml"))


def load(p: pathlib.Path) -> dict:
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def ledger() -> dict:
    return yaml.safe_load(LEDGER.read_text(encoding="utf-8"))


def rows() -> list[dict]:
    return ledger().get("rows", [])


def carrier(sha: str) -> str:
    """把 sha 带进 HEAD 历史的那个提交（唯一子提交）。

    坑：`rev-list --children --max-count=1 <sha>` **不成立** —— 截到 sha 自己，
    集合里根本没有它的子提交，`--children` 无从标注，于是恒报「没有子提交」。
    正确做法是从 HEAD 走全历史，再按 sha 找回那一行。
    （也正因为要读全历史，CI 的 quality job 必须 `fetch-depth: 0`。）
    """
    for line in git("rev-list", "--children", "HEAD").splitlines():
        parts = line.split()
        if parts and parts[0] == sha:
            assert len(parts) >= 2, f"{sha[:12]} 没有子提交 ⇒ 它还是 HEAD，回执要等新提交带进来"
            return parts[1]
    raise AssertionError(f"{sha[:12]} 不在 HEAD 的历史里（浅检出？CI 要 fetch-depth: 0）")


def added_by(path: str) -> str:
    return git("log", "--diff-filter=A", "--format=%H", "-1", "--", path)


def is_ancestor(sha: str) -> bool:
    r = subprocess.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", sha, "HEAD"],
                       capture_output=True)
    return r.returncode == 0


def backfill_declared() -> set[str]:
    return {str(s) for s in (ledger().get("backfill_receipts") or [])}


# ---------- 判据 0：形状 ----------

def test_回执目录存在且非空():
    assert RECEIPTS.is_dir(), f"缺 {RECEIPTS}（回执入库是这一批的目的，空目录等于没做）"
    assert receipt_paths(), "receipts/ 里一份回执都没有"


@pytest.mark.parametrize("p", receipt_paths(), ids=lambda p: p.name)
def test_文件名就是被证明的提交(p: pathlib.Path):
    assert SHA12.match(p.name), f"{p.name} 不是 <sha12>.yaml"
    assert load(p)["sha"].startswith(p.stem), f"{p.name} 里的 sha 不是 {p.stem} 开头"


@pytest.mark.parametrize("p", receipt_paths(), ids=lambda p: p.name)
def test_字段齐全_且不自述信任等级(p: pathlib.Path):
    d = load(p)
    assert set(d) >= REQUIRED_TOP, f"{p.name} 缺字段 {REQUIRED_TOP - set(d)}"
    mf = REQUIRED_FREEZE - set(d["freeze"])
    mp = REQUIRED_PYTEST - set(d["pytest"])
    assert set(d["freeze"]) >= REQUIRED_FREEZE, f"{p.name} freeze 缺 {mf}"
    assert set(d["pytest"]) >= REQUIRED_PYTEST, f"{p.name} pytest 缺 {mp}"
    bad = FORBIDDEN_KEYS & (set(d) | set(d["freeze"]))
    assert not bad, (f"{p.name} 里出现了自述字段 {sorted(bad)}：载体必须由 CI 从 git 推导，"
                     f"文件自述等于把判据交回作者")


# ---------- 判据 1：绑定到不可变对象 ----------

@pytest.mark.parametrize("p", receipt_paths(), ids=lambda p: p.name)
def test_tree_等于该提交的树(p: pathlib.Path):
    d = load(p)
    assert git("rev-parse", f"{d['sha']}^{{tree}}") == d["tree"], (
        f"{p.name} 说它验的是 {d['tree'][:12]}，而 {d['sha'][:12]} 的树不是它")


@pytest.mark.parametrize("p", receipt_paths(), ids=lambda p: p.name)
def test_被证明的提交在历史里(p: pathlib.Path):
    assert is_ancestor(load(p)["sha"]), f"{p.name} 的 sha 不是 HEAD 的祖先"


# ---------- 判据 2：背书顺序（唯一的牙齿） ----------

@pytest.mark.parametrize("p", receipt_paths(), ids=lambda p: p.name)
def test_回执由下一个提交带进来(p: pathlib.Path):
    sha = load(p)["sha"]
    rel = p.relative_to(ROOT).as_posix()
    a = added_by(rel)
    if sha in backfill_declared():
        assert a, f"{p.name} 既声明了 backfill，又根本没在历史里被加过"
        # 反面：声明回填的，就不能同时是「被下一个提交带进来的」——否则白名单被用成了随手可用
        c2 = carrier(sha) if is_ancestor(sha) else ""
        assert not (c2 and a == c2), (
            f"{p.name} 声明了 backfill，但它的入库提交 {a[:12]} 正是它的下一个提交 {c2[:12]} ⇒ "
            f"它不是回填，不该占用 backfill 白名单（回填的定义是『入库晚于下一个提交』）")
        return
    c = carrier(sha)
    assert a == c, (
        f"{p.name} 的入库提交是 {a[:12]}，而 {sha[:12]} 的唯一子提交是 {c[:12]} ⇒ "
        f"回执不是被「下一个提交」带进来的（这正是『可以事后补写』那个缺口）")
    # 反面：入库提交必须晚于被证明的提交（防「自己带自己」）
    assert a != sha, f"{p.name} 由它自己的提交携带 ⇒ 自引用"


def test_切点之后的行一律不许_backfill():
    """白名单按拓扑：切点 = 最早一份 backfill 外回执的入库提交；切点（含）之后交付的行不该回填。"""
    cut = min((added_by(p.relative_to(ROOT).as_posix()) for p in receipt_paths()
               if load(p)["sha"] not in backfill_declared()), default=None)
    if not cut:
        pytest.skip("还没有任何 next-commit 回执（第一批入库时允许）")
    late = []
    for p in receipt_paths():
        sha = load(p)["sha"]
        if sha not in backfill_declared():
            continue
        r = subprocess.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", cut, sha],
                           capture_output=True)
        if r.returncode == 0:
            late.append(p.name)
    assert not late, (f"切点（{cut[:12]}）之后才交付的行却声明了 backfill：{late} ⇒ "
                      f"回执本来能由下一个提交带进来，回填白名单被用成了随手可用")


# ---------- 判据 3：入库存下来的回执必须是绿的 ----------

@pytest.mark.parametrize("p", receipt_paths(), ids=lambda p: p.name)
def test_回执是绿的(p: pathlib.Path):
    d = load(p)
    assert d["verdict"] == "green", f"{p.name} verdict={d['verdict']} ⇒ 红的回执不许入库"
    assert d["pytest"]["failures"] == 0 and d["pytest"]["errors"] == 0, f"{p.name} 有失败/错误"
    assert d["pytest"]["rc"] == 0, f"{p.name} pytest rc={d['pytest']['rc']}"
    for k in ("tree_same", "shape_same", "keys_same"):
        assert d["freeze"][k] is True, f"{p.name} 冻结前后 {k}=False ⇒ 冻结期间树变了"


# ---------- 与台账交叉核 ----------

def test_台账每一行与回执一致():
    by_sha = {load(p)["sha"]: load(p) for p in receipt_paths()}
    problems = []
    for row in rows():
        sha, tree = row["sha"], row["tree"]
        if row.get("attestation") == "git-only":
            if row.get("frozen_proof") != "none" or row.get("frozen_receipt"):
                problems.append(f"{sha[:12]} git-only 行不该有 frozen_proof/frozen_receipt")
            continue
        if row.get("frozen_proof") != "ci":
            problems.append(f"{sha[:12]} attestation={row.get('attestation')} 但 frozen_proof≠ci")
        ref = row.get("frozen_receipt")
        if not ref:
            problems.append(f"{sha[:12]} 缺 frozen_receipt:")
            continue
        p = ROOT / ref
        if not p.is_file():
            problems.append(f"{sha[:12]} receipt: {ref} 不存在")
            continue
        d = load(p)
        if d["sha"] != sha or d["tree"] != tree:
            problems.append(f"{sha[:12]} 与 {ref} 的 sha/tree 不一致")
        # 行里已经记着的 shape（冻结前后必须一致的那个签）必须等于回执里的 shape_after
        shape_row, shape_rec = row.get("shape"), d["freeze"].get("shape_after")
        if shape_row and shape_rec and shape_row != shape_rec:
            problems.append(f"{sha[:12]} 行的 shape={shape_row} 但回执 shape_after={shape_rec}")
        if sha not in by_sha:
            problems.append(f"{sha[:12]} 有 receipt 字段但那份回执不在 receipts/")
    assert not problems, "台账与回执不一致：\n  " + "\n  ".join(problems)


def test_没有孤儿回执():
    """receipts/ 里每一份都必须被台账某一行引用 —— 防「塞一份没主的回执进来」。"""
    referenced = {r.get("frozen_receipt") for r in rows() if r.get("frozen_receipt")}
    ci_sha = {r["sha"] for r in rows() if r.get("frozen_proof") == "ci"}
    orphans = [p.name for p in receipt_paths()
               if p.relative_to(ROOT).as_posix() not in referenced
               and load(p)["sha"] not in ci_sha]
    assert not orphans, f"没有人认领的回执：{orphans}"
