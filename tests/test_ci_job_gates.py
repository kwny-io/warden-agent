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
     否则下一次想知道门控后面还活着没有，只能靠猜）。
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
