#!/usr/bin/env python3
"""把「CI 逐 job 全绿」这条主张做成**仓内可核**的产物（第八十二批 洞2）。

要治的缺口（第八十批真出过，不是假想）：
  * 台账里的 `run:` 是**自由文本** —— 守卫只核「是不是纯数字」
    （`grep -c '"run"' tests/test_delivery_receipts.py` ⇒ 0）。第八十批 Z 行写成 Y 的 run 号，
    没有任何东西能自动发现它；
  * 「逐 job success」的结论只活在 GitHub 页面上，以及本机 `waNN/watch-ci-*.py` 的 stdout 里
    ⇒ 换台机器、过一段时间就无法复核（第七十九批撞过「回执已被覆盖、不可复核」）。

做法：每个交付提交带一份 `evidence/ci/<sha12>.json`，内容是 `gh` 原始 JSON 的**机械摘录**
（没有一个字段是人工结论）。守卫 `tests/test_ci_evidence.py` 逐项核：
run 的 `head_sha` 必须等于被交付的提交（**这一条直接杀掉串号**）、`event=push`、
`conclusion=success`、核心 job 全 success、只有登记过的那两个 job 允许 skipped、
不出现未登记的新 job。产物由**下一个提交**与回执一起带进仓（同一个载体，见守卫的载体判据）。

用法：
  python scripts/ci_evidence.py fetch --sha <40hex> [--out <path>] [--force]
  python scripts/ci_evidence.py check [<path> ...]        # 离线，CI 里也能跑

退出码：0 = 核过；1 = 有问题（逐条打印）；2 = 用法/环境错（如 gh 没登录）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = "kwny-io/warden-agent"
BRANCHES = ("dev", "master")
#: push 上必须 success 的核心 job（第七十九/八十一批逐 job 核过的形状）。
CORE = ("container", "evals-real", "migration", "quality", "sbom", "security")
#: push 上本来就 skipped 的 job（**登记过才允许**；出现第三个 skipped 的 job 必须红）。
EXPECT_SKIP = ("变异测试", "image-signing")
SCHEMA = "warden-ci-evidence/1"
HEX40 = re.compile(r"^[0-9a-f]{40}$")
RUN_LIST_JSON = "databaseId,headBranch,headSha,status,conclusion,url,event,createdAt,workflowName"


def gh(*args: str) -> str:
    r = subprocess.run(["gh", *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SystemExit(
            f"gh {' '.join(args)} 失败（先 `gh auth status` 看一眼）：\n{r.stderr.strip()}")
    return r.stdout


def fetch(sha: str, force: bool, out: pathlib.Path) -> int:
    runs = json.loads(gh("run", "list", "--repo", REPO, "--commit", sha, "--limit", "20",
                         "--json", RUN_LIST_JSON))
    picked: dict[str, dict] = {}
    for b in BRANCHES:
        cand = [r for r in runs if r["headBranch"] == b and r["event"] == "push"]
        done = [r for r in cand if r["status"] == "completed"]
        if not done:
            print(f"✗ {b} 上这个提交还没有跑完的 push run（候选 {len(cand)} 个）："
                  f"{[(r['databaseId'], r['status']) for r in cand]}", file=sys.stderr)
            return 2
        if len(done) > 1:
            print(f"✗ {b} 上有 {len(done)} 个跑完的 push run ⇒ 语义不唯一，拒绝猜："
                  f"{[r['databaseId'] for r in done]}", file=sys.stderr)
            return 2
        picked[b] = done[0]

    doc: dict = {
        "schema": SCHEMA,
        "repo": REPO,
        "sha": sha,
        "fetched_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "gh run list/view --json 的机械摘录（勿手改；"
                  "重取用 scripts/ci_evidence.py fetch）",
        "branches": {},
    }
    for b, run in picked.items():
        view = json.loads(gh("run", "view", str(run["databaseId"]), "--repo", REPO,
                             "--json", "jobs,headSha,status,conclusion,event,url,createdAt,"
                                       "headBranch,workflowName"))
        jobs = sorted(({"name": j["name"], "conclusion": j.get("conclusion"),
                        "status": j.get("status")} for j in view["jobs"]),
                      key=lambda j: j["name"])
        doc["branches"][b] = {
            "run_id": run["databaseId"],
            "url": run["url"],
            "event": run["event"],
            "head_branch": view.get("headBranch"),
            "head_sha": view.get("headSha"),
            "status": view.get("status"),
            "conclusion": view.get("conclusion"),
            "created_at": view.get("createdAt"),
            "workflow": view.get("workflowName"),
            "jobs": jobs,
        }

    text = json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if out.exists() and out.read_text(encoding="utf-8") != text and not force:
        print(f"✗ {out} 已存在且内容不同 ⇒ 拒绝静默覆盖（核对后要换就加 --force）", file=sys.stderr)
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8", newline="\n")
    print(f"已写 {out}（{len(text)} 字节）")
    for b, d in sorted(doc["branches"].items()):
        print(f"  {b}: run {d['run_id']} {d['conclusion']} head_sha={str(d['head_sha'])[:12]}")
        for j in d["jobs"]:
            print(f"    {j['name']} :: {j['conclusion']}")
    return check([out])


def check(paths: list[pathlib.Path]) -> int:
    """离线核：判据与守卫 tests/test_ci_evidence.py 一致（**故意各写一遍**）。"""
    problems: list[str] = []
    for p in paths:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001 - 任何读不动都算问题，逐条报
            problems.append(f"{p}：读不动（{e}）")
            continue
        tag = p.name
        if d.get("schema") != SCHEMA:
            problems.append(f"{tag}：schema={d.get('schema')!r}，期望 {SCHEMA!r}")
        if d.get("repo") != REPO:
            problems.append(f"{tag}：repo={d.get('repo')!r}，期望 {REPO!r}")
        sha = d.get("sha")
        if not (isinstance(sha, str) and HEX40.match(sha)):
            problems.append(f"{tag}：sha={sha!r} 不是 40 位小写 hex")
            continue
        if p.stem != sha[:12]:
            problems.append(f"{tag}：文件名必须是 <sha12>.json（这里 sha[:12]={sha[:12]}）")
        branches = d.get("branches") or {}
        for b in BRANCHES:
            info = branches.get(b)
            if not isinstance(info, dict):
                problems.append(f"{tag}：缺 {b} 分支的 run 记录")
                continue
            if info.get("head_sha") != sha:
                problems.append(f"{tag}：{b} 的 run 跑的是 {str(info.get('head_sha'))[:12]}，"
                                f"而这份证据说被交付的是 {sha[:12]}（**串号**）")
            if info.get("event") != "push":
                problems.append(f"{tag}：{b} 的 run event={info.get('event')!r}，期望 push")
            if info.get("status") != "completed":
                problems.append(f"{tag}：{b} 的 run status={info.get('status')!r}，期望 completed")
            if info.get("conclusion") != "success":
                problems.append(
                    f"{tag}：{b} 的 conclusion={info.get('conclusion')!r}，期望 success")
            if info.get("head_branch") != b:
                problems.append(f"{tag}：{b} 的记录里 head_branch={info.get('head_branch')!r}")
            if not isinstance(info.get("run_id"), int) or info["run_id"] <= 0:
                problems.append(f"{tag}：{b} 的 run_id={info.get('run_id')!r} 不是正整数")
            jobs = info.get("jobs")
            if not isinstance(jobs, list) or not jobs:
                problems.append(f"{tag}：{b} 的 jobs 是空的 ⇒ 没核到任何 job")
                continue
            names = {j.get("name") for j in jobs}
            for name, concl in ((j.get("name"), j.get("conclusion")) for j in jobs):
                if any(str(name).startswith(pre) for pre in EXPECT_SKIP):
                    if concl != "skipped":
                        problems.append(f"{tag}：{b} 的 {name} conclusion={concl!r}，"
                                        f"push 上它本来就该是 skipped（登记见 EXPECT_SKIP）")
                elif concl != "success":
                    problems.append(f"{tag}：{b} 的 {name} conclusion={concl!r}，期望 success")
            missing = [c for c in CORE if not any(n == c for n in names)]
            if missing:
                problems.append(f"{tag}：{b} 的 jobs 里缺核心 job {missing} ⇒ "
                                f"workflow 改了或摘录漏了")
            unreg = [n for n in names
                     if n not in CORE and not any(str(n).startswith(pre) for pre in EXPECT_SKIP)]
            if unreg:
                problems.append(f"{tag}：{b} 出现未登记的新 job {unreg} ⇒ 先明确它该是什么结论，"
                                f"再把它登记进 CORE/EXPECT_SKIP")
    for x in problems:
        print(f"✗ {x}")
    tail = "全部核过 ✅" if not problems else f"{len(problems)} 处问题 ⛔"
    print(f"check：{len(paths)} 份证据，{tail}")
    return 1 if problems else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="从 gh 拉这两条分支的 run 摘录成一份仓内 JSON")
    f.add_argument("--sha", required=True)
    f.add_argument("--out")
    f.add_argument("--force", action="store_true")
    c = sub.add_parser("check", help="离线核已入库的证据")
    c.add_argument("paths", nargs="*")
    a = ap.parse_args()
    if a.cmd == "fetch":
        out = pathlib.Path(a.out) if a.out else pathlib.Path(f"evidence/ci/{a.sha[:12]}.json")
        return fetch(a.sha, a.force, out)
    paths = [pathlib.Path(x) for x in a.paths] or sorted(pathlib.Path("evidence/ci").glob("*.json"))
    if not paths:
        print("没有 evidence/ci/*.json 可核", file=sys.stderr)
        return 2
    return check(paths)


if __name__ == "__main__":
    raise SystemExit(main())
