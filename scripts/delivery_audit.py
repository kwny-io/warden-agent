#!/usr/bin/env python
"""交付面审计：把「默认分支的 tip 现在是什么颜色」变成**会自己喊**的判据。

为什么需要它（第八十七批）：

  * 2026-10-15 17:57 推上 `dev` 的 `5c0faa5` 起，dev 的 `quality` job 一直红
    （`tests/test_delivery_ledger.py::test_交付台账干净`：台账里没有 `HEAD^`（`7cf9b9f`）那一行）。
  * 那条判据的设计本来就是「**靠下一次交付逼人回填**」（见 `tests/DELIVERY_LEDGER.yaml` 头部），
    所以在下一次交付发生之前，交付面必然一直红着 —— **这是设计，不是缺陷**。
  * 缺陷是：**没有任何渠道把这件事告诉人。** 本仓既有的判据都对着「当下这棵树 / 这些文件」，
    而「已经推出去的提交，它的 run 现在什么颜色」只有打开 Actions 页才看得见。
    发现它靠有人恰好推东西（那天恰好是我），这不是机制 —— 21 小时里没有任何东西响过。
    这个脚本就是那个「响」。

只做三件窄事（都能离线注入数据来测）：

  1. 分支 tip 最近一次 completed run 的逐 job 结论里有没有红；**跳过**的 job 只有登记过的
     两个才算正常，其它 job 跳过一律算「这次绿灯没覆盖它」；
  2. 台账里声明了 `ci_evidence` 的行：证据文件必须在树里、必须 `conclusion=success`、
     且没有「不绿」或「没登记却跳过」的 job（离线判据与 `scripts/ci_evidence.py check` 一致 ——
     **故意各写一遍**；`ALLOWED_SKIPPED` 由 `tests/test_delivery_audit.py` 钉死与守卫同源）；
  3. 红了写一份人可读的 markdown 报告；带 `--alert-issue` 时用 `gh` 把报告贴进一条**幂等**的
     issue（标题固定：开着的就评论、没有就新建；**绿了且那条 issue 还开着就评论并关掉它**）。

不做的事（如实记）：**不判断「红的是产品还是交付账目」** —— 那要读失败用例正文。
本脚本只把「哪个 job 红 / 哪条断言」逐字抄进报告，分类留给读报告的人。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]

#: 与 `scripts/ci_evidence.py` 同源的三组常量（**故意各写一遍**，由 `tests/test_delivery_audit.py`
#: 钉死两者相等）：
#:   * `CORE`：每次 push 都该绿的 job；
#:   * `EXPECT_SKIP`：**在 push 上本来就该跳过**的 job（前缀匹配：矩阵 job 的名字带分片后缀）；
#:   * 其余 job：**没登记过的 job 出现就是红** —— 新 job 不许悄悄把绿名单变宽，
#:     而一个登记为「该跳过」的 job 哪天不再跳过了，也是红（登记过期 == 触发器腐烂）。
REPO = "kwny-io/warden-agent"
BRANCHES = ("dev", "master")
CORE = ("container", "evals-real", "migration", "quality", "sbom", "security")
EXPECT_SKIP = ("变异测试", "image-signing")

#: 幂等报警的落点：标题固定，才找得到「上一次喊的那条」。
ISSUE_TITLE = "交付面审计：默认分支的交付面是红的"


@dataclass(frozen=True)
class Finding:
    """一条「红」。三件都要有人读得懂：在哪、哪条判据、逐字事实。"""

    where: str
    criterion: str
    detail: str

    def line(self) -> str:
        return f"[红] {self.where} :: {self.criterion} :: {self.detail}"


def _job_findings(where: str, jobs: Sequence[dict[str, Any]], event_is_push: bool) -> list[Finding]:
    out: list[Finding] = []
    for job in jobs:
        name = str(job.get("name"))
        conclusion = job.get("conclusion")
        if any(name.startswith(pre) for pre in EXPECT_SKIP):
            if event_is_push and conclusion != "skipped":
                out.append(
                    Finding(
                        where,
                        "登记为「push 上该跳过」的 job 这次没跳过",
                        f"{name} conclusion={conclusion!r} ⇒ 登记过期了，或者它的门控被改了",
                    )
                )
            continue
        if name not in CORE:
            out.append(
                Finding(
                    where,
                    "出现没登记的 job",
                    f"{name} conclusion={conclusion!r}：新 job 得先登记进 CORE 或 EXPECT_SKIP",
                )
            )
            continue
        if conclusion == "skipped":
            out.append(Finding(where, "核心 job 被跳过", f"{name}：这次绿灯没覆盖它"))
        elif conclusion != "success":
            out.append(Finding(where, "核心 job 不绿", f"{name} conclusion={conclusion!r}"))
    return out


def audit_run(where: str, run: dict[str, Any], jobs: Sequence[dict[str, Any]]) -> list[Finding]:
    """一次 run（`gh run list/view --json` 的形状）→ 发现。纯函数，用例直接喂假数据。"""
    if str(run.get("status")) != "completed":
        return [Finding(where, "run 还没跑完", f"status={run.get('status')!r}")]
    out: list[Finding] = []
    if run.get("conclusion") != "success":
        out.append(Finding(where, "整个 run 不绿", f"conclusion={run.get('conclusion')!r}"))
    out.extend(_job_findings(where, jobs, str(run.get("event")) == "push"))
    return out


def _walk_dicts(obj: Any) -> Iterator[dict[str, Any]]:
    """整棵树地找字典。台账与证据的顶层键都改过（第七十九批以来），不假设形状。"""
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk_dicts(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk_dicts(value)


def audit_evidence_doc(where: str, doc: Any) -> list[Finding]:
    """证据文档（已解析的 JSON）的离线核。纯函数。"""
    if not isinstance(doc, dict):
        return [Finding(where, "证据顶层不是对象", type(doc).__name__)]
    branches = doc.get("branches")
    if not isinstance(branches, dict) or not branches:
        return [Finding(where, "证据里没有 branches", repr(branches)[:80])]
    out: list[Finding] = []
    for branch, blob in sorted(branches.items()):
        if not isinstance(blob, dict):
            out.append(Finding(where, "分支块不是对象", str(branch)))
            continue
        if blob.get("conclusion") != "success":
            out.append(
                Finding(where, f"{branch} 的 run 不绿", f"conclusion={blob.get('conclusion')!r}")
            )
        jobs = blob.get("jobs")
        if not isinstance(jobs, list) or not jobs:
            out.append(Finding(where, f"{branch} 的 jobs 是空的", "没核到任何 job"))
            continue
        out.extend(_job_findings(f"{where}（{branch}）", jobs, True))
    return out


def audit_evidence_file(path: Path) -> list[Finding]:
    """证据文件在树里的状态。文件不在 / 不是 JSON / 内容不合判据，都要回答。"""
    where = str(path.relative_to(ROOT)) if path.is_absolute() else str(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [Finding(where, "台账声明了证据文件、树里没有它", str(path))]
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        return [Finding(where, "证据文件不是合法 JSON", str(exc))]
    return audit_evidence_doc(where, doc)


def ledger_evidence_paths(ledger: Path) -> list[Path]:
    """台账里所有 `ci_evidence:` 的取值 → 树里的路径（去重、按路径排序）。"""
    doc = yaml.safe_load(ledger.read_text(encoding="utf-8")) or {}
    out: set[Path] = set()
    for node in _walk_dicts(doc):
        value = node.get("ci_evidence")
        if isinstance(value, str) and value.strip():
            out.add(ROOT / value.strip())
    return sorted(out)


def _gh(*args: str) -> Any:
    """`gh` 的薄壳：失败就抛，抛出的话里带逐字的 stderr（不然红在哪没人知道）。"""
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8",
                          check=False)
    if proc.returncode != 0:
        detail = proc.stderr.strip()[:300]
        raise RuntimeError(f"gh {' '.join(args[:3])}… rc={proc.returncode}：{detail}")
    text = proc.stdout.strip()
    if text.startswith(("{", "[")):
        return json.loads(text)
    return text


def select_tip_run(tip_sha: str, runs: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """在 `runs`（gh 的 newest-first）里挑「**tip 那次 push** 的 run」。

    **必须按事件挑**：周一 03:00 那一跑是 `schedule`，而 push 门控的 job（container / evals-real /
    migration / quality / sbom / security）在 schedule 上本来就该 skipped —— 拿它当「tip 的颜色」
    会把每个周一都喊成红，喊两次之后就没人看这条报警了。
    """
    for run in runs:
        if (str(run.get("event")) == "push" and str(run.get("headSha")) == tip_sha
                and str(run.get("status")) == "completed"):
            return run
    return None


def audit_branch(branch: str) -> tuple[str, list[Finding]]:
    """一个分支的**tip** 现在是什么颜色。

    先把「他是谁」问清楚（`gh api …/branches/<b>` 拿 tip sha），再去找**这一次 push** 的 run：
    只按「最近一次 completed run」去取会取到别的提交、别的事件上去。
    """
    raw_sha = _gh("api", f"repos/{REPO}/branches/{branch}", "--jq", ".commit.sha")
    tip_sha = str(raw_sha).strip().strip('"')
    runs = _gh("run", "list", "--branch", branch, "--limit", "20",
               "--json", "databaseId,headSha,status,conclusion,event,url,createdAt")
    where = f"{branch}@{tip_sha[:7]}"
    run = select_tip_run(tip_sha, runs)
    if run is None:
        recent = [(r.get("event"), str(r.get("headSha"))[:7], r.get("status")) for r in runs[:4]]
        return where, [Finding(where, "tip 没有 completed 的 push run",
                               f"可能在跑，也可能根本没被触发；最近的 run：{recent}")]
    label = f"{where}（run {run.get('databaseId')}，{str(run.get('createdAt'))[:16]}）"
    jobs = _gh("run", "view", str(run["databaseId"]), "--json", "jobs").get("jobs") or []
    return label, audit_run(label, run, jobs)


def _open_issue_number() -> int | None:
    listed = _gh("issue", "list", "--state", "open", "--search", ISSUE_TITLE,
                 "--json", "number,title")
    for item in listed:
        if item.get("title") == ISSUE_TITLE:  # 精确标题才算同一条，不信 search 的模糊命中
            return int(item["number"])
    return None


def alert_issue(report: str, red: bool) -> str:
    """红了就喊、绿了就说一声并关掉：**两个方向都要有回音**，只喊不关会变成噪音。"""
    number = _open_issue_number()
    if red:
        if number is None:
            _gh("issue", "create", "--title", ISSUE_TITLE, "--body", report)
            return "已新建 issue"
        _gh("issue", "comment", str(number), "--body", report)
        return f"已在 issue #{number} 追加评论"
    if number is None:
        return "没有开着的 issue，不动"
    _gh("issue", "comment", str(number), "--body", report)
    _gh("issue", "close", str(number), "--comment", "本轮审计全绿，关掉这条。")
    return f"已评论并关闭 issue #{number}"


def render_report(findings: Sequence[Finding], branch_lines: Sequence[str]) -> str:
    head = ["# 交付面审计", "", "## 默认分支", "", *[f"- {line}" for line in branch_lines], ""]
    if not findings:
        return "\n".join([*head, "## 红点", "", "没有。", ""])
    body = ["## 红点", ""]
    for f in findings:
        body.append(f"- **{f.where}** :: {f.criterion} :: {f.detail}")
    body.append("")
    body.append("（本报告由 `scripts/delivery_audit.py` 生成；它只抄事实，不判断"
                "「红的是产品还是交付账目」—— 那条要读失败用例正文。）")
    return "\n".join([*head, *body, ""])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="交付面审计：默认分支的 tip 与已交付提交的证据")
    parser.add_argument("--branch", action="append", default=None,
                        help="要审的分支，可重复（默认 dev 与 master）")
    parser.add_argument("--ledger", default="tests/DELIVERY_LEDGER.yaml")
    parser.add_argument("--report", default=None, help="把 markdown 报告写到这里")
    parser.add_argument("--alert-issue", action="store_true",
                        help="红了就建/更新那条幂等 issue（要 gh 与 issues: write）")
    parser.add_argument("--json", action="store_true", help="把发现按 JSON 打到 stdout")
    args = parser.parse_args(argv)

    branches = tuple(args.branch) if args.branch else BRANCHES
    findings: list[Finding] = []
    branch_lines: list[str] = []
    for branch in branches:
        try:
            where, found = audit_branch(branch)
        except RuntimeError as exc:
            findings.append(Finding(branch, "取 run 失败（这一项没被核到）", str(exc)))
            branch_lines.append(f"{branch}: 取 run 失败 —— {exc}")
            continue
        branch_lines.append(f"{branch}: {where} → " + ("绿" if not found else f"{len(found)} 项红"))
        findings.extend(found)

    ledger = ROOT / args.ledger
    if ledger.exists():
        paths = ledger_evidence_paths(ledger)
        branch_lines.append(f"台账声明了 {len(paths)} 份证据文件")
        for path in paths:
            found = audit_evidence_file(path)
            findings.extend(found)
            if found:
                branch_lines.append(f"{path.name}: {len(found)} 项红")
    else:
        findings.append(Finding(str(args.ledger), "台账不在树里", str(ledger)))

    red = bool(findings)
    for line in branch_lines:
        print(line)
    for f in findings:
        print(f.line())

    report = render_report(findings, branch_lines)
    if args.report:
        Path(args.report).write_text(report, encoding="utf-8", newline="\n")
        print(f"报告写到 {args.report}")
    if args.json:
        print(json.dumps([{"where": f.where, "criterion": f.criterion, "detail": f.detail}
                          for f in findings], ensure_ascii=False, indent=2))
    if args.alert_issue:
        try:
            print("报警：" + alert_issue(report, red))
        except RuntimeError as exc:
            print(f"报警失败（这一条本身就是红）：{exc}")
            return 1
    print("结论：" + ("交付面是红的" if red else "交付面全绿"))
    return 1 if red else 0


if __name__ == "__main__":
    sys.exit(main())
