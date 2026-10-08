"""第七十四批的门禁自测：把「门禁可以被静默关掉」的四个失败模式逐个钉成红。

这些用例不是"演示脚本"——每一条都对应一个**实测过的**失败模式，而且都能被变异矩阵打红：
  (f1) 一行 `# nosec` 让门禁静默变绿（压制计数可验）
  (f2) 目标失效 ⇒ 扫 0 行也绿（loc 下限）
  (f3) 名字说"Medium 及以上"，实际只覆盖 Medium 及以上**且 High confidence**（B608/B113 不在内）
  (f4) `|| true` / `--skip` / `--exit-zero` / `.bandit` / `[tool.bandit]` 这类开关无人看得见
"""

from __future__ import annotations

import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_sast_gate as gate  # noqa: E402

CI = REPO_ROOT / ".github/workflows/ci.yml"
GATE_RUN = (
    "run: uv run --frozen bandit -r src/warden_agent "
    "--severity-level medium --confidence-level medium"
)


def _defects(ci_text: str) -> set[str]:
    return {d.kind for d in gate.audit_ci(ci_text)}


def _root(
    tmp_path: pathlib.Path, *, ci_text: str | None = None, src: str | None = None
) -> pathlib.Path:
    """造一个最小仓库：真实 ci.yml（可替换）+ 一小撮 src/warden_agent 代码。"""
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / ".github/workflows/ci.yml").write_text(
        ci_text if ci_text is not None else CI.read_text(encoding="utf-8"), encoding="utf-8"
    )
    pkg = tmp_path / "src/warden_agent"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    if src is not None:
        (pkg / "mod.py").write_text(src, encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------- (f4) ci.yml 里的 argv


def test_真仓库的两个步骤参数与目标都是自洽的() -> None:
    assert gate.audit_ci(CI.read_text(encoding="utf-8")) == []


def test_门禁步骤被删掉或改名就红() -> None:
    text = CI.read_text(encoding="utf-8").replace(gate.GATE_STEP, "SAST（随便改个名）")
    assert "gate_step_missing" in _defects(text)


@pytest.mark.parametrize("token", ["|| true", "--exit-zero", "--skip B101", " -x"])
def test_门禁步骤含让它永不失败的东西就红(token: str) -> None:
    text = CI.read_text(encoding="utf-8")
    gate_line = GATE_RUN.removeprefix("run: ") + "\n"
    assert gate_line in text, "ci.yml 的门禁命令长得跟预期不一样，用例要跟着改"
    assert "gate_neutered" in _defects(
        text.replace(gate_line, gate_line.rstrip() + f" {token}\n")
    )


@pytest.mark.parametrize("dropped", ["--severity-level medium ", "--confidence-level medium"])
def test_门禁少一个开关就红(dropped: str) -> None:
    # 只能改 run 行：注释里也写着这两个开关（口径说明），改到注释上等于什么都没改
    text = CI.read_text(encoding="utf-8")
    run_line = GATE_RUN
    assert run_line + "\n" in text, "ci.yml 的门禁命令长得跟预期不一样，用例要跟着改"
    assert "gate_flags_changed" in _defects(
        text.replace(run_line, run_line.replace(dropped, ""), 1)
    )


def test_打印步骤和门禁步骤扫的不是同一个目标就红() -> None:
    text = CI.read_text(encoding="utf-8").replace(
        "bandit -r src/warden_agent --severity-level medium",
        "bandit -r src --severity-level medium",
    )
    assert "target_mismatch" in _defects(text)


def test_门禁注释必须写明真实口径是_Medium_且_High_confidence() -> None:
    """名字比口径大是本批实测出来的缺陷（B608/B113 是 Medium/LOW，门禁从不看它们）。"""
    text = CI.read_text(encoding="utf-8")
    assert "High confidence" in text, "门禁注释要写清口径：它只管 High confidence"


# ---------------------------------------------------------------- (f1)(f2) 真扫、真对照


def test_正向对照_Medium_档缺陷确实会被门禁拦住() -> None:
    measured = gate.control_positive()
    assert measured["exit"] != 0, "含 B307（Medium/High）的夹具居然通过了门禁——正对照失效"
    assert measured["SEVERITY.MEDIUM"] == 1 and measured["results"] == 1


def test_负面对照_一行_nosec_就静默变绿而且压制是可计数的() -> None:
    measured = gate.control_suppressed()
    assert measured["exit"] == 0, "加了 # nosec 却没变绿：夹具可能没喂到扫描器"
    assert measured["results"] == 0
    assert measured["skipped_tests"] == 1, "bandit 自己会报压制条数；这个数丢了，压制就彻底不可见了"


def test_空扫描自检_目标不存在时_loc_为_0() -> None:
    """这就是 loc 下限在守的东西：扫了 0 行，bandit 依然 exit=0。"""
    measured = gate.control_empty_target()
    assert measured["loc"] == 0 and measured["exit"] == 0


def test_只扫到一点点代码算缺陷而不是通过(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path, src="X = 1\n")
    defects, measured = gate.audit_repo(root, "src/warden_agent")
    assert measured["exit"] == 0  # 门禁眼里它"干净"
    assert {d.kind for d in defects} == {"empty_scan"}  # 守卫眼里它是"什么都没扫到"


# ---------------------------------------------------------------- (f1) 压制与签字


def test_被压制的发现没有签字就红_签了字才放行(tmp_path: pathlib.Path) -> None:
    src = (
        "import builtins\n\n\ndef f(s: str) -> object:\n"
        "    return eval(s)  # nosec B307  # noqa: PGH001\n"
    )
    root = _root(tmp_path, src=src)
    defects, measured = gate.audit_repo(root, "src/warden_agent")
    assert measured["skipped_tests"] == 1
    assert "suppressed_without_signature" in {d.kind for d in defects}

    (root / "tests").mkdir()
    (root / gate.LEDGER).write_text(
        "suppressions:\n"
        "  - file: src/warden_agent/mod.py\n"
        "    code: B307\n"
        "    reason: 用例夹具，故意压制以验证守卫\n"
        "    signed_by: tests/test_sast_gate.py\n",
        encoding="utf-8",
    )
    defects2, _ = gate.audit_repo(root, "src/warden_agent")
    assert "suppressed_without_signature" not in {d.kind for d in defects2}


def test_配置旁路_bandit_文件或_tool_bandit_段落就红(tmp_path: pathlib.Path) -> None:
    root = _root(tmp_path)
    assert gate.audit_suppression_switches(root) == []
    (root / ".bandit").write_text("[bandit]\nskips = B608\n", encoding="utf-8")
    assert "config_bypass" in {d.kind for d in gate.audit_suppression_switches(root)}
    (root / ".bandit").unlink()
    (root / "pyproject.toml").write_text('[tool.bandit]\nskips = ["B608"]\n', encoding="utf-8")
    assert "config_bypass" in {d.kind for d in gate.audit_suppression_switches(root)}


# ---------------------------------------------------------------- (f3) 口径差


def test_Medium_档被_confidence_过滤掉时红(tmp_path: pathlib.Path) -> None:
    """B608 是 Medium/LOW：门禁参数下 results=0、exit=0，但它确实是一条 Medium 发现。"""
    src = 'def f(uid: str) -> str:\n    return "SELECT * FROM t WHERE id = \'%s\'" % uid\n'
    root = _root(tmp_path, src=src)
    defects, measured = gate.audit_repo(root, "src/warden_agent")
    assert measured["SEVERITY.MEDIUM"] >= 1 and measured["results"] == 0
    assert "severity_filtered_out" in {d.kind for d in defects}


def test_真仓库现在既零压制又没有漏掉的_Medium_发现() -> None:
    """本批的基线：loc=21162 / Medium=0 / nosec=0 / skipped_tests=0。"""
    defects, measured = gate.audit_repo(REPO_ROOT, "src/warden_agent")
    assert defects == [], [d.render() for d in defects]
    assert measured["loc"] >= gate.MIN_LOC
    assert (measured["nosec"], measured["skipped_tests"], measured["results"]) == (0, 0, 0)


def test_脚本的退出码语义_0_干净_1_缺陷_2_启动失败(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert gate.main(["--root", str(REPO_ROOT)]) == 0
    assert gate.main(["--root", str(tmp_path)]) == 2  # 没有 ci.yml：被测物不在，不能算通过
    _root(tmp_path, src="X = 1\n")
    assert gate.main(["--root", str(tmp_path)]) == 1  # 扫不到东西 ⇒ 缺陷
    assert "empty_scan" in capsys.readouterr().out
