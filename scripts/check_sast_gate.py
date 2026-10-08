#!/usr/bin/env python
"""SAST 门禁的口径守卫：门禁可以被三行之外的任何东西静默关掉，脚本负责把这些都变成红灯。

## 这条门禁要防的四个失败模式（都实测过，证据在下面）

(f1) **一行 `# nosec` 把门禁从红变绿，而绿里看不出被压了几条。**
     实测（bandit 1.9.4）：夹具里 `eval(s)`（B307，Medium/High）在门禁参数下
     `exit=1 / SEVERITY.MEDIUM=1 / results=1`；**同一夹具只加 `# nosec B307`** 之后
     `exit=0 / SEVERITY.MEDIUM=0 / results=0`，而 bandit 自己报的 `skipped_tests` 从 0 变成 **1**。
     ⇒ 压制是静默的，但**可计数**：只要有人肯看 `skipped_tests`/`nosec` 这两个数。
     所以本脚本断言这两个数就是 0（要压制就必须在台账里签字）。

(f2) **目标写错、目录改名 ⇒ bandit 扫 0 行，照样 `exit=0`。**
     实测：在不含 `src/warden_agent` 的目录里跑门禁那条命令 ⇒ `loc=0`、`exit=0`、无任何输出差别。
     ⇒ "Medium 0" 可能只是"什么都没扫"。所以本脚本设 `loc` 下限（今天实测 21162 行）。

(f3) **门禁的名字比它的口径大：真正拦住的是「Medium 及以上**且 High confidence**」。**
     实测：B608（SQL 字符串拼接，MEDIUM/**LOW**）、B113（requests 无 timeout，MEDIUM/LOW）
     在门禁参数下 `results=0 / exit=0` —— **根本不在门禁覆盖内**，而 B608 正是本仓历史上
     真用 `# nosec B608` 压制过的那条规则。名字叫"Medium 及以上必须为 0"，读的人会以为它管。
     ⇒ 本脚本断言 `SEVERITY.MEDIUM + SEVERITY.HIGH == results`：一旦出现"Medium 档但
     confidence 为 Low、被过滤器悄悄漏掉"的发现，立刻红，并把这个口径差写在注释与 README 里。

(f4) **`|| true` / `--skip` / `--exit-zero` / `.bandit` / `[tool.bandit] skips` 这些开关。**
     它们一样能把门禁变成摆设，而且**没有一个断言看得见**——今天全靠 ci.yml 里那句散文
     （"现在无任何 #nosec 压制"）。散文不是断言 ⇒ 本脚本把 ci.yml 那两个步骤的 argv 当作**被测物**：
     参数、目标、禁用词都断言；打印步骤与门禁步骤**必须扫同一个目标**（否则"打印出来的"和
     "拦下来的"是两件事）。

## 判据只用退出码 + bandit 自己的 metrics
不 grep 文本、不人读输出：`loc` / `nosec` / `skipped_tests` / `SEVERITY.*` / `results` 全部来自
bandit 的 JSON。理由与 `scripts/gitleaks_reverse_control.sh` 同源（那边第一版用
`grep -q "leaks found"` 当判据，而 "no leaks found" 也含这个子串 ⇒ 那条对照**永真**）。

## 对照（不是演示，是会红的东西）
- **正对照**：含 B307 的夹具在门禁参数下必须 `exit != 0`（证明门禁真的拦得住 Medium 档）；
- **负对照自检**：夹具加 `# nosec` 后必须 `exit == 0` 且 `skipped_tests == 1`
  （证明正对照真的喂到了扫描器；挂载/路径错导致的"扫了个空目录"会在这里现形）；
- **空扫描自检**：目标不存在时必须 `loc == 0`（证明 `loc` 下限在守什么）。

## 退出码
0=全部通过；1=有缺陷（含压制未签字）；2=启动失败（bandit 不在、ci.yml 读不出来、空扫描）。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
from dataclasses import dataclass

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# 门禁那一步的名字：改名字会让守卫直接找不到被测物（这是有意的，改名必须同时改这里）。
GATE_STEP = "SAST 门禁：Medium 及以上必须为 0"
PRINT_STEP = "SAST（bandit）—— 打印全部发现（含 Low，只记录不拦截）"
# 门禁的真实口径：Medium 及以上 **且** High confidence。两个开关都必须原样在 argv 里。
GATE_FLAGS = ("--severity-level", "medium", "--confidence-level", "medium")
# 让门禁永不失败/永不扫描的写法。命中任何一条 ⇒ 门禁已不是门禁。
FORBIDDEN = ("|| true", "--exit-zero", "--skip", "-s ", " -x", "--ignore", "; true", "|| exit 0")
# 今天实测 21162 行（src/warden_agent）。下限不是为了好看，是为了"扫了 0 行也绿"这条路。
MIN_LOC = 12_000
LEDGER = "tests/SAST_SUPPRESSION_LEDGER.yaml"


@dataclass(frozen=True)
class Defect:
    kind: str
    where: str
    detail: str

    def render(self) -> str:
        return f"❌ [{self.kind}] {self.where}：{self.detail}"


def _steps(ci_text: str) -> list[dict[str, object]]:
    """取出 security job 的 steps（用 yaml，不自己写解析器）。"""
    import yaml  # pyyaml 是运行依赖（pyproject: pyyaml>=6.0）

    data = yaml.safe_load(ci_text)
    steps: list[dict[str, object]] = []
    for job in (data.get("jobs") or {}).values():
        if isinstance(job, dict):
            steps.extend(s for s in (job.get("steps") or []) if isinstance(s, dict))
    return steps


def _find(steps: list[dict[str, object]], name: str) -> str | None:
    for step in steps:
        if step.get("name") == name:
            return str(step.get("run", ""))
    return None


def _target(run: str) -> str | None:
    """取 bandit 的扫描目标（`-r <path>`）。两个步骤必须扫同一个目标。"""
    parts = run.split()
    for i, part in enumerate(parts):
        if part == "-r" and i + 1 < len(parts):
            return parts[i + 1]
    return None


def audit_ci(ci_text: str) -> list[Defect]:
    """把 ci.yml 当被测物：参数、目标、禁用词。纯函数，便于自测与变异。"""
    defects: list[Defect] = []
    steps = _steps(ci_text)
    gate = _find(steps, GATE_STEP)
    printed = _find(steps, PRINT_STEP)
    if gate is None:
        return [Defect("gate_step_missing", GATE_STEP, "找不到这个步骤——门禁被删除或改名了")]
    if printed is None:
        defects.append(
            Defect("print_step_missing", PRINT_STEP, "找不到打印步骤（它在给「看得见」兜底）"),
        )

    flags = " ".join(GATE_FLAGS)
    if flags not in " ".join(gate.split()):
        defects.append(
            Defect(
                "gate_flags_changed", GATE_STEP,
                f"argv 里必须原样含 `{flags}`，实际：{gate.strip()}",
            ),
        )
    for token in FORBIDDEN:
        if token in gate:
            defects.append(
                Defect("gate_neutered", GATE_STEP, f"含 `{token}`：这条命令已经不可能是门禁了"),
            )
    gate_target = _target(gate)
    if gate_target is None:
        defects.append(
            Defect(
                "gate_target_missing", GATE_STEP,
                f"argv 里没有 `-r <target>`：{gate.strip()}",
            ),
        )
    elif printed is not None:
        print_target = _target(printed)
        if print_target != gate_target:
            defects.append(
                Defect(
                    "target_mismatch",
                    f"{PRINT_STEP} vs {GATE_STEP}",
                    f"打印的是 {print_target}、拦的是 {gate_target}——"
                    "那样「打印出来看得见」和「拦得住」就是两件事",
                ),
            )
    return defects


def audit_suppression_switches(root: pathlib.Path) -> list[Defect]:
    """.bandit / bandit.yaml / [tool.bandit] 都是"换一份配置"的旁路开关。"""
    defects: list[Defect] = []
    for name in (".bandit", "bandit.yaml", ".bandit.yaml"):
        if (root / name).exists():
            defects.append(
                Defect(
                    "config_bypass", name,
                    "多了一份 bandit 配置：门禁的参数从此可能被文件里的 skips 覆盖",
                ),
            )
    pyproject = root / "pyproject.toml"
    has_tool_bandit = pyproject.is_file() and "[tool.bandit]" in pyproject.read_text(
        encoding="utf-8", errors="replace",
    )
    if has_tool_bandit:
        defects.append(
            Defect(
                "config_bypass", "pyproject.toml",
                "含 [tool.bandit]：skips/exclude 会静默改变门禁口径",
            ),
        )
    return defects


def run_bandit(args: list[str], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "bandit", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd),
        env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        check=False,
    )


def totals(completed: subprocess.CompletedProcess[str]) -> dict[str, int]:
    """bandit 自己报的数字。解析不出来就返回 parse_error=-1（上层会当启动失败）。"""
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"parse_error": -1}
    raw = data.get("metrics", {}).get("_totals", {})
    out = {
        key: int(raw.get(key, -1))
        for key in ("loc", "nosec", "skipped_tests", "SEVERITY.MEDIUM", "SEVERITY.HIGH")
    }
    out["results"] = len(data.get("results", []))
    out["exit"] = completed.returncode
    return out


def gate_args(target: str) -> list[str]:
    return ["-r", target, "-f", "json", "-q", *GATE_FLAGS]


FIXTURE_VULN = 'import builtins\n\n\ndef f(s: str) -> object:\n    return eval(s)  # noqa: PGH001\n'
FIXTURE_NOSEC = (
    "import builtins\n\n\ndef f(s: str) -> object:\n"
    "    return eval(s)  # nosec B307  # noqa: PGH001\n"
)


def control_positive(flags: tuple[str, ...] = GATE_FLAGS) -> dict[str, int]:
    """正对照：含 Medium 档缺陷的夹具，在门禁参数下必须被拦（exit != 0）。"""
    with tempfile.TemporaryDirectory() as td:
        work = pathlib.Path(td)
        (work / "fixture.py").write_text(FIXTURE_VULN, encoding="utf-8")
        return totals(run_bandit(["-r", ".", "-f", "json", "-q", *flags], work))


def control_suppressed(flags: tuple[str, ...] = GATE_FLAGS) -> dict[str, int]:
    """负面对照自检：加一行 `# nosec` 必须**静默**变绿，且压制计数为 1。"""
    with tempfile.TemporaryDirectory() as td:
        work = pathlib.Path(td)
        (work / "fixture.py").write_text(FIXTURE_NOSEC, encoding="utf-8")
        return totals(run_bandit(["-r", ".", "-f", "json", "-q", *flags], work))


def control_empty_target() -> dict[str, int]:
    """空扫描自检：目标不存在时 bandit 照样 exit=0、loc=0 —— 这就是 loc 下限在守的东西。"""
    with tempfile.TemporaryDirectory() as td:
        return totals(run_bandit(gate_args("src/warden_agent"), pathlib.Path(td)))


def read_ledger(root: pathlib.Path) -> list[str]:
    """台账里登记过的压制（file+code+reason+signed_by 齐全才算数）。"""
    path = root / LEDGER
    if not path.is_file():
        return []
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    signed = []
    for entry in data.get("suppressions") or []:
        if not isinstance(entry, dict):
            continue
        if all(entry.get(k) for k in ("file", "code", "reason", "signed_by")):
            signed.append(f"{entry['file']}:{entry['code']}")
    return signed


def nosec_sites(root: pathlib.Path, target: str) -> list[str]:
    """被扫目录里 `# nosec` 的确切位置（报错时要指名道姓，而不是只给一个计数）。"""
    sites: list[str] = []
    for path in sorted((root / target).rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            if "nosec" in line.lower():
                sites.append(f"{path.relative_to(root).as_posix()}:{lineno}")
    return sites


def audit_repo(root: pathlib.Path, target: str) -> tuple[list[Defect], dict[str, int]]:
    """跑真门禁口径，然后把「口径是否可信」逐条断言。"""
    defects: list[Defect] = []
    measured = totals(run_bandit(gate_args(target), root))
    if measured.get("parse_error", 0) == -1:
        return [
            Defect("bandit_unparsable", target, "bandit 没吐出可解析的 JSON（启动失败？）"),
        ], measured

    if measured["loc"] < MIN_LOC:
        defects.append(
            Defect(
                "empty_scan",
                target,
                f"只扫到 {measured['loc']} 行（下限 {MIN_LOC}）——「Medium 0」可能只是没扫到东西",
            ),
        )
    signed = read_ledger(root)
    if measured["nosec"] or measured["skipped_tests"]:
        sites = nosec_sites(root, target)
        unsigned = [
            site for site in sites
            if not any(site.startswith(entry.split(":")[0]) for entry in signed)
        ]
        if unsigned:
            defects.append(
                Defect(
                    "suppressed_without_signature",
                    target,
                    f"bandit 报 nosec={measured['nosec']} "
                    f"skipped_tests={measured['skipped_tests']}，"
                    f"未签字的压制点：{unsigned[:5]}——"
                    f"压制必须在 {LEDGER} 里写理由与签字人",
                ),
            )
    medium_high = measured["SEVERITY.MEDIUM"] + measured["SEVERITY.HIGH"]
    if medium_high != measured["results"]:
        defects.append(
            Defect(
                "severity_filtered_out",
                target,
                f"Medium/High 档共 {medium_high} 条，但门禁口径下只报出 {measured['results']} 条："
                "有一条因 confidence 为 Low 被过滤器漏掉了（B608/B113 就是这一档）。"
                "要么修掉它，要么把门禁口径写清楚并在此处登记",
            ),
        )
    return defects, measured


def main(argv: list[str] | None = None) -> int:
    # 本机控制台是 cp936：`❌` 直接 print 会 UnicodeEncodeError（本仓已踩过多次）。
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    parser = argparse.ArgumentParser(description="SAST 门禁的口径守卫（真扫、真对照）")
    parser.add_argument("--root", default=str(REPO_ROOT), help="仓库根（默认本脚本所在仓库）")
    args = parser.parse_args(argv)
    root = pathlib.Path(args.root).resolve()
    ci = root / ".github/workflows/ci.yml"
    if not ci.is_file():
        print(f"❌ 读不到 {ci}：门禁的被测物不在，守卫无法工作")
        return 2
    if run_bandit(["--version"], root).returncode != 0:
        print(
            "❌ bandit 跑不起来（`python -m bandit`）：守卫无法工作。"
            "CI 里由 `uv run --frozen` 提供",
        )
        return 2

    defects = audit_ci(ci.read_text(encoding="utf-8"))
    defects += audit_suppression_switches(root)

    positive = control_positive()
    if positive.get("exit") == 0:
        defects.append(
            Defect(
                "control_positive", "夹具 B307",
                f"含 Medium 缺陷的夹具竟然通过了门禁：{positive}",
            ),
        )
    suppressed = control_suppressed()
    if suppressed.get("exit") != 0 or suppressed.get("skipped_tests", 0) < 1:
        defects.append(
            Defect(
                "control_not_fed",
                "夹具 B307 + # nosec",
                f"压制后应当 exit=0 且 skipped_tests>=1"
                f"（证明对照真的喂到了扫描器），实际：{suppressed}",
            ),
        )
    empty = control_empty_target()
    if empty.get("loc") != 0:
        defects.append(
            Defect("control_empty_target", "空目录", f"目标不存在时应当 loc=0，实际：{empty}"),
        )

    gate_step_run = _find(_steps(ci.read_text(encoding="utf-8")), GATE_STEP) or ""
    gate_target = _target(gate_step_run) or "src/warden_agent"
    repo_defects, measured = audit_repo(root, gate_target)
    defects += repo_defects

    for defect in defects:
        print(defect.render())
    print(
        f"\n门禁口径实测：loc={measured.get('loc')} Medium={measured.get('SEVERITY.MEDIUM')} "
        f"High={measured.get('SEVERITY.HIGH')} results={measured.get('results')} "
        f"nosec={measured.get('nosec')} skipped_tests={measured.get('skipped_tests')} "
        f"exit={measured.get('exit')}（下限 loc>={MIN_LOC}）",
    )
    print(
        f"对照：正对照 exit={positive.get('exit')}（期望 1）"
        f"｜压制后 exit={suppressed.get('exit')} "
        f"skipped_tests={suppressed.get('skipped_tests')}（期望 0/1）"
        f"｜空扫描 loc={empty.get('loc')}（期望 0）",
    )
    if defects:
        return 1
    print(
        "✅ SAST 门禁的口径可信：参数未被削弱、没有静默压制、"
        "也没有被 confidence 档漏掉的 Medium 发现",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
