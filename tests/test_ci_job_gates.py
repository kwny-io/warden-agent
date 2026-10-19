"""job 级门控台账（`tests/CI_JOB_GATES.yaml`）的守卫。

现场与理由写在台账头部注释里，一句话版本：**run 绿 ≠ 交付链绿**。
第六十八批拿 `gh run list` 的 `success` 当"交付面全绿"的证据，而那个 run 里
`image-signing`（签名 + 钉 digest）与 `mutation`（变异测试）都被 `if:` 挡成 skipped；
第六十九批手动 dispatch 让它们真跑，`image-signing` 当场红。

这个文件把"哪些 job 在什么事件下不跑"钉成可校验的东西：新增/修改门控必须回来签字。

四条判据：
  1. workflow 里任何带 `if:` 的 job 都必须有条目（没签字 = 红）；
  2. 台账里不许有失效条目（job 没了 / `if:` 没了）；
  3. `gate` 必须与 workflow 的 `if:` **逐字一致**（改了条件就撞红，逼人回来表态）；
  4. 每条必须写清 `why`、`not_run_on`、`run_it_now`（"怎么真跑一次"必须可复现，
     否则下一次想知道门控后面还活着没有，只能靠猜）；
  5. `run_it_now` 必须把 `workflow_dispatch` 的**必需入参**带齐（`-f tag=…`）。
     第 5 条是第六十九批收尾补的：第一版两条 `run_it_now` 都写成
     `gh workflow run ci.yml --ref dev`，而 `inputs.tag` 是 `required: true`
     ⇒ 手动真跑的命令**根本跑不动**（`HTTP 422: Required input 'tag' not provided`）。
     而第 4 条只断言它以 `gh workflow run` 开头 ⇒ **钉在文字上的判据不会红**，
     正是这个台账要治的病。
  6.（第七十批）**step 级跳过**（`step_gates:` 段）：workflow 里某个 step **内部**自己 `exit 0` 时，
     run 结论与「真跑了」一模一样（step 级 conclusion 同样是 `success`，平台事实，
     实测于 run 37570764622）⇒ 这一层只能靠四个钉子：登记（skip_when / how_to_really_run）、
     签字开关（allow_flag）、回执 artifact（receipt_artifact）、notice 字样；
     外加一条自动撞红：
     `ci.yml` 里再出现新的静默跳却未登记 ⇒ 红。
  7.（第七十一批）**触发器级**（`triggers:` 段）：`on.schedule` 写在哪**不等于**它会响——
     GitHub 只从**默认分支**上的 workflow 文件求值 `schedule`（平台文档原文：
     *This event will only trigger a workflow run if the workflow file exists on the default
     branch*、*Scheduled workflows will only run on the default branch*）。
     现场：cron `0 3 * * 1` 加在 `dev`
     （`2760bd6`，2026-09-22），而默认分支是 `master`（HEAD 停在 2026-09-07）⇒ 全仓 69 次 run 里
     `event=schedule` **0 次**，而 `docs/operations.md` 与 `runs_on` 都把它当“会跑”在引用。
     这类缺席**没有任何本地痕迹**（没 run、没日志、没红色），所以只能靠台账：
     cron / `evaluated_from` / `last_observed_fire` / `how_to_verify` / `review_by`（过期即红）。
  8.（第七十五批）**“它会唤醒谁”也是必须被验证的断言**（`fires_jobs:`）——`why` 是散文，散文会漂移。
    现场：cron 那条的 `why` 写着它是 `mutation` 与 `image-signing` “那两条门”的唯一正常触发，
    而 `image-signing` 的 `if:` 只接受 `push v* tag` 与 `workflow_dispatch`（不接受 `schedule`）
    ⇒ 那句承诺**永远是假的**，而且默认分支被修好之后，它还会让人以为“签名链路每周自己验一次了”。
    ⇒ ① 每条触发器写 `fires_jobs:`，守卫对着 workflow 的 `if:` 断言它真能唤醒这些 job
    （表达式形状不认识就直接报错，不静默放过）；② 反向：任何 `if:` 接受 `schedule` 的 job，
    必须出现在某条 `kind: schedule` 条目的 `fires_jobs:` 里——两边都不留缝。
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Any

import pytest
import yaml


def _utc_today() -> datetime.date:
    """判据里的「今天」一律取 **UTC** 日期，不取本机日期。

    为什么：CI（GitHub 的 runner）与 workflow 的 cron 都按 UTC 算，而这台开发机在 UTC+8
    ⇒ 取本机日期会让**同一个提交在本地绿、在 CI 红**。2026-10-17 实测：本地已是 10-17、
    UTC 还是 10-16，台账里写 10-17 的那一行在 CI 上被判成「在未来」而红（第八十九批现场：
    修 master 推送那三次红里，有两次就是这一条）。

    口径：台账 `last_checked` / `first_possible_fire` 也按这个 UTC 口径写。
    """
    return datetime.datetime.now(datetime.UTC).date()

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"
LEDGER = REPO / "tests" / "CI_JOB_GATES.yaml"

_EVENTS = frozenset({"push", "pull_request", "schedule", "workflow_dispatch", "release"})


def _gated_jobs() -> dict[tuple[str, str], str]:
    """{(workflow 文件名, job 名): if 表达式}：所有带 `if:` 的 job。"""
    gated: dict[tuple[str, str], str] = {}
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for name, job in (data.get("jobs") or {}).items():
            cond = job.get("if")
            if isinstance(cond, str) and cond.strip():
                gated[(path.name, name)] = cond
    return gated


def _ledger() -> dict[tuple[str, str], dict[str, Any]]:
    raw: Any = yaml.safe_load(LEDGER.read_text(encoding="utf-8")) or {}
    entries = raw.get("entries") or []
    assert entries, "台账是空的——空台账会让下面每条断言都变成永真"
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in entries:
        key = (entry["workflow"], entry["job"])
        assert key not in out, f"台账里 {key} 重复了"
        out[key] = entry
    return out


def _required_dispatch_inputs(workflow: str) -> set[str]:
    """workflow 的 `workflow_dispatch.inputs` 里声明为 `required` 的入参名。

    注意 YAML 1.1 会把裸 `on:` 解析成布尔键 `True`，所以两个键都要试。
    """
    path = WORKFLOWS / workflow
    data: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    triggers = data.get("on") or data.get(True) or {}
    if not isinstance(triggers, dict):
        return set()
    dispatch = triggers.get("workflow_dispatch")
    if not isinstance(dispatch, dict):
        return set()
    inputs = dispatch.get("inputs")
    if not isinstance(inputs, dict):
        return set()
    return {
        str(name)
        for name, spec in inputs.items()
        if isinstance(spec, dict) and spec.get("required")
    }


def test_台账非空且每条都能解析() -> None:
    assert len(_ledger()) >= 1


def test_每个被门控的_job_都在台账里() -> None:
    """新增一个带 `if:` 的 job 而没签字 ⇒ 红。这正是"缺席要看得见"的机制。"""
    missing = sorted(set(_gated_jobs()) - set(_ledger()))
    assert not missing, (
        "这些 job 被 `if:` 门控，但没在 tests/CI_JOB_GATES.yaml 里签字：\n"
        + "\n".join(f"  {wf}:{job}" for wf, job in missing)
        + "\n请补一条：写清何时不跑（not_run_on）、为什么（why）、怎么真跑一次（run_it_now）"
    )


def test_台账里没有失效条目() -> None:
    """job 被删了 / `if:` 被去掉了，台账里的条目必须跟着走（否则台账会越长越假）。"""
    stale = sorted(set(_ledger()) - set(_gated_jobs()))
    assert not stale, (
        "台账里有条目已经不对应任何带 `if:` 的 job（job 改名/删除，或门控被去掉）：\n"
        + "\n".join(f"  {wf}:{job}" for wf, job in stale)
    )


def test_台账里的_gate_与_workflow_逐字一致() -> None:
    """沉默的条件漂移是最坏的一种：台账说"只在 tag 上跑"，workflow 早改成每次 push 都跑。"""
    gated = _gated_jobs()
    drift = {
        key: (ledger["gate"], gated[key])
        for key, ledger in _ledger().items()
        if ledger.get("gate") != gated.get(key)
    }
    assert not drift, "\n".join(
        f"{wf}:{job} 台账写的是 {want!r}，workflow 里是 {got!r}"
        for (wf, job), (want, got) in drift.items()
    )


def test_每条都写清了为什么_何时不跑_怎么真跑() -> None:
    for (wf, job), entry in sorted(_ledger().items()):
        where = f"{wf}:{job}"
        why = entry.get("why") or ""
        assert len(why.strip()) >= 40, f"{where} 的 why 太短：要写清代价（为什么可以少跑）"
        not_run_on = entry.get("not_run_on") or []
        assert not_run_on, (
            f"{where} 没写 not_run_on——「缺席在哪个事件上」是这条台账的全部意义"
        )
        for item in not_run_on:
            event = str(item).split()[0]
            assert event in _EVENTS, f"{where} 的 not_run_on 里 {item!r} 不是已知事件名"
        run_it_now = str(entry.get("run_it_now") or "")
        assert run_it_now.startswith("gh workflow run"), (
            f"{where} 的 run_it_now 必须是可直接复制的 `gh workflow run …`（现在是 {run_it_now!r}）"
        )
        assert entry.get("added"), f"{where} 没写 added（空洞要留日期，才好判断是不是历史遗留）"


def test_真跑命令把必需入参带齐() -> None:
    """`run_it_now` 是"怎么真跑一次"的唯一说明：少一个必需入参，抄下来就是 HTTP 422。

    现场：台账两条 `run_it_now` 都写成 `gh workflow run ci.yml --ref dev`，
    而 `ci.yml` 的 `workflow_dispatch.inputs.tag` 是 `required: true`
    （有意如此：不让分支名冒充版本号）⇒ 这两条命令**手动真跑时根本跑不动**。
    判据钉在**机制**上：从 workflow 里读出 `required` 入参，
    再看 `run_it_now` 有没有带 `-f <name>=`。
    """
    bad: list[str] = []
    for (wf, job), entry in sorted(_ledger().items()):
        required = _required_dispatch_inputs(str(wf))
        cmd = str(entry.get("run_it_now") or "")
        missing = sorted(name for name in required if f"-f {name}=" not in cmd)
        if missing:
            bad.append(
                f"{wf}:{job} 的 run_it_now 缺必需入参 {missing}（缺了就是 HTTP 422，"
                f"等于这条「怎么真跑」抄不动）：{cmd!r}"
            )
    assert not bad, "\n".join(bad)


# ─────────────────────────────────────────────────────────────────────────────
# step 级跳过（第七十批）：**run 结论看不见它**，所以只能靠「登记 + 签字 + 回执」。
# 平台事实（实测）：step 内部自己 `exit 0` 的跳过，step 级 conclusion 仍是 `success`；
# 只有整个 job 被 `if:` 跳过才是 `skipped`。⇒ 这一层不能靠 API，必须靠台账与 artifact。


def _step_scripts(workflow: str) -> list[tuple[str, str, str]]:
    """[(job, step 名, 脚本文本)]：workflow 里所有带 `run:` 的步骤。"""
    data: Any = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8")) or {}
    out: list[tuple[str, str, str]] = []
    for job_name, job in (data.get("jobs") or {}).items():
        for step in (job or {}).get("steps") or []:
            script = step.get("run")
            if isinstance(script, str) and script.strip():
                out.append((str(job_name), str(step.get("name") or ""), script))
    return out


def _steps_of(workflow: str) -> list[tuple[str, dict[str, Any]]]:
    """[(job, step dict)]：用于查 `uses:` / `with:` / `env:`。"""
    data: Any = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8")) or {}
    out: list[tuple[str, dict[str, Any]]] = []
    for job_name, job in (data.get("jobs") or {}).items():
        for step in (job or {}).get("steps") or []:
            if isinstance(step, dict):
                out.append((str(job_name), step))
    return out


def _step_env(workflow: str, job: str, step: str) -> dict[str, Any]:
    """某个具名步骤的 `env:`（没写就是空 dict）。"""
    for j, s in _steps_of(workflow):
        if j == job and str(s.get("name") or "") == step:
            return dict(s.get("env") or {})
    return {}


def _step_ledger() -> list[dict[str, Any]]:
    raw: Any = yaml.safe_load(LEDGER.read_text(encoding="utf-8")) or {}
    entries = raw.get("step_gates") or []
    assert entries, "`step_gates:` 是空的——空台账会让下面每条断言都变成永真"
    return entries


def test_step_级台账每条都写全了() -> None:
    for entry in _step_ledger():
        where = f"{entry.get('workflow')}:{entry.get('job')}:{entry.get('step')}"
        for key in (
            "workflow",
            "job",
            "step",
            "skip_when",
            "allow_flag",
            "receipt_artifact",
            "notice",
            "how_to_really_run",
            "added",
            "evidence",
        ):
            assert entry.get(key), f"{where} 缺 {key}"
        assert len(str(entry["skip_when"]).strip()) >= 20, f"{where} 的 skip_when 太短"
        assert len(str(entry["evidence"]).strip()) >= 20, f"{where} 的 evidence 太短"


def test_step_级条目指向的_job_与_step_真实存在() -> None:
    bad: list[str] = []
    for entry in _step_ledger():
        wf, job, step = str(entry["workflow"]), str(entry["job"]), str(entry["step"])
        steps = _step_scripts(wf)
        if not any(j == job and s == step for j, s, _ in steps):
            names = sorted({s for j, s, _ in steps if j == job})
            bad.append(f"{wf}:{job} 里没有名为 {step!r} 的步骤（该 job 的 run 步骤：{names}）")
    assert not bad, "\n".join(bad)


def test_step_级的签字开关在_workflow_里真被用到() -> None:
    """`allow_flag` 不能只是个名字：必须在该 step 的 `env:` 里显式给出，
    且脚本里真的拿它做判断。"""
    bad: list[str] = []
    for entry in _step_ledger():
        wf = str(entry["workflow"])
        job = str(entry["job"])
        step = str(entry["step"])
        flag = str(entry["allow_flag"])
        script = {s: sc for j, s, sc in _step_scripts(wf) if j == job}.get(step, "")
        env = _step_env(wf, job, step)
        if flag not in env:
            bad.append(
                f"{wf}:{job}:{step} 的 env 里没有 `{flag}`"
                "（签字开关必须写在 workflow 里：可 diff、可 review、守卫断言得到）"
            )
        if f"${flag}" not in script:
            bad.append(f"{wf}:{job}:{step} 的脚本里没有 `${flag}` 判断——签字开关没被真正使用")
    assert not bad, "\n".join(bad)


def test_step_级跳过必须产出回执_artifact_且上传是有条件的() -> None:
    """回执是“有它 ⇔ 那一步没跑”的双向证据：既要有上传步骤，且**必须带 `if:`**
    （无条件上传会在“真跑了”的场合也试图上传不存在的文件，那个⇔就断了）。"""
    bad: list[str] = []
    for entry in _step_ledger():
        wf = str(entry["workflow"])
        name = str(entry["receipt_artifact"])
        uploads = [
            step
            for _, step in _steps_of(wf)
            if str(step.get("uses", "")).startswith("actions/upload-artifact")
            and str((step.get("with") or {}).get("name") or "") == name
        ]
        if not uploads:
            names = sorted(
                str((s.get("with") or {}).get("name") or "")
                for _, s in _steps_of(wf)
                if str(s.get("uses", "")).startswith("actions/upload-artifact")
            )
            bad.append(
                f"{wf} 声明跳过时产出 artifact `{name}`，但 workflow 里没有上传它的步骤"
                f"（已见 upload-artifact 的 name：{names}）"
            )
            continue
        for step in uploads:
            if not str(step.get("if") or "").strip():
                bad.append(
                    f"{wf} 的 artifact `{name}` 上传步骤没有 `if:`——"
                    "无条件上传会在“真跑了”的场合也去上传不存在的文件，回执就不再是双向证据"
                )
    assert not bad, "\n".join(bad)


def test_step_级跳过的_notice_字样还在_workflow_里() -> None:
    """notice 是“人看的那一层”（日志里的 `##[notice]`）。它被改掉 ⇒ 台账要跟着走。"""
    bad: list[str] = []
    for entry in _step_ledger():
        wf = str(entry["workflow"])
        needle = str(entry["notice"])
        if needle not in (WORKFLOWS / wf).read_text(encoding="utf-8"):
            bad.append(f"{wf} 里找不到 notice 字样 {needle!r}")
    assert not bad, "\n".join(bad)


def test_workflow_里的静默跳过都被登记了() -> None:
    """新增一处“step 内部自己决定不跑”（形如 `::notice::` + `exit 0`）而未登记 ⇒ 红。

    这是 job 级规则 1 在 step 层的对应物：光靠“记得来登记”是治不住新洞的。
    """
    registered = {
        (str(e["workflow"]), str(e["job"]), str(e["step"])) for e in _step_ledger()
    }
    unregistered: list[str] = []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        for job, step, script in _step_scripts(path.name):
            silent = ("::notice::" in script or "::warning::" in script) and "exit 0" in script
            if silent and (path.name, job, step) not in registered:
                unregistered.append(f"{path.name}:{job}:{step}")
    assert not unregistered, (
        "这些 step 里有「看起来成功、其实跳过了」的分支，但没在 step_gates 里登记：\n"
        + "\n".join(f"  {x}" for x in sorted(unregistered))
        + "\n（step 级结论在 run 里**看不出来**，所以必须登记："
        "skip_when / allow_flag / receipt_artifact / notice）"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 触发器级（第七十一批）：**“我承诺它会自己响”本身就是一个必须被验证的断言。**
#
# 与上面两层的关系：job 级管“哪些 job 在什么事件下不跑”，step 级管“哪一步自己决定不跑”，
# 这一层管“**那个事件本身到底会不会发生**”。只有三层都在，才能回答“这次绿灯覆盖了什么”。

_TRIGGER_KEYS = (
    "workflow",
    "kind",
    "cron",
    "evaluated_from",
    "fires_jobs",
    "why",
    "expected_cadence_days",
    "last_observed_fire",
    "last_checked",
    "first_possible_fire",
    "how_to_verify",
    "review_by",
    "added",
    "evidence",
)


def _workflow_triggers(workflow: str) -> dict[str, Any]:
    data: Any = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8")) or {}
    triggers = data.get("on") or data.get(True) or {}
    return triggers if isinstance(triggers, dict) else {}


def _declared_crons(workflow: str) -> list[str]:
    """workflow 的 `on.schedule` 里声明的 cron（没写就是空表）。"""
    sched = _workflow_triggers(workflow).get("schedule")
    if sched is None:
        return []
    if isinstance(sched, dict):
        sched = [sched]
    assert isinstance(sched, list), (
        f"{workflow} 的 on.schedule 形状不认识（{type(sched).__name__}）——"
        "不认识的形状会让下面每条断言静默放过"
    )
    out: list[str] = []
    for item in sched:
        assert isinstance(item, dict) and "cron" in item, (
            f"{workflow} 的 schedule 条目缺 cron：{item!r}"
        )
        out.append(str(item["cron"]))
    return out


def _trigger_ledger() -> list[dict[str, Any]]:
    raw: Any = yaml.safe_load(LEDGER.read_text(encoding="utf-8")) or {}
    entries = raw.get("triggers") or []
    assert entries, "`triggers:` 是空的——空台账会让下面每条断言都变成永真"
    return entries


def test_触发器台账每条都写全了() -> None:
    for entry in _trigger_ledger():
        where = f"{entry.get('workflow')}:{entry.get('kind')}:{entry.get('cron')}"
        for key in _TRIGGER_KEYS:
            assert entry.get(key) not in (None, ""), f"{where} 缺 {key}"
        for key, least in (("why", 40), ("evidence", 40), ("how_to_verify", 20)):
            assert len(str(entry[key]).strip()) >= least, f"{where} 的 {key} 太短"


def test_每个声明的_schedule_都在触发器台账里() -> None:
    """workflow 里新加一条 cron 而没登记 ⇒ 红（job 级规则 1 在触发器层的对应物）。"""
    registered = {
        (str(e["workflow"]), str(e["cron"]))
        for e in _trigger_ledger()
        if e.get("kind") == "schedule"
    }
    missing: list[str] = []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        for cron in _declared_crons(path.name):
            if (path.name, cron) not in registered:
                missing.append(f"{path.name}: {cron}")
    assert not missing, (
        "这些 schedule 触发器没在 tests/CI_JOB_GATES.yaml 的 `triggers:` 里登记：\n"
        + "\n".join(f"  {x}" for x in missing)
        + "\n每条要写：evaluated_from / last_observed_fire / how_to_verify / review_by / evidence"
        "（写在 on: 里 ≠ 会发生：schedule 只从默认分支求值）"
    )


def test_触发器台账里的_cron_与_workflow_逐字一致() -> None:
    """双向：登记的 cron 必须还在 workflow 里（否则台账在说一件不存在的事）。"""
    stale: list[str] = []
    for entry in _trigger_ledger():
        if entry.get("kind") != "schedule":
            continue
        wf, cron = str(entry["workflow"]), str(entry["cron"])
        crons = _declared_crons(wf)
        if cron not in crons:
            stale.append(f"{wf} 里没有 cron {cron!r}（现在声明的是：{crons}）")
    assert not stale, "\n".join(stale)


def test_触发器台账的复核期限没过期() -> None:
    """空洞必须有期限：到日子没回来复核就红——逼人回答“它到底响了没有”。

    这是**故意**与日历耦合的判据：一个从未响过的触发器没有别的强制力，
    只有期限能让它不能“安静地一直是假的”。
    """
    today = _utc_today()
    bad: list[str] = []
    for entry in _trigger_ledger():
        where = f"{entry.get('workflow')}:{entry.get('kind')}:{entry.get('cron')}"
        try:
            added = datetime.date.fromisoformat(str(entry["added"]))
            review = datetime.date.fromisoformat(str(entry["review_by"]))
        except ValueError as exc:
            bad.append(f"{where} 的日期不是 ISO 形式（YYYY-MM-DD）：{exc}")
            continue
        if review < added:
            bad.append(f"{where} 的 review_by({review}) 早于 added({added})")
        elif review < today:
            bad.append(
                f"{where} 的复核期限已过（review_by={review}，今天={today}）："
                "回去复核这个触发器到底响了没有，再更新 last_observed_fire 与 review_by"
            )
    assert not bad, "\n".join(bad)


def test_从未响过的触发器必须写明原因与解锁代价() -> None:
    """`last_observed_fire: never` 不能只是一句“它就是不响”：要写清机制与出路（含代价）。"""
    bad: list[str] = []
    for entry in _trigger_ledger():
        if str(entry.get("last_observed_fire")) != "never":
            continue
        where = f"{entry.get('workflow')}:{entry.get('kind')}:{entry.get('cron')}"
        for key, least in (("root_cause", 40), ("unblock", 40)):
            text = str(entry.get(key) or "").strip()
            if len(text) < least:
                bad.append(
                    f"{where} 的 {key} 缺失或太短（{text!r}）："
                    "never 的触发器必须能自证“为什么不会响”与“怎样才能响”"
                )
    assert not bad, "\n".join(bad)


def _trigger_rot(entries: list[dict[str, Any]], today: datetime.date) -> list[str]:
    """触发器台账里「声明与世界对不上」的问题（纯函数：便于喂合成反例，也便于变异）。

    为什么需要这一层：本文件既有的几条只查**内部自洽**（写全了 / cron 与 workflow 逐字一致 /
    `review_by` 没过期 / `never` 必须带 root_cause 与 unblock），它们查不出
    「台账说它永远不会响，而世界已经变了」。
    2026-10-15 就是这个情形：默认分支上的 `ci.yml` 从 561 字节的 9 月存根变成了 42880 字节的全量
    （含 cron 与 `mutation` job），而条目里 `root_cause` 还写着「永远不会被求值」、`unblock` 还在
    教人去改默认分支——**这类断言没有任何本地痕迹能推翻它**（没 run、没日志、没红色），
    所以给它一个日子：`first_possible_fire`。日子过了还写 `never`，守卫就红。
    """
    problems: list[str] = []
    for entry in entries:
        if entry.get("kind") != "schedule":
            continue
        where = f"{entry.get('workflow')}:{entry.get('kind')}:{entry.get('cron')}"
        try:
            checked = datetime.date.fromisoformat(str(entry["last_checked"]))
            first = datetime.date.fromisoformat(str(entry["first_possible_fire"]))
        except (KeyError, ValueError) as exc:
            problems.append(f"{where} 的 last_checked/first_possible_fire 不是 ISO 日期：{exc!r}")
            continue
        if checked > today:
            problems.append(
                f"{where} 的 last_checked({checked}) 在未来（今天 {today}）—— 那是没发生过的复核"
            )
        # `first < today`（而不是 `<=`）：cron 带**时刻**，第一次交火的那天在当天早上还没到点。
        # 2026-10-19 就是现场：`0 3 * * 1` 的首个交点就是当天 03:00Z，
        # 而 UTC 今天已是 10-19（01:03Z），
        # `<=` 会在事件**还没到点**时把守卫变红，让人跑去 `gh run list` 再发现“什么都没有”——
        # 跟写满 “never” 一样是假信号，只是方向反过来（假红）。越过一天后仍 never 才是真对不上。
        if str(entry.get("last_observed_fire")) == "never" and first < today:
            problems.append(
                f"{where} 写着 first_possible_fire={first} 且 last_observed_fire=never，"
                f"而今天已是 {today}：第一次可能响的日子过去了，回去看它到底响没响"
                "（gh run list --event schedule --limit 5 "
                "--json databaseId,conclusion,createdAt），"
                "再更新 last_observed_fire 与 review_by，或重写 root_cause 说明为什么还没轮到"
            )
    return problems


def test_触发器台账的_never_不能越过第一次可能响的日子() -> None:
    """`never` 必须是有期限的断言，不能无限期地说「还没轮到」（现场见 `_trigger_rot` 说明）。"""
    problems = _trigger_rot(_trigger_ledger(), _utc_today())
    assert not problems, "\n".join(problems)


#: 允许取本机日期的位置（空 = 一处都不许）。与 `tests/SKIP_LEDGER.yaml` 同一个哲学：
#: 要留一个洞，必须在这里写清「为什么本机日期在这一处是对的」，而不是悄悄多出一处。
LOCAL_DATE_ALLOWED: dict[str, str] = {}


def test_判据与脚本里的今天必须按UTC取() -> None:
    """判据/脚本里的「今天」一律 UTC：取本机日期会让**同一提交在本地绿、在 CI 红**。

    为什么扫全仓而不只扫本文件：这个坑在 2026-10-17 一天内出现两处 ——
    触发器腐烂判据（本文件）与镜像过期天数（`scripts/check_pinned_images.py`），
    而两处在本机都「看起来完全正常」：只有 CI（UTC）与开发机（UTC+8）对不上时才暴露
    （实测：本地 10-17 / UTC 10-16，台账写 10-17 的那一行在 CI 上被判成「在未来」而红）。

    怎么证明有牙：把任一处换回取本机日期，这条立刻红；`LOCAL_DATE_ALLOWED` 里多一个文件也红。
    """
    needle = "date." + "today()"  # 拼出来，免得这条判据命中自己的源码
    root = Path(__file__).resolve().parents[1]
    hits: list[str] = []
    for base in ("tests", "scripts"):
        for p in sorted((root / base).rglob("*.py")):
            rel = p.relative_to(root).as_posix()
            if rel in LOCAL_DATE_ALLOWED:
                continue
            if needle in p.read_text(encoding="utf-8"):
                hits.append(rel)
    assert not hits, (
        f"这些文件取本机日期：{hits} ⇒ 判据的「今天」跟着开发机时区走，"
        "而 CI 与 workflow 的 cron 按 UTC：同一个提交会有两种真值。"
        "（要用 UTC 就写 `datetime.now(timezone.utc).date()`；真要取本机日期，"
        "就在 `LOCAL_DATE_ALLOWED` 里写清为什么。）"
    )


def test_牙_触发器腐烂判据四条都得有牙() -> None:
    """正/反向对照：合成条目必须分别红/绿，否则上面那条就是永真判据。"""
    today = datetime.date(2026, 10, 15)
    ok: dict[str, Any] = {
        "workflow": "ci.yml",
        "kind": "schedule",
        "cron": "0 3 * * 1",
        "last_observed_fire": "never",
        "last_checked": "2026-10-15",
        "first_possible_fire": "2026-10-19",
    }
    assert _trigger_rot([ok], today) == [], "正对照：还没到第一次机会就不该红"
    fired = dict(ok, last_observed_fire="2026-10-12")
    assert _trigger_rot([fired], today) == [], "反向对照：响过了就不该红"
    past = dict(ok, first_possible_fire="2026-10-12")
    assert _trigger_rot([past], today), "机会过去了还写 never ⇒ 必须红"
    future_check = dict(ok, last_checked="2026-11-01")
    assert _trigger_rot([future_check], today), "复核日期在将来 ⇒ 必须红"
    not_iso = dict(ok, last_checked="昨天")
    assert _trigger_rot([not_iso], today), "日期不是 ISO ⇒ 必须红"


def test_schedule_承诺与观察到的现实必须一致() -> None:
    """两个方向都要拦：

    * `runs_on` 里承诺了 `schedule` ⇒ 必须有真的响过（`last_observed_fire != never`）的
      schedule 触发器；
    * `if:` 里写了 `schedule` 却把它从 `runs_on` 划掉 ⇒ 必须有一条 `never` 的 schedule
      触发器做依据，并写 `runs_on_note` 说明何时加回来。

    现场（第七十一批）：`mutation` 的 `if:` 含 `schedule`，但 cron 从未被求值
    ⇒ `runs_on` 只能是 `[workflow_dispatch]` + `runs_on_note`。
    """
    fired = {
        str(e["workflow"])
        for e in _trigger_ledger()
        if e.get("kind") == "schedule" and str(e.get("last_observed_fire")) != "never"
    }
    dead = {
        str(e["workflow"])
        for e in _trigger_ledger()
        if e.get("kind") == "schedule" and str(e.get("last_observed_fire")) == "never"
    }
    gated = _gated_jobs()
    bad: list[str] = []
    for (wf, job), entry in sorted(_ledger().items()):
        where = f"{wf}:{job}"
        promised = "schedule" in [str(x).split()[0] for x in (entry.get("runs_on") or [])]
        mentioned = "schedule" in str(gated.get((wf, job)) or "")
        if promised and wf not in fired:
            bad.append(
                f"{where} 的 runs_on 承诺了 schedule，但 {wf} 没有一条真的响过的 schedule 触发器"
            )
        if mentioned and not promised:
            if wf not in dead:
                bad.append(
                    f"{where} 的 if: 提到 schedule 却没算进 runs_on，"
                    f"而 {wf} 没有一条 `last_observed_fire: never` 的 schedule 触发器做依据"
                )
            elif not str(entry.get("runs_on_note") or "").strip():
                bad.append(
                    f"{where} 把 schedule 从 runs_on 里划掉了，必须写 runs_on_note："
                    "依据是什么、等触发器修好后何时加回来"
                )
    assert not bad, "\n".join(bad)


# ─────────────────────────────────────────────────────────────────────────────
# 触发器 → job 的连线（第七十五批）：**“这条触发器会唤醒哪些 job”以前只是散文。**
# 实景：cron 条目的 `why` 写着它同时唤醒 `mutation` 与 `image-signing`；而 `image-signing` 的
# `if:` 不接受 `schedule` ⇒ 修好默认分支也永远不会被它唤醒，那句承诺永远是假的。
# 散文没人检查就会漂移，所以这里把它落成字段 `fires_jobs`，并对着 workflow 的 `if:` 双向断言。

_CLAUSE_RE = re.compile(
    r"github(?:\.event_name|\[['\"]event_name['\"]\])\s*(?P<op>==|!=)\s*['\"](?P<event>[A-Za-z_]+)['\"]"
)
_EVENT_NAME_RE = re.compile(r"github\.event_name")
# `github.event.schedule` 是 GitHub 给 schedule 事件提供的字段（值就是触发它的那个 cron 串）：
# 拿它做比较的条件**只在 schedule 下成立**，所以必须被认成“接受 schedule”。
# 不认它的后果实测过（第七十五批独立复核给出的反例 A1）：写
# `if: github.event.schedule == '0 3 * * 1'` 的 job 真会被 cron 唤醒，却**不必出现在台账里**——
# 因为守卫看不懂 `github.event_name` 之外的条件，就把它当成“什么都不接受”了。
_SCHEDULE_CLAUSE_RE = re.compile(
    r"github(?:\.event\.schedule|\[['\"]event\.schedule['\"]\])\s*(?P<op>==|!=)\s*['\"]"
)
# 不认识的 `github.event.*` 用法必须**报错**：静默认成“什么都不接受”会让
# “连线断了”和“守卫看不懂”变成同一件事。（`github.ref` / `github.ref_name` 这类**收窄**不算，
# 它们不改变“这条 job 接受哪些事件”——`image-signing` 的 `startsWith(github.ref, …)` 就是例子。）
_EVENT_FIELD_RE = re.compile(
    r"github(?:\.event\.(?!schedule\b)[A-Za-z_]+|\[['\"]event\.[^'\"]*['\"]\])"
)


def _declared_events(workflow: str) -> set[str]:
    """workflow 的 `on:` 里声明了哪些事件（裸 `on:` 会被 YAML 1.1 解析成布尔键，两个都试）。"""
    return {str(key) for key in _workflow_triggers(workflow)}


def _job_conditions(workflow: str) -> dict[str, str]:
    """{job 名: `if:` 表达式}（没有 `if:` 就是空串，空串 = 所有声明的事件都跑）。"""
    data: Any = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8")) or {}
    out: dict[str, str] = {}
    for name, job in (data.get("jobs") or {}).items():
        cond = (job or {}).get("if")
        out[str(name)] = cond.strip() if isinstance(cond, str) else ""
    return out


def _accepted_events(workflow: str, expr: str) -> set[str]:
    """`if:` 表达式接受哪些事件。

    形状不认识就直接报错：**不认识的形状会让断言静默放过**（这是本仓反复吃过的亏）。
    """
    if not expr.strip():
        return _declared_events(workflow)
    accepted: set[str] = set()
    excluded: set[str] = set()
    for match in _CLAUSE_RE.finditer(expr):
        (accepted if match.group("op") == "==" else excluded).add(match.group("event"))
    for match in _SCHEDULE_CLAUSE_RE.finditer(expr):
        (accepted if match.group("op") == "==" else excluded).add("schedule")
    rest = _CLAUSE_RE.sub("", expr)
    rest = _SCHEDULE_CLAUSE_RE.sub("", rest)
    assert not _EVENT_NAME_RE.search(rest), (
        f"{workflow} 的 `if:` 里有守卫不认识的 `github.event_name` 用法：{expr!r}\n"
        "守卫只认 `github.event_name == 'x'` 与 `!= 'x'`；形状变了就得回来改这里，别让它静默放过。"
    )
    assert not _EVENT_FIELD_RE.search(rest), (
        f"{workflow} 的 `if:` 里有守卫不认识的 `github.event.*` 用法：{expr!r}\n"
        "这类条件可能改变“这条 job 接受哪些事件”，守卫看不懂时**必须报错**\n"
        "（静默忽略会把“连线断了”和“守卫看不懂”变成同一件事）。\n"
        "要支持新形状就来这里加一条正则 + 一个用例；只是**收窄**（`github.ref` 之类）不用管。"
    )
    if excluded:
        return (_declared_events(workflow) | accepted) - excluded
    return accepted


def test_触发器声明的唤醒对象必须真的接受它的事件() -> None:
    """`fires_jobs` 是“这条触发器唤醒谁”的唯一可机检形式；散文（`why`）不算证据。

    口径要看清：本用例只管**连线形状**——被点名的 job 的 `if:` 确实接受这条触发器的事件。
    “这条触发器是否真的会响”不在这里（那要现实证据，见
    `test_schedule_承诺与观察到的现实必须一致`；变异 M6 恰好是被它抓住的，这是变异矩阵
    当场抓出我自己写的一条用例名大过口径的结果）。
    """
    bad: list[str] = []
    for entry in _trigger_ledger():
        wf = str(entry["workflow"])
        kind = str(entry["kind"])
        where = f"{wf}:{kind}:{entry.get('cron')}"
        fires = entry.get("fires_jobs")
        assert isinstance(fires, list) and fires, (
            f"{where} 的 fires_jobs 必须是非空列表（这条触发器到底唤醒了谁）。"
            "一条谁都不唤醒的触发器不该留在台账里"
        )
        conditions = _job_conditions(wf)
        for job in fires:
            name = str(job)
            if name not in conditions:
                bad.append(f"{where} 的 fires_jobs 里有 {name!r}，但 {wf} 里没有这个 job")
                continue
            cond = conditions[name]
            if not cond.strip():
                bad.append(
                    f"{where} 的 fires_jobs 里有 {name!r}，但它没有 `if:`："
                    "所有事件都会跑它，不需要任何触发器“唤醒”；列在这里说明对机制的理解偏了"
                )
                continue
            accepted = _accepted_events(wf, cond)
            if kind not in accepted:
                bad.append(
                    f"{where} 说它会唤醒 {name}，但 {name} 的 `if:` 不接受 {kind}"
                    f"（接受的是 {sorted(accepted)}；条件：{cond!r}）"
                )
    assert not bad, "\n".join(bad)


def test_每个接受_schedule_的_job_都要出现在唤醒名单里() -> None:
    """反向：新写一个 `if:` 里接受 `schedule` 的 job，却没登记“这条 cron 唤醒它” ⇒ 红。"""
    bad: list[str] = []
    for entry in _trigger_ledger():
        if str(entry["kind"]) != "schedule":
            continue
        wf = str(entry["workflow"])
        claimed = {str(x) for x in (entry.get("fires_jobs") or [])}
        for name, cond in sorted(_job_conditions(wf).items()):
            if cond.strip() and "schedule" in _accepted_events(wf, cond) and name not in claimed:
                bad.append(
                    f"{wf}:{name} 的 `if:` 接受 schedule，但没有任何 schedule 触发器"
                    f"把它写进 fires_jobs（条件：{cond!r}）"
                )
    assert not bad, "\n".join(bad)


def test_解析器认得两种事件字段写法与_schedule_字段() -> None:
    """把解析器本身钉住（第七十五批独立复核给的 A1 反例）。

    三种写法必须等价；**不认识的形状必须报错**而不是静默返回空集——
    静默返回空集的后果实测过：写 `github.event.schedule == '…'` 的 job 真会被 cron 唤醒，
    却因为守卫看不懂而**不必登记**。
    """
    assert _accepted_events("ci.yml", "github.event_name == 'schedule'") == {"schedule"}
    assert _accepted_events("ci.yml", "github['event_name'] == 'schedule'") == {"schedule"}
    assert _accepted_events("ci.yml", "github.event.schedule == '0 3 * * 1'") == {"schedule"}
    assert _accepted_events("ci.yml", "github.event_name != 'schedule'") == {
        "push",
        "pull_request",
        "workflow_dispatch",
    }
    with pytest.raises(AssertionError):
        _accepted_events("ci.yml", "github.event.pull_request.draft == false")


def test_只在_schedule_上跑的_job_必须有一条_cron_会唤醒它() -> None:
    """反向的“睡不醒的 job”：`if:` 若**只**接受 `schedule`，就必须有 cron 登记说它唤醒它。

    为什么不能只看台账：`test_每个接受_schedule_的_job_都要出现在唤醒名单里` 只遍历台账里**已有**的
    schedule 条目——独立复核给出的反例 A2 正是从这里钻的：新写一个 workflow，里面放一个
    `if: github.event_name == 'schedule'` 的 job，全仓没有任何 cron ⇒ 那条用例不会红，
    而这个 job 永远等不到自己的那一天。本用例扫的是**全仓所有 workflow**，不依赖台账先有记录。
    """
    only_schedule: dict[str, list[str]] = {}
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        workflow = path.name
        if not _declared_events(workflow):
            continue
        for job, expr in _job_conditions(workflow).items():
            if _accepted_events(workflow, expr) == {"schedule"}:
                only_schedule.setdefault(workflow, []).append(job)

    fired: dict[str, set[str]] = {}
    for entry in _trigger_ledger():
        if entry.get("kind") == "schedule":
            fired.setdefault(str(entry.get("workflow")), set()).update(
                str(x) for x in (entry.get("fires_jobs") or [])
            )

    for workflow, jobs in only_schedule.items():
        for job in jobs:
            assert job in fired.get(workflow, set()), (
                f"`{workflow}` 的 `{job}` 只在 `schedule` 上跑，却没有任何 cron 登记说它唤醒它"
                "——要么给它所在的 workflow 补一条带 `fires_jobs` 的 `triggers:` 条目，"
                "要么它的 `if:` 里加上别的事件。这正是“凌晨那次唤醒从来没到过任何人手里”的形状。"
            )


def test_这条连线机制本身被真的用到过() -> None:
    """防空转：至少要有一条“触发器 → 带 `if:` 的 job，且 `if:` 里真有这个事件”的连线。"""
    pairs = 0
    for entry in _trigger_ledger():
        wf, kind = str(entry["workflow"]), str(entry["kind"])
        conditions = _job_conditions(wf)
        for job in entry.get("fires_jobs") or []:
            cond = conditions.get(str(job), "")
            if cond.strip() and kind in _accepted_events(wf, cond):
                pairs += 1
    assert pairs >= 1, (
        "没有任何一条触发器指向一个被 `if:` 门控的 job——上面两条断言等于空转"
        "（台账或 workflow 被改空了就会这样）"
    )


def test_每个_job_声称的事件必须真的被它自己的_workflow_声明过() -> None:
    """`runs_on` 里的每个事件，必须在**它的 workflow 的 `on:` 里**，否则要写 `runs_on_note`。

    口径要看清：这条只保证“事件被声明过”，**不保证“真的会被触发”**——`schedule` 是本仓现成的
    反例（`on:` 里有它，平台却从不求值）。真伪由 `test_schedule_承诺与观察到的现实必须一致`
    与新增的两条连线断言管。名字写大过口径，就是本批在治的病。
    """
    bad: list[str] = []
    for (wf, job), entry in sorted(_ledger().items()):
        declared = _declared_events(wf)
        note = str(entry.get("runs_on_note") or "").strip()
        for item in entry.get("runs_on") or []:
            event = str(item).split()[0]
            if event not in _EVENTS:
                bad.append(f"{wf}:{job} 的 runs_on 里 {item!r} 不是已知事件名")
            elif event not in declared and not note:
                bad.append(
                    f"{wf}:{job} 承诺在 {event} 上跑，但 {wf} 的 `on:` 里没有 {event}"
                    f"（现有：{sorted(declared)}）；要么改正 runs_on，要么写 runs_on_note 说明依据"
                )
    assert not bad, "\n".join(bad)


# 故意不在 CI 里跑的守卫脚本：键是文件名，值是理由。
# 「排除必须带理由」——没有理由的排除就是一条悄悄失效的门禁。
_NOT_IN_CI: dict[str, str] = {}


def test_每个守卫脚本都必须被_ci_真的调用() -> None:
    """「写了守卫」≠「CI 里有人跑它」：这是同一族病的脚本层版本。

    现场：本仓 `scripts/check_*.py` 共 7 个，前 6 个在 `ci.yml` 里各有独立步骤；
    第七十六批新加的 `scripts/check_doc_claims.py` 第一版**只活在本地**——
    脚本能跑、它自己的测试也能过、文档里也能写「已加守卫」，而 CI 里没人调用它：
    下次交付面上的 Markdown 又被写坏时，没有任何东西会红。

    （同一族的前三层已经在台账里各有一节：job 级 `if:`、step 级 `exit 0`、
    触发器级 `schedule`；这里补上第四层——**脚本到底有没有人调用**。）

    口径：只要求工作流文件的**非注释行**里出现过这个文件名（真能跑起来的那次调用必然要写它），
    不解析命令行参数——那会变成「钉在文字上」的判据，而文字判据不会红。
    **注释行不算**：本批给 `ci.yml` 那一步写了段「为什么单独一条」的注释，里面就点到这个文件名；
    只看整份文件的话，「把调用注释掉」也能过。
    """
    scripts = sorted(path.name for path in (REPO / "scripts").glob("check_*.py"))
    assert len(scripts) >= 7, f"只找到 {len(scripts)} 个守卫脚本，glob 或目录结构变了吗"
    invoked_lines = [
        line
        for path in sorted(WORKFLOWS.glob("*.y*ml"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]
    invoked = "\n".join(invoked_lines)
    orphaned = [name for name in scripts if name not in invoked and name not in _NOT_IN_CI]
    assert not orphaned, (
        f"这些守卫脚本在 .github/workflows 里一次都没被调用：{orphaned}"
        "——脚本存在不等于门禁存在。要么加一条步骤，要么写进 _NOT_IN_CI 并说明理由"
    )
    stale = sorted(name for name in _NOT_IN_CI if name not in scripts)
    assert not stale, f"_NOT_IN_CI 里的这些脚本已经不存在了：{stale}"


def test_触发器腐烂判据_在首交火当天不提前红() -> None:
    """`first == today` 不红，`first == today - 1` 且仍 never 就红。

    2026-10-19 现场：`0 3 * * 1` 的首个交点就是当天 03:00Z，而 UTC 当天早上还是 01:03Z。
    旧写法 `first <= today` 会在事件**还没到点**时变红，把「回去看它响没响」变成一次空跑——
    和写满 `never` 一样是假信号，只是方向反过来（假红）。
    """
    from datetime import date

    today = date(2026, 10, 19)
    base = {
        "workflow": "ci.yml",
        "kind": "schedule",
        "cron": "0 3 * * 1",
        "last_observed_fire": "never",
        "review_by": "2026-10-26",
    }
    same_day = [dict(base, last_checked=today, first_possible_fire=today)]
    assert _trigger_rot(same_day, today) == [], "首交火当天不许提前红"

    next_day = [dict(base, last_checked=today, first_possible_fire=date(2026, 10, 18))]
    problems = _trigger_rot(next_day, today)
    assert problems, "越过一天还写着 never 就必须红"
