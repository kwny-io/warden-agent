"""交付回执的**机器生成**版：回执里每个数字都由产物推出，作者一个字都不写。

要治的缺口（`tests/test_delivery_receipts.py` 头部自己承认的那条）：

    冻结回执住在仓库外（`wa78/freeze78k.log` 这种），**可以在事后补写而不被任何机械判定发现**。

第七十八批第九处的现场：回执写着「tree `e1ddf6a3…`」，而实际交付的是 `83891221…`
——**回执指到了被作废的那次冻结**。语法对、文件在、提交也在，任何文本守卫都抓不到。

这份脚本把「回执」从**人写的地方**搬到**跑出来的地方**：

| 字段 | 谁给的 |
|---|---|
| `sha` / `tree` | `git rev-parse`（作者不能写） |
| `pytest.{tests,failures,errors,skipped,passed}` | junit XML（pytest 自己写的） |
| `pytest.rc` | 子进程退出码（不是人填的 0） |
| `coverage_percent` | `--cov` 的 `Total coverage: N%`（与 CI 同一组参数；抽不到就拒绝） |
| `freeze.shape_before/after` | `git status --porcelain` 的指纹（干净树 = `e3b0c442…`） |
| `freeze.keys_before/after` | `_snapshot_routes.py` 的路由+OpenAPI 快照哈希（**真树指纹**） |
| `freeze.tree_before/after` | 跑前/跑后两次 `git rev-parse <sha>^{tree}` |

所以「冻结期间树没变」不再是作者的一句声明，而是 `tree_same/shape_same/keys_same` 三个
**算出来的**布尔值；只要任何一个不为真，回执本身就写不出来（`--run` 退出码非 0）。

用法（只有一种：让它自己跑）：
    python scripts/freeze_receipt.py --run [--phase f2] [--sha <commit>]

**没有**「拿现成 junit 生成回执」的模式 —— 那种模式等于把数字交回给作者，正是本脚本要消灭的东西。
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COVERAGE_RE = re.compile(r"Total coverage:\s*([0-9]+(?:\.[0-9]+)?)%")
# 干净树的 `git status --porcelain` 指纹：sha256("") 的前 16 位。
EMPTY_SHAPE = hashlib.sha256(b"").hexdigest()[:16]


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True
    ).stdout.strip()


def _is_tracked(rel: str) -> bool:
    """`git ls-files --error-unmatch` 对未跟踪文件**本来就**返回 1 ⇒
    不能走 `_git`（它 check=True 会抛）。"""
    if not rel:
        return False
    proc = subprocess.run(
        ["git", "ls-files", "--error-unmatch", rel],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return proc.returncode == 0


def _shape() -> str:
    """`git status --porcelain` 的指纹（干净树恒等于 EMPTY_SHAPE）。"""
    return hashlib.sha256(_git("status", "--porcelain").encode()).hexdigest()[:16]


def _keys() -> str:
    """路由表 + OpenAPI 快照的哈希 —— 这才是能对账的树指纹（`shape` 只看得见工作区脏不脏）。"""
    with tempfile.TemporaryDirectory() as out:
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "_snapshot_routes.py"), out],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
        digest = hashlib.sha256()
        for path in sorted(Path(out).rglob("*")):
            if path.is_file():
                digest.update(path.name.encode())
                digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


# 与 `.github/workflows/ci.yml:191` 的 quality 步骤逐项对齐：
# `--cov` 同时执行覆盖率门禁（`fail_under` 在 pyproject.toml，单一事实源）；
# `-rs` 让跳过原因进日志；`--tb=short` 让人能读。**回执背的书必须是 CI 会重复的那一次**。
CI_PYTEST_FLAGS = ("--tb=short", "-rs", "-p", "no:cacheprovider", "--cov=warden_agent")


def _counts(junit: Path) -> dict[str, int]:
    root = ET.parse(junit).getroot()
    suite = root if root.tag == "testsuite" else root[0]
    def get(key: str) -> int:
        return int(suite.get(key) or 0)

    tests, failures, errors, skipped = get("tests"), get("failures"), get("errors"), get("skipped")
    return {
        "tests": tests,
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
        "passed": tests - failures - errors - skipped,
    }


def build_receipt(
    *, sha: str, tree: str, phase: str, log_name: str, log_sha256: str,
    counts: dict[str, int], rc: int, coverage: float,
    tree_before: str, tree_after: str,
    shape_before: str, shape_after: str, keys_before: str, keys_after: str,
    workspace_untracked: list[str] | None = None,
    pytest_args: list[str] | None = None,
) -> dict[str, object]:
    """纯函数：给定实测值拼回执。判据可以拿构造值直接测它，不用真跑 pytest。"""
    same = {
        "tree_same": tree_before == tree_after == tree,
        "shape_same": shape_before == shape_after,
        "keys_same": keys_before == keys_after,
    }
    return {
        "sha": sha,
        "tree": tree,
        "freeze": {
            "log": log_name,
            "log_sha256": log_sha256,
            "phase": phase,
            "tree_before": tree_before,
            "tree_after": tree_after,
            **same,
            "shape_before": shape_before,
            "shape_after": shape_after,
            "keys_before": keys_before,
            "keys_after": keys_after,
            # 冻结时工作区里**未跟踪**的文件：写出来让人自己判断，不静默放过。
            "workspace_untracked": list(workspace_untracked or []),
        },
        "pytest": {**counts, "rc": rc, "args": list(pytest_args or CI_PYTEST_FLAGS)},
        "coverage_percent": coverage,
        # 绿的唯一条件是「跑完了 + 没失败 + 三对账都真」；任何一条不成立就写 red，
        # 而红回执不许入库（`tests/test_delivery_receipts.py::test_回执是绿的`）。
        "verdict": "green"
        if rc == 0 and counts["failures"] == 0 and counts["errors"] == 0 and all(same.values())
        else "red",
    }


# 矩阵证据的三种行（与 `tests/test_matrix_evidence.py` 里的守卫正则同源）。
# 这里不是「重新发明一套格式」，而是把生产者侧也写成可校验的：写不对就拒写回执。
M_HDR = re.compile(r"^=== matrix\S* \S+ HEAD=([0-9a-f]{40}) ===$", re.M)
M_PASS = re.compile(
    r"^MATRIX (PASS|FAIL): (\d+)/(\d+) 符合预期"
    r"（(\d+) 条正对照 \+ (\d+) 条反向对照(?:；[^）]*)?）$", re.M)
M_SHALLOW = re.compile(r"^SHALLOW depth=1: rc=(\d+) 红 (\d+) 条", re.M)
M_ROW_M = re.compile(r"^M\d+ .*$", re.M)
M_ROW_R = re.compile(r"^R\d+ .*$", re.M)
M_SKIP = "SKIP（"


def mutation_block(path: Path, sha: str) -> dict[str, object]:
    """把矩阵证据日志解析成回执的 `mutation` 字段；任何不自信的地方都抛 ValueError。

    这里故意把「不老实」全当成错误而不是警告：回执是拿来背书的，宁可不写。
    """
    if not path.is_file():
        raise ValueError(f"证据文件不存在：{path}")
    text = path.read_text(encoding="utf-8", errors="replace")
    hdr = M_HDR.search(text)
    if hdr is None:
        raise ValueError("没有 `=== matrix… HEAD=<40 位 sha> ===` 头行")
    if hdr.group(1) != sha:
        raise ValueError(f"证据自称跑在 {hdr.group(1)[:12]}，而回执要背书 {sha[:12]}")
    pm = M_PASS.search(text)
    if pm is None:
        raise ValueError("没有 `MATRIX PASS/FAIL: n/m 符合预期` 汇总行")
    if pm.group(1) != "PASS":
        raise ValueError("MATRIX FAIL 不许进回执（有缺口就不是证据）")
    matched, total = int(pm.group(2)), int(pm.group(3))
    positive, negative = int(pm.group(4)), int(pm.group(5))
    rows = M_ROW_M.findall(text) + M_ROW_R.findall(text)
    live = [r for r in rows if M_SKIP not in r]
    if matched != total:
        raise ValueError(f"matched={matched} ≠ total={total}")
    if positive + negative != total:
        raise ValueError(f"正 {positive} + 反 {negative} ≠ 总 {total}（有臂没跑却算进通过了）")
    if len(live) != total:
        raise ValueError(f"非 SKIP 的行 {len(live)} 条 ≠ 总数 {total}（SKIP 不计入，也不许白占）")
    sh = M_SHALLOW.search(text)
    if sh is None:
        raise ValueError("没有 `SHALLOW depth=1: rc=N 红 K 条` 行 ⇒ 没证明 fetch-depth: 0 承重")
    if int(sh.group(1)) != 1:
        raise ValueError(f"浅检出 rc={sh.group(1)}（期望 1）⇒ 判据在浅克隆里不承重")
    if int(sh.group(2)) < 1:
        raise ValueError("浅检出红条数为 0 ⇒ 自我否定的证据")
    rel = path.resolve().relative_to(ROOT.resolve()).as_posix()
    if rel != f"evidence/matrix/{sha[:12]}.log":
        raise ValueError(f"证据得叫 evidence/matrix/{sha[:12]}.log，而不是 {rel}")
    return {
        "evidence": rel,
        "evidence_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "total": total,
        "matched": matched,
        "positive": positive,
        "negative": negative,
        "shallow_red": int(sh.group(2)),
    }


def _dump(receipt: dict[str, object]) -> str:
    head = (
        "# 交付回执 —— 由 scripts/freeze_receipt.py --run 机器生成。\n"
        "# 这里每个数字都从产物推出（git / junit / 子进程退出码 / 快照哈希），作者不写数字。\n"
        "# 因此它对「事后补写」免疫：补写的人得先造一份 log 与一份 junit，而 log_sha256 对不上。\n"
    )
    return head + yaml.safe_dump(receipt, allow_unicode=True, sort_keys=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="跑全套 pytest 并当场生成回执")
    parser.add_argument("--sha", default="HEAD", help="要背书的提交（默认 HEAD）")
    parser.add_argument("--phase", default="f1", help="冻结阶段标记，写进 freeze.phase")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的回执（默认拒绝）")
    parser.add_argument(
        "--allow-untracked",
        action="store_true",
        help="允许工作区有未跟踪文件（会写进回执的 freeze.workspace_untracked，不是静默放过）",
    )
    parser.add_argument("--pytest-args", default="", help="附加给 pytest 的参数（分片用）")
    parser.add_argument(
        "--mutation-evidence",
        default="",
        help="矩阵证据日志（evidence/matrix/<sha12>.log）；给了就写进回执的 mutation 字段，不合格则拒写",
    )
    args = parser.parse_args(argv)

    if not args.run:
        print("只支持 --run：不接受「拿现成 junit 生成回执」。", file=sys.stderr)
        return 2

    if _git("status", "--porcelain"):
        dirty = [line[3:] for line in _git("status", "--porcelain").splitlines() if line.strip()]
        untracked = [p for p in dirty if (ROOT / p).is_file() and not _is_tracked(p)]
        other = [p for p in dirty if p not in untracked]
        if other or not args.allow_untracked:
            print(
                "工作区不干净：冻结没有意义（回执要背的书还没定型）。先提交，再跑。\n"
                f"  未跟踪：{untracked}\n  其它改动：{other}",
                file=sys.stderr,
            )
            return 2
        untracked_at_freeze = untracked
    else:
        untracked_at_freeze = []

    sha = _git("rev-parse", args.sha)
    sha12 = sha[:12]
    out = ROOT / "receipts" / f"{sha12}.yaml"
    if out.exists() and not args.force:
        print(f"{out.name} 已存在；确认要重跑就加 --force。", file=sys.stderr)
        return 2

    log_name = f"freeze-{sha12}.log"
    log_path = ROOT / ".pi" / log_name
    log_path.parent.mkdir(parents=True, exist_ok=True)

    tree_before = _git("rev-parse", f"{args.sha}^{{tree}}")
    shape_before, keys_before = _shape(), _keys()

    junit = ROOT / ".pi" / f"junit-{sha12}.xml"
    cmd = [
        sys.executable, "-m", "pytest", *CI_PYTEST_FLAGS, "tests",
        f"--junitxml={junit}", "-o", "junit_family=xunit2",
    ] + (args.pytest_args.split() if args.pytest_args else [])
    print(f"跑：{' '.join(cmd[1:])}", flush=True)
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    log_text = proc.stdout + proc.stderr
    log_path.write_text(log_text, encoding="utf-8")

    tree_after, shape_after, keys_after = (
        _git("rev-parse", f"{args.sha}^{{tree}}"), _shape(), _keys()
    )
    counts = _counts(junit)
    match = COVERAGE_RE.search(log_text)
    if match is None:
        print("没抽到 `Total coverage: N%` ⇒ 拒绝写回执（不猜数字）。", file=sys.stderr)
        print(log_text[-2000:], file=sys.stderr)
        return 2

    receipt = build_receipt(
        sha=sha,
        tree=tree_before,
        phase=args.phase,
        log_name=log_name,
        log_sha256=hashlib.sha256(log_path.read_bytes()).hexdigest(),
        counts=counts,
        rc=proc.returncode,
        coverage=float(match.group(1)),
        tree_before=tree_before,
        tree_after=tree_after,
        shape_before=shape_before,
        shape_after=shape_after,
        keys_before=keys_before,
        keys_after=keys_after,
        workspace_untracked=untracked_at_freeze,
        pytest_args=CI_PYTEST_FLAGS,
    )
    if args.mutation_evidence:
        try:
            receipt["mutation"] = mutation_block(Path(args.mutation_evidence), sha)
        except ValueError as exc:
            print(f"--mutation-evidence 不合格 ⇒ 拒绝写回执：{exc}", file=sys.stderr)
            return 2

    out.write_text(_dump(receipt), encoding="utf-8")
    print(f"回执：{out.relative_to(ROOT)}  verdict={receipt['verdict']}  "
          f"tests={counts['tests']} passed={counts['passed']} skipped={counts['skipped']} "
          f"coverage={match.group(1)}%")
    print(f"日志：{log_path.relative_to(ROOT)}（sha256 已写进回执，事后补写会对不上）")
    return 0 if receipt["verdict"] == "green" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
