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

def reconcile_rows(rows_in: list[dict], root: pathlib.Path) -> list[str]:
    """把「台账行 ⇄ 它指向的回执」对账，返回所有不一致。**纯读**：不碰 git，可在构造数据上跑。

    为什么抽成函数（第八十二批补，原先是内联在用例里）：内联时用例读的是真台账，
    它只能报「没问题」；检查器本身被弱化（少对一类账）时真台账照样绿 —— 没有牙。
    抽出来后用 `test_对账检查器认得五类坏行` 拿构造数据把每一类钉死。

    对账四项（前两项本来就有，后三项是这一批补的）：
      · sha/tree 绑定；shape_after = 行里的 shape；
      · `keys` —— **真正的树指纹**（CHANGELOG.md:226-228：`shape` 只是 `git status` 空指纹，
        干净树上恒等于 `e3b0c44298fc1c14`，拿它当指纹等于白对）；
      · `freeze.tree_before/tree_after` 必须与行里的 tree 相等（回执指向的树不能是另一棵）；
      · `receipt`（仓外日志名）必须与回执里记的 `freeze.log` **大小写敏感**一致。
    回执里那四个 hex 是冻结日志的机械抽取，且回执记着日志的 `log_sha256` ⇒ 回执权威；
    行对不上就是行抄错（第八十二批实测两例：`b040ae6c` 的 keys、`814eeb07` 的 receipt 大小写）。
    """
    paths = sorted((root / "receipts").glob("*.yaml"))
    by_sha = {load(p)["sha"]: load(p) for p in paths}
    problems = []
    for row in rows_in:
        sha, tree = row["sha"], row["tree"]
        if row.get("attestation") == "git-only":
            # git-only 行只准有 sha/tree/attestation/frozen_proof/说明 —— 没回执就没有任何东西
            # 能证 shape/keys/receipt 这几个数（自己报的数就是自己报的数）；None 不算自报。
            claimed = [k for k in ("shape", "keys", "receipt", "frozen_receipt") if row.get(k)]
            if row.get("frozen_proof") != "none" or claimed:
                problems.append(
                    f"{sha[:12]} git-only 行不许自报数字/回执（frozen_proof="
                    f"{row.get('frozen_proof')!r}，自报 {claimed}）")
            continue
        if row.get("frozen_proof") != "ci":
            problems.append(f"{sha[:12]} attestation={row.get('attestation')} 但 frozen_proof≠ci")
        ref = row.get("frozen_receipt")
        if not ref:
            problems.append(f"{sha[:12]} 缺 frozen_receipt:")
            continue
        rel = str(ref).replace("\\", "/")
        # 路径必须限定在 receipts/ 下（不只是一句「文件存在」）：绝对路径与 `..` 能把手伸到仓外
        if rel.startswith("/") or ".." in rel.split("/") or not rel.startswith("receipts/"):
            problems.append(f"{sha[:12]} frozen_receipt={rel!r} 不是 receipts/ 下的仓内相对路径")
            continue
        p = root / rel
        if not p.is_file():
            problems.append(f"{sha[:12]} receipt: {rel} 不存在")
            continue
        d = load(p)
        if d["sha"] != sha or d["tree"] != tree:
            problems.append(f"{sha[:12]} 与 {rel} 的 sha/tree 不一致")
        fz = d.get("freeze") or {}
        # 三对账：跑前/跑后逐项相等（回执自己的用例也查一遍），且都等于行里记的值
        for k in ("tree", "shape", "keys"):
            before, after = fz.get(f"{k}_before"), fz.get(f"{k}_after")
            if before != after:
                problems.append(
                    f"{sha[:12]} 回执 {rel} 的 freeze.{k}_before≠{k}_after ⇒ 冻结期间树动过")
            if row.get(k) and before != row.get(k):
                problems.append(f"{sha[:12]} 行的 {k}={row.get(k)} 但回执 {k}_before={before}")
        if row.get("receipt") and fz.get("log") != row.get("receipt"):
            problems.append(
                f"{sha[:12]} 行写 receipt={row.get('receipt')!r}，"
                f"但回执记的日志是 {fz.get('log')!r}"
            )
        if sha not in by_sha:
            problems.append(f"{sha[:12]} 有 receipt 字段但那份回执不在 receipts/")
    return problems


def test_台账每一行与回执一致():
    problems = reconcile_rows(rows(), ROOT)
    assert not problems, "台账与回执不一致：\n  " + "\n  ".join(problems)


def test_对账检查器认得五类坏行(tmp_path: pathlib.Path):
    """判据有牙：五类坏行喂给检查器，每一类都必须被指出；正对照（好行）必须一句不说。

    为什么必须有这条：真台账用例只能报「没问题」，检查器被弱化时它照样绿。
    没有这条，下面五类账的对账全部是「看起来在核」—— 本仓刚在沙箱注入上栽过同一个跟头
    （变异装置的「原文不唯一 ⇒ SKIP」被当成通过）。
    """
    sha = "a" * 40
    receipt_rel = f"receipts/{sha[:12]}.yaml"
    (tmp_path / "receipts").mkdir()

    def good_doc() -> dict:
        return {
            "sha": sha,
            "tree": "b" * 40,
            "freeze": {
                "log": "freeze80z.log",
                "tree_before": "b" * 40, "tree_after": "b" * 40, "tree_same": True,
                "shape_before": "c" * 16, "shape_after": "c" * 16, "shape_same": True,
                "keys_before": "d" * 16, "keys_after": "d" * 16, "keys_same": True,
            },
        }

    def good_row() -> dict:
        return {
            "sha": sha, "tree": "b" * 40, "attestation": "git+frozen", "frozen_proof": "ci",
            "frozen_receipt": receipt_rel, "shape": "c" * 16, "keys": "d" * 16,
            "receipt": "freeze80z.log",
        }

    def check(doc: dict, row: dict) -> list[str]:
        (tmp_path / receipt_rel).write_text(yaml.safe_dump(doc), encoding="utf-8")
        return reconcile_rows([row], tmp_path)

    assert check(good_doc(), good_row()) == [], "正对照：好行不该被挑出任何问题"

    d = good_doc()
    d["freeze"]["keys_before"] = d["freeze"]["keys_after"] = "e" * 16
    assert check(d, good_row()), "keys 对不上（真正的树指纹）必须被指出来"

    d = good_doc()
    d["freeze"]["log"] = "freeze80Z.log"
    assert check(d, good_row()), "仓外日志名大小写不一致必须被指出来（CI 在 Linux 上跑）"

    d = good_doc()
    d["freeze"]["tree_before"] = d["freeze"]["tree_after"] = "f" * 40
    assert check(d, good_row()), "回执指向另一棵树必须被指出来"

    d = good_doc()
    d["freeze"]["keys_after"] = "0" * 16
    assert check(d, good_row()), "跑前/跑后 keys 不相等（冻结期间树动过）必须被指出来"

    row = good_row()
    row["frozen_receipt"] = "../../etc/passwd"
    # 只断言「不是空的」不够：仓外路径本来就不存在，去掉路径限定后仍会被「不存在」这句揩下去
    # （变异装置实测：这一条曾 SURVIVED）。必须断言报出来的是**路径越狱**这个理由。
    probs = check(good_doc(), row)
    assert any("不是 receipts/ 下的仓内相对路径" in p for p in probs), (
        f"回执路径越狱必须被当成越狱报出来（而不是含混的「不存在」），实得：{probs}")
    row = good_row()
    row["frozen_receipt"] = "receipts/../tests/DELIVERY_LEDGER.yaml"
    probs = check(good_doc(), row)
    assert any("不是 receipts/ 下的仓内相对路径" in p for p in probs), (
        f"`..` 走到仓内其它文件也必须被当成越狱，实得：{probs}")

    # git-only 行：正对照不能报（否则「git-only 也得有回执」这种回退不被发现），
    # 而它要是自报 shape/keys/receipt（没回执就没有任何东西能证这四个数字）就必须报。
    git_only = {"sha": sha, "tree": "b" * 40, "attestation": "git-only",
                "frozen_proof": "none"}
    assert check(good_doc(), git_only) == [], "正对照：干净的 git-only 行不该被挑出问题"
    for field, value in (("frozen_receipt", receipt_rel), ("shape", "c" * 16),
                         ("keys", "d" * 16), ("receipt", "freeze80z.log")):
        dirty = dict(git_only, **{field: value})
        assert check(good_doc(), dirty), f"git-only 行自报 {field} 必须被指出来（没回执也敢写）"


def test_没有孤儿回执():
    """receipts/ 里每一份都必须被台账某一行引用 —— 防「塞一份没主的回执进来」。"""
    referenced = {r.get("frozen_receipt") for r in rows() if r.get("frozen_receipt")}
    ci_sha = {r["sha"] for r in rows() if r.get("frozen_proof") == "ci"}
    orphans = [p.name for p in receipt_paths()
               if p.relative_to(ROOT).as_posix() not in referenced
               and load(p)["sha"] not in ci_sha]
    assert not orphans, f"没有人认领的回执：{orphans}"
