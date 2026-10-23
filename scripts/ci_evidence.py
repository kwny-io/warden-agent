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


BILLING_HINT = "was not started because recent account payments have failed"
# 退出码 3：CI 根本没跑起来（job 0 个 step / 账单）。既不是绿，也不是代码红。
NOT_STARTED_RC = 3


def _not_started_reason(jobs: list[dict]) -> str | None:
    """job 一个 step 都没跑时，去问一下原因（账单？额度？）。

    为什么必须单独认这一种（2026-10-19 实锤）：`gh api .../check-runs/<job>/annotations` 的原文是
    「The job was not started because recent account payments have failed or your spending limit
    needs to be increased.」——job 的 `conclusion` 却写作 `failure`、且 `steps` 为空。
    如果取证只记 conclusion，这种「根本没跑」就会被当成「代码跑挂了」写进台账：
    既误拆了同事的活，又让「逐 job 全绿」的判据把一个从未执行过的 run 接纳成证据。
    """
    for job in jobs:
        if job.get("steps"):
            continue
        jid = job.get("databaseId")
        if not jid:
            continue
        try:
            annotations = json.loads(
                gh("api", f"repos/{REPO}/check-runs/{jid}/annotations"))
        except SystemExit:
            continue
        for a in annotations if isinstance(annotations, list) else []:
            msg = str(a.get("message") or "")
            if BILLING_HINT in msg or "spending limit" in msg:
                return msg
    return None


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
    all_runs: dict[str, list[dict]] = {}
    for b in BRANCHES:
        cand = [r for r in runs if r["headBranch"] == b and r["event"] == "push"]
        done = [r for r in cand if r["status"] == "completed"]
        if not done:
            print(f"✗ {b} 上这个提交还没有跑完的 push run（候选 {len(cand)} 个）："
                  f"{[(r['databaseId'], r['status']) for r in cand]}", file=sys.stderr)
            return 2
        # 多条跑完的 run 是**合法**情形，不是歧义：ref 的几何被补救过（例如 master 落后多个提交
        # 被一次性快进 ⇒ 「推前形状守卫」判红 ⇒ 退到上一跳再单步重推），同一个 sha 上就会既有
        # 那条红的、又有重推后那条绿的。此时**摘最新**的一条：它对应的才是 ref 现在这份历史。
        # 但同 sha 的每一条都**必须落进产物**（第八十九批订正：此前只取最新、其余只打到 stderr
        # ⇒ 「拿一条绿盖掉同 sha 那条红」在产物里根本看不出来，那就是洗证据的口子）。
        ordered = sorted(done, key=lambda r: (str(r.get("createdAt") or ""), r["databaseId"]))
        all_runs[b] = [{"run_id": r["databaseId"], "url": r.get("url"),
                        "event": r.get("event"), "status": r.get("status"),
                        "conclusion": r.get("conclusion"),
                        "created_at": r.get("createdAt")} for r in ordered]
        picked[b] = ordered[-1]
        if len(ordered) > 1:
            older = [(r["databaseId"], r.get("conclusion")) for r in ordered[:-1]]
            print(f"· {b} 上这个提交有 {len(ordered)} 条跑完的 push run ⇒ 摘最新 "
                  f"{picked[b]['databaseId']}（{picked[b].get('conclusion')}）；"
                  f"更早的同 sha 候选全部记进产物 runs：{older}", file=sys.stderr)

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
                        "status": j.get("status"),
                        # steps 的**条数**才是「到底跑没跑」的可机械判定的事实：
                        # conclusion=failure 但 steps=0 讲的是「没启动」，不是「跑挂了」。
                        "steps": len(j.get("steps") or []),
                        "databaseId": j.get("databaseId")} for j in view["jobs"]),
                      key=lambda j: j["name"])
        started = any(j["steps"] for j in jobs)
        not_started_reason = None if started else _not_started_reason(jobs)
        doc["branches"][b] = {
            "run_id": run["databaseId"],
            "picked_run_id": run["databaseId"],
            "runs": all_runs[b],
            "url": run["url"],
            "event": run["event"],
            "head_branch": view.get("headBranch"),
            "head_sha": view.get("headSha"),
            "status": view.get("status"),
            "conclusion": view.get("conclusion"),
            "created_at": view.get("createdAt"),
            "workflow": view.get("workflowName"),
            "jobs": jobs,
            "started": started,
            "not_started_reason": not_started_reason,
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
            print(f"    {j['name']} :: {j['conclusion']}（{j['steps']} 个 step）")
    dead = {b: d for b, d in doc["branches"].items() if not d.get("started")}
    if dead:
        print(
            "✗ 这次 CI 根本没跑起来（job 一个 step 都没执行）——它既不是绿，也不是代码红：",
            file=sys.stderr,
        )
        for b, d in sorted(dead.items()):
            print(f"  {b}: run {d['run_id']}；原因：{d.get('not_started_reason') or '未知'}",
                  file=sys.stderr)
        print("⇒ 这份产物不能证明代码健康，也不该被当成「代码跑挂了」写进台账。"
              "先修账单/额度，再重跑这个提交。", file=sys.stderr)
        return NOT_STARTED_RC
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
            # 「没跑起来」不能被当成证据：记在产物里、并在核的时候点名（否则账单红会冒充代码红）。
            if info.get("started") is False:
                problems.append(
                    f"{tag}：{b} 的 job 一个 step 都没跑（原因："
                    f"{info.get('not_started_reason') or '未知'}）⇒ 这份证据不成立")
            if info.get("head_branch") != b:
                problems.append(f"{tag}：{b} 的记录里 head_branch={info.get('head_branch')!r}")
            if not isinstance(info.get("run_id"), int) or info["run_id"] <= 0:
                problems.append(f"{tag}：{b} 的 run_id={info.get('run_id')!r} 不是正整数")
            listed = info.get("runs")
            if not isinstance(listed, list) or not listed:
                problems.append(f"{tag}：{b} 缺 runs（同 sha 的**全部** push run 必须一条不藏）")
            else:
                ids = [r.get("run_id") for r in listed if isinstance(r, dict)]
                if info.get("run_id") not in ids:
                    problems.append(f"{tag}：{b} 摘的 run_id={info.get('run_id')!r} 不在 runs 里")
                newest = max(listed, key=lambda r: (str(r.get("created_at") or ""),
                                                    r.get("run_id") or 0))
                if newest.get("run_id") != info.get("run_id"):
                    problems.append(
                        f"{tag}：{b} 摘的是 {info.get('run_id')}，而同 sha 还有更晚的 "
                        f"{newest.get('run_id')} ⇒ 不许挑旧的那条（挑旧的就是洗证据）")
                for r in listed:
                    if isinstance(r, dict) and r.get("run_id") != info.get("run_id") \
                            and r.get("conclusion") != "success":
                        print(f"· {tag}：{b} 同 sha 还有一条 {r.get('conclusion')} 的 run "
                              f"{r.get('run_id')} ⇒ 台账里必须点名它（不点名就是拿绿盖红）")
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
            # 逐 job 的「到底跑没跑」：conclusion=success 也可能是缓存命中，真正跑过的证据
            # 是 steps ≥ 1。写进产物的是条数（可机械判定），这里就核条数。
            dead = [(j.get("name"), j.get("steps")) for j in jobs
                    if isinstance(j, dict) and j.get("steps") is not None
                    and int(j["steps"]) == 0
                    and not any(str(j.get("name")).startswith(pre) for pre in EXPECT_SKIP)]
            if dead:
                problems.append(f"{tag}：{b} 的 {dead} 一个 step 都没执行 ⇒ 这份证据不成立"
                                f"（账单/额度？见分支级 not_started_reason）")
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
