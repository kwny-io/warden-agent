"""交付面审计的判据（第八十七批）。

对标的是**两个已经发生过的失败**，不是「想象中可能失败」：

  * 2026-10-15 17:57 推上 `dev` 的 `5c0faa5` 起，dev 的 `quality` 红着（台账缺 `HEAD^` 的行），
    **21 小时里没有任何东西响过** —— 所以本文件里「tip 红 ⇒ 有发现」是一条真判据，
    而「tip 绿 ⇒ 一条发现都没有」是它的反向对照（没有反向对照，判据可能只是恒红）。
  * 「拿最近一次 completed run 当 tip 的颜色」会在**周一 03:00**（`schedule`）上把每个周一喊成红 ——
    push 门控的 job 在 schedule 上本来就该 skipped。`test_挑tip的run_必须按事件挑` 钉这一条。

判据自己也得有牙：`CORE` / `EXPECT_SKIP` 必须与 `scripts/ci_evidence.py` 里那份**相等**
（两边故意各写一遍，靠这条用例把它们绑在一起），工作流本身也被钉住（定时、权限、脚本路径）。
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "delivery-audit.yml"


def _load_module(name: str, rel: str) -> ModuleType:
    """按路径加载 `scripts/` 下的脚本（`scripts/` 不是包，本仓脚本都各自独立）。

    加载**真模块**而不是在用例里重写一遍分类逻辑：那样 `mutant_check.py` 里
    登记在 `scripts/delivery_audit.py` 上的变异才有牙（重写版会把变异放过去）。
    """
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


audit = _load_module("delivery_audit_under_test", "scripts/delivery_audit.py")

GREEN_JOBS: list[dict[str, Any]] = [
    {"name": n, "conclusion": "success", "status": "completed"}
    for n in audit.CORE
] + [
    {"name": "变异测试（mutation-shard-1）", "conclusion": "skipped", "status": "completed"},
    {"name": "image-signing", "conclusion": "skipped", "status": "completed"},
]


def _push_run(**over: Any) -> dict[str, Any]:
    run: dict[str, Any] = {
        "databaseId": 123,
        "headSha": "a" * 40,
        "status": "completed",
        "conclusion": "success",
        "event": "push",
        "createdAt": "2026-10-16T01:00:00Z",
    }
    run.update(over)
    return run


# ── 1. 反向对照：全绿时一条发现都不该有 ────────────────────────────────────────
def test_分支tip全绿_一条发现都没有() -> None:
    assert audit.audit_run("dev@a" * 1, _push_run(), GREEN_JOBS) == []


# ── 2. tip 红 ⇒ 报出 job 名与 run 结论 ────────────────────────────────────────
def test_分支tip红_报出job名与run结论() -> None:
    jobs = [dict(j) for j in GREEN_JOBS]
    for job in jobs:
        if job["name"] == "quality":
            job["conclusion"] = "failure"
    found = audit.audit_run("dev@aaaaaaa", _push_run(conclusion="failure"), jobs)
    text = "\n".join(f.line() for f in found)
    assert "整个 run 不绿" in text
    assert "quality" in text and "failure" in text


# ── 3/4/5. 跳过与不跳过，两边都要有牙 ────────────────────────────────────────
def test_登记过该跳过的job在push上跳过_不喊() -> None:
    found = audit.audit_run("dev@aaaaaaa", _push_run(), GREEN_JOBS)
    assert not [f for f in found if "跳过" in f.criterion or "没登记" in f.criterion]


def test_核心job被跳过_算红() -> None:
    jobs = [dict(j) for j in GREEN_JOBS]
    for job in jobs:
        if job["name"] == "security":
            job["conclusion"] = "skipped"
    found = audit.audit_run("dev@aaaaaaa", _push_run(), jobs)
    assert any("核心 job 被跳过" in f.criterion and "security" in f.detail for f in found)


def test_登记该跳过的job这次没跳过_算红() -> None:
    jobs = [dict(j) for j in GREEN_JOBS]
    for job in jobs:
        if str(job["name"]).startswith("变异测试"):
            job["conclusion"] = "success"
    found = audit.audit_run("dev@aaaaaaa", _push_run(), jobs)
    assert any(
        "登记为「push 上该跳过」的 job 这次没跳过" in f.criterion and "登记过期" in f.detail
        for f in found
    )


def test_没登记的job出现_算红() -> None:
    jobs = [*GREEN_JOBS, {"name": "新来的job", "conclusion": "success", "status": "completed"}]
    found = audit.audit_run("dev@aaaaaaa", _push_run(), jobs)
    assert any("没登记的 job" in f.criterion and "新来的job" in f.detail for f in found)


# ── 6. 没跑完就不许当成绿 ───────────────────────────────────────────────────
def test_run没跑完_直接回答没跑完() -> None:
    found = audit.audit_run("dev@aaaaaaa", _push_run(status="in_progress", conclusion=None), [])
    assert len(found) == 1 and "还没跑完" in found[0].criterion


# ── 7/8. 挑「tip 那次 push 的 run」：按事件、按 sha ────────────────────────────
def test_挑tip的run_必须按事件挑_不能拿schedule的run充数() -> None:
    runs = [
        _push_run(databaseId=9, event="schedule", conclusion="failure"),  # 周一那一跑：会误报
        _push_run(databaseId=8, event="push", conclusion="success"),
    ]
    picked = audit.select_tip_run("a" * 40, runs)
    assert picked is not None and picked["databaseId"] == 8


def test_挑tip的run_只认tip那个sha_别人推的run不算() -> None:
    runs = [_push_run(headSha="b" * 40)]
    assert audit.select_tip_run("a" * 40, runs) is None


# ── 9. 常量同源：判据与证据守卫不许各漂各的 ───────────────────────────────────
def _const(rel: str, name: str) -> object:
    text = (ROOT / rel).read_text(encoding="utf-8")
    match = re.search(rf"^{name} = (.+)$", text, re.M)
    assert match, f"{rel} 里找不到 {name}"
    return ast.literal_eval(match.group(1))


def test_同源_CORE与EXPECT_SKIP必须与证据守卫相等() -> None:
    assert tuple(_const("scripts/ci_evidence.py", "CORE")) == tuple(audit.CORE)
    assert tuple(_const("scripts/ci_evidence.py", "EXPECT_SKIP")) == tuple(audit.EXPECT_SKIP)


# ── 10/11. 证据文件的离线核 ─────────────────────────────────────────────────
def test_证据文件不在树里_算红(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """脚本按仓库相对路径报位置（`relative_to(ROOT)`），所以用例要把 ROOT 一起钉到 tmp 上。"""
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    found = audit.audit_evidence_file(tmp_path / "nope.json")
    assert len(found) == 1 and "树里" in found[0].criterion


def test_证据里conclusion不绿_算红(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    path = tmp_path / "e.json"
    path.write_text(
        '{"schema": "warden-ci-evidence/1", "branches": {"dev": {'
        '"conclusion": "failure", "jobs": [{"name": "quality", "conclusion": "failure"}]}}}',
        encoding="utf-8",
    )
    text = "\n".join(f.line() for f in audit.audit_evidence_file(path))
    assert "dev 的 run 不绿" in text and "quality" in text


# ── 12. 对着真仓库：台账声明了几份证据，就得有几份真过判据（且不清零）───────────
def test_真台账声明的证据文件_在真仓里全过判据() -> None:
    paths = audit.ledger_evidence_paths(ROOT / "tests" / "DELIVERY_LEDGER.yaml")
    assert paths, "台账里一份 ci_evidence 都没声明 ⇒ 这条判据等于空转，别再放宽"
    for path in paths:
        assert audit.audit_evidence_file(path) == [], f"{path} 没通过离线判据"


# ── 13. 工作流本身被钉住：定时、权限、脚本路径 ───────────────────────────────
def _workflow() -> dict[str, Any]:
    data: Any = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")) or {}
    assert isinstance(data, dict)
    # PyYAML 把裸 `on:` 解析成布尔 True（YAML 1.1），两种键都认。
    return data


def test_工作流钉住定时与写权限与脚本路径() -> None:
    wf = _workflow()
    triggers = wf.get("on") or wf.get(True)
    assert triggers["schedule"][0]["cron"] == "0 6 * * *"
    assert "workflow_dispatch" in triggers
    perms = wf["permissions"]
    assert perms["contents"] == "read" and perms["actions"] == "read"
    assert perms["issues"] == "write", "不会喊的审计等于没有审计"
    steps = wf["jobs"]["audit"]["steps"]
    checkout = [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@")]
    assert checkout, "没检出树就没法核 evidence/ci/*.json"
    assert re.fullmatch(r"actions/checkout@[0-9a-f]{40}", checkout[0]["uses"]), "action 必须钉 SHA"
    assert checkout[0]["with"]["ref"] == "dev", "审的是交付面 dev，不是默认分支"
    runs = [str(s.get("run", "")) for s in steps]
    assert any("scripts/delivery_audit.py" in r and "--alert-issue" in r for r in runs)


# ── 14. 报警正文不许是空的（红了却贴一条空 issue 等于没喊）────────────────────
def test_报告_红时列出红点_绿时明说没有() -> None:
    green = audit.render_report([], ["dev@aaaaaaa: 绿"])
    assert "没有。" in green
    finding = audit.Finding("dev@aaaaaaa", "整个 run 不绿", "conclusion='failure'")
    red = audit.render_report([finding], ["dev@aaaaaaa: 1 项红"])
    assert "conclusion='failure'" in red and "dev@aaaaaaa" in red
