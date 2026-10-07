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
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

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
