"""生成器自己的判据：`scripts/mutant_check.py --evidence-out` 写出来的东西能不能当真。

为什么要给"生产者"写守卫：这份矩阵证据的全部价值，在于它的 `OK` 列是**从真实判定算出来的**。
如果生成器只是把期望抄进 `OK` 列，那么守卫会看起来全绿、实际上什么都没核——那正是这一批要消灭的病。

于是这里只核三件在最容易退化处的事：
1. **格式即契约**：它写出来的头行/终值行/表格行必须能被 `tests/test_matrix_evidence.py` 的正则原样吃下去
   （那边改了格式就要一起改；这里不过，说明证据根本进不了账）。
2. **期望没满足的行不许写成 `OK`**：否则 `MATRIX PASS` 就是假的。
3. **SKIP 行必须带原因**：`SKIP（）` 会被守卫判红，生成器不能产出这种行。
"""

import importlib.util
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "mutant_check", ROOT / "scripts" / "mutant_check.py"
)
assert _SPEC is not None and _SPEC.loader is not None
mc = importlib.util.module_from_spec(_SPEC)
sys.modules["mutant_check"] = mc
_SPEC.loader.exec_module(mc)

# 与 tests/test_matrix_evidence.py 里的守卫**同一套形状**（故意的重复：改一边就要改另一边）
HDR = re.compile(r"^=== matrix\S* \S+ HEAD=([0-9a-f]{40}) ===$", re.M)
PASS = re.compile(
    r"^MATRIX (PASS|FAIL): (\d+)/(\d+) 符合预期"
    r"（(\d+) 条正对照 \+ (\d+) 条反向对照(?:；[^）]*)?）$", re.M)
SHALLOW = re.compile(r"^SHALLOW depth=1: rc=(\d+) 红 (\d+) 条", re.M)
ROW_M = re.compile(r"^M\d+ .*$", re.M)
ROW_R = re.compile(r"^R\d+ .*$", re.M)
SKIP_MARK = "SKIP（"

SHA = "b" * 40


def _gen(tmp_path, rows, rc=1, red=3):
    out = tmp_path / "evidence" / "matrix" / "bbbbbbbbbbbb.log"
    total, pos, neg = mc._write_evidence(out, SHA, rows, rc, red)
    return out.read_text(encoding="utf-8"), total, pos, neg


def test_写出来的文本能被守卫的正则原样吃下去(tmp_path):
    rows = [
        ("KILLED", mc.KILL, "起点头被删掉时整条收摊"),
        ("KILLED?", mc.KILL, "基线本身红：弱证据也要能入账"),
        ("SURVIVED", mc.EQUIV, "codec 别名替换：等价生存"),
    ]
    text, total, pos, neg = _gen(tmp_path, rows)
    assert HDR.search(text).group(1) == SHA, "头行没绑定到被证明的提交"
    m = PASS.search(text)
    assert m and m.group(1) == "PASS"
    assert (int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))) == (total, total, pos, neg)
    assert (total, pos, neg) == (3, 2, 1), f"计数不对：{(total, pos, neg)}"
    assert SHALLOW.search(text).group(1) == "1" and SHALLOW.search(text).group(2) == "3"
    assert len(ROW_M.findall(text)) == pos and len(ROW_R.findall(text)) == neg
    live = [r for r in ROW_M.findall(text) + ROW_R.findall(text) if SKIP_MARK not in r]
    assert len(live) == total, "非 SKIP 行数必须等于 total"
    assert all(r.rstrip().endswith("OK") for r in live), "判定列被截断或有行没给结论"


def test_期望没满足的行不许写成_OK(tmp_path):
    """该红的没红（SURVIVED 而期望 KILL）⇒ 这一行末尾必须是 `!!`。

    生成器在 main() 里会先拒写文件；这里是第二道闸：万一有人绕过 main 直接调它，
    产出的文本也必须被守卫判红（行末不是 OK），而不是被当成通过。
    """
    rows = [("SURVIVED", mc.KILL, "这条本该被杀死")]
    text, _total, _pos, _neg = _gen(tmp_path, rows)
    line = ROW_M.search(text).group(0)
    assert line.rstrip().endswith("!!"), f"期望没满足却写成了 OK：{line!r}"
    assert mc._problem(rows), "缺口行必须让 _problem() 为真（main 才会拒写证据）"


def test_SKIP行必须带原因(tmp_path):
    """锚点没命中的行：既不计入对照数，也必须写明为什么，且不能算进非 SKIP 行。"""
    rows = [
        ("SKIP", mc.KILL, "源码改过，锚点没了"),
        ("SURVIVED", mc.EQUIV, "等价生存"),
    ]
    text, total, pos, neg = _gen(tmp_path, rows)
    line = ROW_M.search(text).group(0)
    assert SKIP_MARK in line and "源码改过" in line
    reason = line.split(SKIP_MARK, 1)[1].strip().rstrip("）").strip()
    assert reason, "SKIP 行写了空原因（守卫会判红）"
    assert (total, pos, neg) == (1, 0, 1), f"SKIP 不该计入对照数：{(total, pos, neg)}"
