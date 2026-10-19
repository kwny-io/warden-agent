"""回执里 `mutation` 字段的**生产者判据**：不合格的证据不许变成回执里的一个数字。

前一批补的是「矩阵证据由矩阵自己写」（`scripts/mutant_check.py --evidence-out`）；
这一份补另一半：`scripts/freeze_receipt.py --mutation-evidence` 得**自己再核一遍**，
不能因为日志是自家生成器写的就照单全收——仓外那条 `gen-receipt.py` 当年就是这般把
「期望没满足的行」直接写成 `OK` 的。

所以这里每条反向对照都对应一个真实会出事的形状：
- `MATRIX FAIL` 入库 ⇒ 有缺口的矩阵变成交付证据；
- SKIP 行被算进 positive/negative ⇒ 没跑的臂被当成通过（生成器初版就这么错过）；
- `SHALLOW depth=1: rc=0` ⇒ 判据在浅检出不承重，"fetch-depth: 0 承重"这句话是假的；
- 证据自报的 sha ≠ 要背书的 sha ⇒ 拿别人的绿来背书自己；
- 文件名不是 `<sha12>.log` ⇒ 守卫按 sha 找证据会找不到。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SHA = "0123456789abcdef0123456789abcdef01234567"


def _load(tmp_path: pathlib.Path):
    spec = importlib.util.spec_from_file_location("freeze_receipt", ROOT / "scripts" / "freeze_receipt.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["freeze_receipt"] = mod
    spec.loader.exec_module(mod)
    mod.ROOT = tmp_path  # mutation_block 里所有路径都在 ROOT 下算
    return mod


GOOD = f"""矩阵证据（这是 scripts/freeze_receipt.py 的自测样本，不是真产物的替代品）。

基线：tests/test_run_lock.py tests/test_run_lease.py。
=== matrix mutant_check HEAD={SHA} ===
M1  KILLED   基线绿  某个变异（说明）
M2  KILLED   基线绿  另一个变异（说明）
R1  KILLED   基线绿  反向对照（说明）
MATRIX PASS: 3/3 符合预期（2 条正对照 + 1 条反向对照）
SHALLOW depth=1: rc=1 红 73 条（只看到 HEAD 的克隆里，回执/证据/台账判据必然红）
"""


def _write(tmp_path: pathlib.Path, text: str, *, name: str | None = None) -> pathlib.Path:
    p = tmp_path / "evidence" / "matrix" / (name or f"{SHA[:12]}.log")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_正常证据能解出全部字段(tmp_path):
    mod = _load(tmp_path)
    got = mod.mutation_block(_write(tmp_path, GOOD), SHA)
    assert got["evidence"] == f"evidence/matrix/{SHA[:12]}.log"
    assert got["total"] == got["matched"] == 3
    assert got["positive"] == 2 and got["negative"] == 1
    assert got["shallow_red"] == 73
    assert len(got["evidence_sha256"]) == 64


def test_MATRIX_FAIL_不许进回执(tmp_path):
    mod = _load(tmp_path)
    bad = GOOD.replace("MATRIX PASS: 3/3", "MATRIX FAIL: 2/3")
    with pytest.raises(ValueError, match="MATRIX FAIL"):
        mod.mutation_block(_write(tmp_path, bad), SHA)


def test_SKIP_行被算进通过就拒(tmp_path):
    """生成器初版真踩过：SKIP 行占编号也被计进 positive ⇒ 正+反 ≠ 总。"""
    mod = _load(tmp_path)
    bad = GOOD.replace(
        "R1  KILLED   基线绿  反向对照（说明）",
        "R1  SKIP（本相位不适用）",
    ).replace("MATRIX PASS: 3/3 符合预期（2 条正对照 + 1 条反向对照）",
              "MATRIX PASS: 3/3 符合预期（2 条正对照 + 1 条反向对照）")
    with pytest.raises(ValueError, match="非 SKIP 的行"):
        mod.mutation_block(_write(tmp_path, bad), SHA)


def test_浅检出全绿就拒(tmp_path):
    """rc=0 意味着「判据在浅克隆里也全过」⇒ fetch-depth: 0 承重这句话不成立，证据自相矛盾。"""
    mod = _load(tmp_path)
    bad = GOOD.replace("SHALLOW depth=1: rc=1 红 73 条", "SHALLOW depth=1: rc=0 红 0 条")
    with pytest.raises(ValueError, match="期望 1"):
        mod.mutation_block(_write(tmp_path, bad), SHA)


def test_缺浅检出那一行就拒(tmp_path):
    mod = _load(tmp_path)
    bad = "\n".join(l for l in GOOD.splitlines() if not l.startswith("SHALLOW"))
    with pytest.raises(ValueError, match="SHALLOW"):
        mod.mutation_block(_write(tmp_path, bad), SHA)


def test_证据自报的_sha_对不上就拒(tmp_path):
    mod = _load(tmp_path)
    other = "f" * 40
    bad = GOOD.replace(SHA, other)
    with pytest.raises(ValueError, match="自称跑在"):
        mod.mutation_block(_write(tmp_path, bad), SHA)


def test_文件名必须是_sha12(tmp_path):
    mod = _load(tmp_path)
    with pytest.raises(ValueError, match="evidence/matrix/"):
        mod.mutation_block(_write(tmp_path, GOOD, name="latest.log"), SHA)


# ── 崩溃型缺陷：pytest 跑完但 stdout 是 None（2026-10-19 真撞到） ────────────────
def test_pytest日志拼接到_None_不许崩():
    """整整跑了十几分钟，`proc.stdout` 是 None ⇒ `stdout + stderr` 直接 TypeError。

    那一刻回执本该由 junit 生成；因为「日志形状」把整条冻结链崩掉，是判据把自己当成了目标。
    反向对照也在：真的拿到文本时，两路都得拼进去（别为了防崩把 stderr 丢了）。
    """
    import types

    mod = _load(pathlib.Path("."))
    assert mod._pytest_log(types.SimpleNamespace(stdout=None, stderr=None)) == ""
    assert mod._pytest_log(types.SimpleNamespace(stdout="A", stderr=None)) == "A"
    assert mod._pytest_log(types.SimpleNamespace(stdout=None, stderr="B")) == "B"
    assert mod._pytest_log(types.SimpleNamespace(stdout="A", stderr="B")) == "AB"
