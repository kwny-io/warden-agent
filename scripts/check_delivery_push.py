"""推之前先看：这次 push 会不会在交付链上留下**永久拿不到证据**的提交（第八十三批现场）。

为什么要有这条（现场 = 2026-10-15 本仓实测）：

- GitHub 的 push 事件**只为这次 push 的 tip 建 run**。实测：`dev` 一次推上
  `904071e`（F3）/ `7cf9b9f`（探针）/ `5c0faa5`（自检）三个提交之后，
  `gh api repos/<repo>/actions/runs?head_sha=<sha>` 给出 `total_count = 0 / 0 / 1`
  ——两个**中间提交永远不会有自己的 run**。
- 而仓库里两条判据合起来要求：`tests/test_delivery_ledger.py` 判据 7：`HEAD^` 到
  `coverage_from` 的 first-parent 链上**每个提交都要有台账行**；
  `tests/test_ci_evidence.py`：切点之后**每一行**都要带 `evidence/ci/<该行 sha12>.json`，
  且那份证据只认 `head_sha` **精确等于**该提交的 run（这一条是用来杀串号的）。
- 两条相加 ⇒ 隐含契约是「交付范围里不许有非交付提交」+「每个交付提交必须单独 push」。
  一次推多个提交 = **结构上不可满足**，而且**不可逆**：推送那一刻就已经不是
  「重推一遍」能补的（没有新 push 事件 = 没有新 run = 没有证据），只能改写历史。
  这条契约此前**没有任何文档、也没有任何装置**——本批为此付了一个 force-push 的代价。

本守卫只把这件事**提前到推之前**说清楚。它不查网络、不查 CI：`git` 就能判，
判据是「拓扑 + 台账行」这两件本地事实。

判据（任一成立就是红）：

1. **一次推多个提交** ⇒ 除 tip 以外的提交拿不到 run ⇒ 它们的行永远拿不到证据。
2. **提交没有携带它 first parent 的台账行** ⇒ 它自己那棵树在 CI 里就是红的
   （判据 7 会报「覆盖范围内有洞」）⇒ 拿不到「逐 job 全绿」的证据。
3. tip 携带的父行若声明了 `ci_evidence`，那份文件必须真在 tip 的树里
   （回执与证据必须由**同一个**提交带进来）。

正确的顺序（本仓的交付节奏）：

    冻结 → 提交（携带上一提交的行 + 回执 + `evidence/ci/<上一 sha12>.json`）→ **只推这一个**
         → 等它自己的 run 全绿 → 渲染 `evidence/ci/<它的 sha12>.json` + 它的回执
         → 下一个提交携带它们 → 只推那一个 → …

用法：

    python scripts/check_delivery_push.py                    # base 取 @{u}，tip 取 HEAD
    python scripts/check_delivery_push.py --base 17cd5e5     # 显式 base（复核现场）
    python scripts/check_delivery_push.py --base <a> --tip <b>

退出码：0 = 形状对；1 = 这次 push 会留下不可取证的提交；2 = 用错或核不动（**不是**「没问题」）。

「起点不在树里」是第四种结局（第八十七批补）：改写历史后 `github.event.before` 那个旧 tip
不再被**任何** ref 可达 ⇒ CI 的检出里根本没有它（实锤：第八十六批把本分支压成一个提交再推，
push run 就报「核不动：git rev-parse c4b265f^{commit} → rc=128」，逐字见 CHANGELOG）。
这里**不能**整条收摊：判据 1 自己推荐的出路就是「改写历史把提交压成一个」——若判据在改写后
必然报红，那条出路就被它自己封死了，红会退化成噪声。所以退一步核 `tip^..tip`（判据 2/3 照样
生效），并把因此**没核到**的那部分（这次 push 到底推了几个提交）连同原因一起印出来：
「核不了」要看得见，但不等于「有罪」。

CI 里也在跑（`.github/workflows/ci.yml` 的「推前形状守卫（这次交付范围的形状）」那一步，
无条件跑）：范围按事件取——push = `github.event.before..github.sha`；PR = `base.sha..head.sha`
（判 PR 自己的提交，不判合成合并提交）；手动/定时没有「一次 push」这个概念，就判 tip 自己
（`sha^..sha`）。在 CI 里它只能**事后报警**（推出去就不可逆了），但报警仍然值：第八十三批
那次一次推三个提交，是到下一个提交才发现台账拿不到证据的；推完立刻看到这条红，就还来得及
在**再推之前**改写历史。
"""

from __future__ import annotations

import argparse
import contextlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[1]
LEDGER = "tests/DELIVERY_LEDGER.yaml"

#: 台账里「行」必须带的位置字段（只用它来定位一行，不在这里重述判据）。
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class Commit:
    """一次 push 里的一个提交：只留判据要用的两件事实。"""

    sha: str
    first_parent: str | None


def check_push(*, commits: list[Commit], rows_at: dict[str, set[str]]) -> list[str]:
    """纯函数：`commits` 按 first-parent 旧→新；`rows_at[sha]` = 该提交的树里台账**已有**的行 sha。

    为什么把数据当参数传，而不是自己读 git：这条判据要能用**构造数据**验
    （正对照 + 各类坏形状）。只靠「本仓现在恰好长什么样」等于把上一次的现场当判据。
    """
    out: list[str] = []
    if not commits:
        return out
    if len(commits) > 1:
        out.append(
            f"这次 push 带了 {len(commits)} 个提交（{[c.sha[:7] for c in commits]}）：GitHub 只为"
            " push 的 tip 建 run，中间的提交永远拿不到 `head_sha == 自己` 的 run ⇒ 它们的台账行"
            "永远拿不到 `evidence/ci/<sha12>.json`（判据只认精确匹配的那一份）。"
            "→ **一次只推一个提交**：把中间的改动并进下一个交付提交再推。"
        )
    for index, facts in enumerate(commits):
        if facts.first_parent is None:
            continue
        if facts.first_parent in rows_at.get(facts.sha, set()):
            continue
        if index == len(commits) - 1:
            out.append(
                f"{facts.sha[:7]}（tip）没有携带它 first parent {facts.first_parent[:7]} 的行 ⇒ "
                "它自己那棵树在 CI 里就会红（守卫会报「覆盖范围内有洞」）⇒ 这次 push 也拿不到"
                "「逐 job 全绿」的证据。→ 交付提交必须携带「上一提交的行 + 回执 + 它的证据」。"
            )
        else:
            out.append(
                f"{facts.sha[:7]} 没有携带它 first parent {facts.first_parent[:7]} 的台账行，"
                "而它又不是 tip ⇒ 它**永远**拿不到自己的 run（只为 tip 建）⇒ 它的行永远拿不到证据，"
                "且不可逆（只能改写历史）。→ 非交付提交不能单独入链，要并进下一个交付提交。"
            )
    return out


def _git(*args: str) -> str:
    """跑 git 并把输出按 **UTF-8** 解。

    为什么显式写 encoding：Windows 上 `text=True` 会用**本机编码**（GBK）解 —— 台账里全是中文，
    一行都解不出来（实测：`UnicodeDecodeError: 'gbk' codec can't decode byte 0xaf`，而且
    这个异常发生在读取线程里，`stdout` 会变成 `None`，看起来像「命令成功了但输出是空」）。
    **解码失败必须响亮**：宁可疑成「核不动」，也不能把乱码或空串当台账内容。
    """
    done = subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, check=False, encoding="utf-8"
    )
    if done.returncode != 0:
        raise GitFailed(f"git {' '.join(args)} → rc={done.returncode}：{done.stderr.strip()}")
    if done.stdout is None:
        raise GitFailed(f"git {' '.join(args)} 的输出读不出来（不是 UTF-8？）⇒ 核不动")
    return done.stdout


class GitFailed(RuntimeError):
    """git 命令失败：调用方只能报「核不动」，**不许**把它读成「没问题」。"""


def _first_parent(sha: str) -> str | None:
    done = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{sha}^"],
        cwd=REPO,
        capture_output=True,
        check=False,
        encoding="utf-8",
    )
    out = (done.stdout or "").strip()
    return out or None


def _rows_at(sha: str) -> set[str]:
    """该提交的树里台账**已有**的行 sha 集合。读不动就抛（空集会被读成「全是洞」，那是假红）。"""
    raw: Any = yaml.safe_load(_git("show", f"{sha}:{LEDGER}"))
    if not isinstance(raw, dict) or not isinstance(raw.get("rows"), list):
        raise GitFailed(f"{sha[:7]}:{LEDGER} 顶层不是带 `rows:` 的 mapping ⇒ 核不动")
    rows: set[str] = set()
    for row in raw["rows"]:
        if isinstance(row, dict) and isinstance(row.get("sha"), str) and SHA_RE.match(row["sha"]):
            rows.add(row["sha"])
    return rows


def _row_of(sha: str, row_sha: str) -> dict[str, Any] | None:
    raw: Any = yaml.safe_load(_git("show", f"{sha}:{LEDGER}"))
    for row in (raw or {}).get("rows", []):
        if isinstance(row, dict) and row.get("sha") == row_sha:
            return row
    return None


def evidence_problems(commits: list[Commit]) -> list[str]:
    """追加判据 3：tip 携带的父行声明了 `ci_evidence` 时，那份文件必须真在 tip 的树里。"""
    if not commits:
        return []
    tip = commits[-1]
    if tip.first_parent is None:
        return []
    row = _row_of(tip.sha, tip.first_parent)
    if row is None:
        return []  # 「没带这一行」已经由判据 2 报了，不重复
    path = row.get("ci_evidence")
    if not isinstance(path, str) or not path:
        return []
    done = subprocess.run(
        ["git", "cat-file", "-e", f"{tip.sha}:{path}"],
        cwd=REPO,
        capture_output=True,
        check=False,
        encoding="utf-8",
    )
    if done.returncode != 0:
        return [
            f"{tip.sha[:7]} 携带的 {tip.first_parent[:7]} 那一行写着 `ci_evidence: {path}`，"
            f"但那份文件不在 {tip.sha[:7]} 的树里 ⇒ 回执与证据不是同一个提交带进来的，CI 会红。"
        ]
    return []


def empty_range_verdict(base_sha: str, tip: str) -> str:
    """空 push 范围的两种含义（混成一种 = 一次静默放行）。

    实测（2026-10-19）：`--base <下游 sha> --tip <上游 sha>`（也就是非快进/回退/force push
    那种极性）时 `rev-list base..tip` 返回 0 个提交，而 main() 把这个 0 当成「形状对」
    并印出「本次 push 只推一个交付提交」——**0 个提交却说推了一个**，而且 force push
    会丢弃远端已有的提交（连它们的台账行与 CI 证据一起丢），恰恰是最该喊的那一种。

    - `noop`：起点就是 tip，本地与远端同一提交，真没东西可推（放行，但要说实话）；
    - `force`：起点不是 tip 却核不出提交 ⇒ 历史被改写/回退，几何无法评估（**核不动**）。
    """
    return "noop" if base_sha == tip else "force"


def _say_what_was_not_checked(commits: list[Commit]) -> None:
    """把「本条判据到底核了什么、没核什么」明说出来。

    为什么不能只报一句固定话术：判据 3 **只在**父行写了 `ci_evidence` 时才核证据，而父行是
    `git-only` 那类提交根本没有回执与证据 —— 那句「携带上一提交的行 + 回执 + 证据」就成了
    **超额声明**（实测：本仓带 `git-only` 父行时它一个字都没核）。工具的价值全在「说了什么
    就真核到了什么」，所以这里按实际核到的东西分岔说。
    """
    if not commits:
        return
    tip = commits[-1]
    if tip.first_parent is None:
        return
    row = _row_of(tip.sha, tip.first_parent)
    if row is None:
        return
    if row.get("attestation") == "git-only":
        print(
            "  没核的：上一提交那一行是 `git-only`（它没有回执文件、也没有 ci_evidence）——"
            "本条只核了「行在不在这个提交的树里、tree 对不对」，回执与证据本来就不存在。"
        )
    elif row.get("ci_evidence"):
        print("  核过的：父行声明的 `ci_evidence` 真在这棵树的路径上。")


def _upstream() -> str:
    return _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}").strip()


def _force_utf8_output() -> None:
    """把 stdout/stderr 钉成 UTF-8。

    Windows 的管道流默认按本机编码（cp936）编码，而且 stderr 默认是 `backslashreplace` ——
    中文化和「⇒」会变成乱码或 `\u21d2` 这种反斜杠转义（实测）。这条装置的全部价值就是
    「把原因说清楚」，输出读不了等于没有。Linux/CI 下本来就是 UTF-8，重配是一次无操作。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        encoding = (getattr(stream, "encoding", "") or "").replace("-", "").lower()
        if encoding == "utf8":
            continue  # 已经是 UTF-8（Linux/CI/pytest 的捕获流）⇒ 不做无意义的重配
        # 已包装/已关闭的流不接受重配：不因此失败
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    _force_utf8_output()
    parser = argparse.ArgumentParser(description="推之前检查这次 push 的提交形状")
    parser.add_argument("--base", default=None, help="push 范围起点（默认 @{u}，取不到就报错）")
    parser.add_argument("--tip", default="HEAD", help="push 范围终点（默认 HEAD）")
    args = parser.parse_args(argv)
    try:
        tip = _git("rev-parse", f"{args.tip}^{{commit}}").strip()
        base = args.base if args.base else _upstream()
        rewritten = ""
        try:
            base_sha = _git("rev-parse", f"{base}^{{commit}}").strip()
        except GitFailed as exc:
            # 起点不在树里：改写历史（旧 tip 不再被任何 ref 可达）或浅检出。
            # 不能因为核不到起点就把整条判据收摊——理由见模块 docstring「第四种结局」。
            rewritten = str(exc).splitlines()[0]
            fallback = _first_parent(tip)
            if fallback is None:
                raise GitFailed(f"{tip[:7]} 连第一父提交都定位不到（根提交）⇒ 核不动") from exc
            base_sha = fallback
        raw = _git("rev-list", "--first-parent", "--reverse", f"{base_sha}..{tip}")
        shas = raw.split()
        if not shas:
            # 空范围不是「形状对」：它要么是 no-op，要么是改写/回退（见 empty_range_verdict）。
            verdict = empty_range_verdict(base_sha, tip)
            if verdict == "noop":
                print(f"这次 push 没有任何提交：本地与远端都是 {tip[:7]}（no-op，无需校验）。")
                return 0
            print(
                f"核不动：push 范围 {base_sha[:7]}..{tip[:7]} 为空，但起点不是 tip —— "
                "起点比 tip 新，这是非快进（force / 回退）推送。\n"
                "  它会丢弃远端已有的提交，同时丢掉那些提交的台账行与 CI 证据，\n"
                "  而「这次到底推了几个提交」在改写后的树里无法评估。\n"
                "⇒ 这不是「形状对」：先把要保留的历史重新并进本地，再按一次一个交付提交推；\n"
                "  确实要改写时，也必须让被改写后的那个交付提交带上台账行与回执。",
                file=sys.stderr,
            )
            return 2
        commits = [Commit(sha=s, first_parent=_first_parent(s)) for s in shas]
        rows_at = {c.sha: _rows_at(c.sha) for c in commits}
        problems = check_push(commits=commits, rows_at=rows_at) + evidence_problems(commits)
    except (GitFailed, yaml.YAMLError) as exc:
        print(f"核不动：{exc}\n⇒ 这不是「没问题」。", file=sys.stderr)
        return 2
    print(f"范围：{base_sha[:7]}..{tip[:7]}（first-parent，旧→新）= {len(commits)} 个提交")
    if rewritten:
        print(
            f"  注意：push 的起点（{base}）不在树里——{rewritten}\n"
            f"  多半是改写历史（旧 tip 不再被任何 ref 可达）或浅检出。已退回核 "
            f"{base_sha[:7]}..{tip[:7]}：判据 2/3 照样生效；\n"
            f"  没核到的是：这次 push 到底推了几个提交——改写历史后树里没有可比的起点。"
        )
    if not problems:
        if rewritten:
            print("形状对（按可核的部分）：tip 携带了上一提交的台账行。")
        else:
            print("形状对：本次 push 只推一个交付提交，而且它携带了上一提交的台账行。")
        _say_what_was_not_checked(commits)
        return 0
    print(f"这次 push 会留下不可取证的提交，共 {len(problems)} 条：", file=sys.stderr)
    for item in problems:
        print(f"  - {item}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
