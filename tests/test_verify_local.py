"""判据：本地验证总闸本身必须诚实 —— 非 0 要累计、覆盖面与 CI 的差要登记。

治的两个真实事故（第八十七批，同一天发生）：

1. **红藏在绿里**：交付时把几条验证命令用 shell 串起来跑，只看得到最后一条的退出码，
   中间 pytest 红着而整串退 0，于是「全绿」被报了出去。⇒ `run_stages` 必须累计退出码，
   本文件用**注入一个假 runner** 的方式钉住它（不真起子进程、不依赖耗时）。
2. **本地比 CI 少跑却不说**：手工挑「受影响的那几个守卫文件」跑，而 CI 的 quality job 有
   17 个命名步骤 —— 挑着跑本身没错，错的是**不说话**。⇒ 对着 `.github/workflows/ci.yml`
   的 quality job 双向对账：每个 CI 命名步骤要么被某个本地阶段指向，要么在 `EXEMPT` 里登记理由；
   本地阶段的 `ci_step` 也必须真的存在（防止写了个人家不跑的步骤名）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("verify_local", ROOT / "scripts" / "verify_local.py")
assert _spec and _spec.loader
verify_local = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify_local)

_STAGES = verify_local._stages(Path("junit.xml"))


def _ci_quality_steps() -> list[str]:
    """CI quality job 里所有带 `name:` 的步骤名（逐字）。"""
    data = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    steps = data["jobs"]["quality"]["steps"]
    return [str(s["name"]) for s in steps if "name" in s]


def _fake_runner(codes: list[int]):
    """假 runner：按顺序吐退出码，并记录被调用的 argv。"""
    seen: list[list[str]] = []

    def run(argv: list[str], _env: dict[str, str]) -> int:
        seen.append(argv)
        return codes[len(seen) - 1]

    return run, seen


def test_中间阶段红着时整体必须非_0_不看最后一条退出码():
    """第八十七批事故复现：三段里第二段红 ⇒ 整体必须非 0。"""
    stages = [
        {"name": "一", "ci_step": "Ruff (lint)", "argv": ["x"]},
        {"name": "二", "ci_step": "Mypy (strict)", "argv": ["y"]},
        {"name": "三", "ci_step": "Pytest（含覆盖率门槛）", "argv": ["z"]},
    ]
    run, seen = _fake_runner([0, 1, 0])
    rc, results = verify_local.run_stages(stages, None, runner=run)
    assert rc == 1, "中间一段红却整体退 0 —— 这正是把「全绿」报出去的那次事故"
    assert [c for _, c, _ in results] == [0, 1, 0], "每段的退出码都要留在汇总里"
    assert len(seen) == 3, "红了也要把后面的阶段跑完（否则汇总缺项，读者会以为它绿）"


def test_全绿才返回_0():
    stages = [{"name": "一", "ci_step": "Ruff (lint)", "argv": ["x"]}]
    run, _ = _fake_runner([0])
    rc, results = verify_local.run_stages(stages, None, runner=run)
    assert (rc, results[0][1]) == (0, 0)


def test_形状守卫没给_base_时必须记账为没跑而不是静默绿():
    """`--base` 缺席时形状守卫不能假装绿：应记为 -1（未跑）且不影响退出码。"""
    stages = [verify_local._shape_stage(None)]
    run, seen = _fake_runner([0])
    rc, results = verify_local.run_stages(stages, None, runner=run)
    assert rc == 0 and results[0][1] == -1, "没给 --base 却报绿（或不记账）会让人以为形状核过了"
    assert not seen, "没给 --base 就不该真的去跑它"


def test_本地阶段的_ci_step_必须真实存在于_CI_quality_job():
    """防止把阶段指向一个 CI 里不存在的步骤名（那会让「与 CI 对账」变成自说自话）。"""
    known = set(_ci_quality_steps())
    bad = [st["ci_step"] for st in _STAGES if st.get("ci_step") and st["ci_step"] not in known]
    assert not bad, f"这些 ci_step 在 ci.yml 的 quality job 里不存在：{bad}"


def test_本地专有阶段必须写明理由():
    for st in _STAGES:
        if not st.get("ci_step"):
            why = str(st.get("local_only", "")).strip()
            assert why, f"阶段 {st['name']} 既没指向 CI 步骤，也没写为什么本地专有"


def test_CI_quality_job_的每个命名步骤都被覆盖或登记豁免():
    """双向对账的另一半：CI 有的、本地不跑的，必须逐条登记理由（缺席要看得见）。"""
    known = set(_ci_quality_steps())
    covered = {str(st["ci_step"]) for st in _STAGES if st.get("ci_step")}
    declared = set(verify_local.EXEMPT)
    missing = known - covered - declared
    assert not missing, f"这些 CI 步骤本地既没覆盖也没登记豁免（悄悄比 CI 少跑）：{sorted(missing)}"
    stale = declared - known
    assert not stale, f"EXEMPT 里登记了 CI 里不存在的步骤（被改名或删了？）：{sorted(stale)}"
    both = declared & covered
    assert not both, f"这些步骤既被本地阶段覆盖又登记成豁免（自相矛盾）：{sorted(both)}"


def test_豁免理由必须是真理由_不是占位符():
    thin: list[str] = []
    for step, why in verify_local.EXEMPT.items():
        text = str(why).strip()
        if len(text) < 20 or text in {"略", "不重要", "TODO", "-"}:
            thin.append(step)
    assert not thin, f"这些豁免理由太薄，等于没登记：{thin}"
