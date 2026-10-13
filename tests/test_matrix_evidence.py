"""「守卫有牙」的**仓内**证据守卫（第八十二批 洞1）。

要治的缺口（第八十一批实测出来的，不是猜）：

  * 台账与交付面都写着「变异矩阵 14/14 PASS ⇒ 守卫有牙」，但**回执里没有任何矩阵字段**
    （`grep -l 矩阵 receipts/*.yaml | wc -l` ⇒ 0）；
  * 矩阵日志住在**仓库外**（`wa81/matrix82f1.log` 这种），台账只把它的 sha256 抄进来；
  * 而回执里的 `log` / `log_sha256` **一个字都不参与任何判定** —— 这一点不是我在文档里声明的，
    是第八十一批变异矩阵自己的**反向对照 R14**（「改回执里的日志名 —— 不该红」）证明的。
  ⇒ 仓外那份日志一丢/被覆盖（第七十九批真撞过：「回执已被覆盖、不可复核」），
  「守卫有牙」立刻退化成**不可复核的断言**。

判据（四条，全部由 git 与仓内字节推导；回执里不许自述载体）：

  1. **绑定**：`mutation.evidence` 必须是 `evidence/matrix/<sha12>.log`，且仓内那份文件的
     sha256 等于回执里的 `evidence_sha256`（逐字节）。
  2. **自述一致**：证据文件自己写着 `HEAD=<40 位 sha>`，必须等于被证明的提交；
     终值行 `MATRIX PASS: n/n 符合预期（p 条正对照 + q 条反向对照）` 与
     `SHALLOW depth=1: rc=1 红 k 条` 必须与回执字段逐项相等；表格里的对照行数
     （`M\\d+` / `R\\d+` 开头的行）也必须等于 `p` / `q`。
     ⇒ 光把文件哈希钉住还不够：**回执里的数字必须真是那份文件里的数字**，否则「14/14」只是自说自话。
  3. **载体同源**：证据与回执必须由**同一个提交**带进来 —— 否则「证据」可以在事后换上另一份
     （回执的载体由下一个提交背书，证据必须跟着同一个载体走）。
  4. **真入库**（不是「盘上存在」）：证据必须在 git 索引里、且不命中忽略规则；
     且必须**豁免 EOL 归一化**（证据按字节哈希，文本文件在 Windows/Linux 上字节不同）。
     实测的坑：仓根 `.gitignore` 的 `*.log` 会把 `evidence/matrix/*.log` **静默**排除 ——
     `git add -A` 不报错、本地照绿，而 CI 检出后那份证据根本不存在，
     「守卫有牙」只在开发者的机器上成立。盘上有 ≠ 入库，所以判据落在 `git ls-files` 上。

切点（`CUT`）：第八十二批 **F1 的提交** = 证据入库的切点 = **第一份带 `mutation` 的回执**
所证明的提交（该回执由紧随其后的 F2 携带入库，机制固有的一步滞后）。自切点（含）之后
每份回执都必须带 `mutation`；更早的历史回执没有它 —— 按历史放行，**不回填**
（回执的回填规则不许回填，见 `tests/test_delivery_receipts.py`）。切点写成常量而不是从
「哪份回执有字段」反推：反推会有一个逃生口 —— 把唯一那份带字段的回执的字段删掉，切点就消失了。

本文件是**自包含**的：不 import 仓库外的生成器（`wa82/gen-receipt.py`），
所以那几个正则在这里重写一遍。这不是重复代码，是「守卫不依赖被守卫者」。
"""

from __future__ import annotations

import hashlib
import pathlib
import re
import subprocess

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
RECEIPTS = ROOT / "receipts"
EVIDENCE = ROOT / "evidence" / "matrix"

# 第八十二批 F1 的提交 = 证据入库的切点（第一份带 mutation 的回执所证明的提交，由 F2 携带）。
# 判据本身是在 F2 那次提交里落地的，但切点是 F1 —— 两者差一步，别混。
CUT = "12d4514fda8e26c88200bfe885171ce0370b4609"

REQUIRED_MUTATION = {"evidence", "evidence_sha256", "total", "matched",
                     "positive", "negative", "shallow_red"}
SHA12 = re.compile(r"^[0-9a-f]{12}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")

# 生成器（mutate82.py）写出来的三行格式。守卫在这里**重写**正则、并**拒绝静默**：
# 任何一行匹配不上 ⇒ 红（格式改了就要跟着改守卫，不能悄悄少核一项）。
HDR = re.compile(r"^=== matrix\S* \S+ HEAD=([0-9a-f]{40}) ===$", re.M)
PASS = re.compile(
    r"^MATRIX (PASS|FAIL): (\d+)/(\d+) 符合预期（(\d+) 条正对照 \+ (\d+) 条反向对照）$", re.M)
SHALLOW = re.compile(r"^SHALLOW depth=1: rc=(\d+) 红 (\d+) 条", re.M)
ROW_M = re.compile(r"^M\d+ ", re.M)
ROW_R = re.compile(r"^R\d+ ", re.M)


def _git(*args: str) -> str:
    r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 0, f"git {' '.join(args)} 失败：{r.stderr.strip()}"
    return r.stdout.strip()


def _added_by(path: str) -> str:
    return _git("log", "--diff-filter=A", "--format=%H", "-1", "--", path)


def _after_cut(sha: str) -> bool:
    """sha 在切点上或之后（切点是它的祖先或它自己）。"""
    r = subprocess.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", CUT, sha],
                       capture_output=True)
    return r.returncode == 0


def _load(p: pathlib.Path) -> dict:
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def receipt_paths() -> list[pathlib.Path]:
    return sorted(RECEIPTS.glob("*.yaml")) if RECEIPTS.is_dir() else []


def with_mutation() -> list[pathlib.Path]:
    return [p for p in receipt_paths() if "mutation" in _load(p)]


def evidence_text(p: pathlib.Path) -> str:
    return (ROOT / _load(p)["mutation"]["evidence"]).read_text(encoding="utf-8", errors="replace")


# ---------- 形状 ----------

def test_证据目录存在且非空():
    assert EVIDENCE.is_dir(), f"缺 {EVIDENCE}（证据入库是这一批的目的，空目录等于没做）"
    assert list(EVIDENCE.glob("*.log")), "evidence/matrix/ 里一份产物都没有"


def test_切点在本仓历史里():
    assert _git("rev-parse", "--verify", f"{CUT}^{{commit}}"), f"切点 {CUT[:12]} 不在本仓"
    r = subprocess.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", CUT, "HEAD"],
                       capture_output=True)
    assert r.returncode == 0, (
        f"切点 {CUT[:12]} 不是 HEAD 的祖先 ⇒ 这份守卫在浅检出/别的分支上是查不动而不是没问题"
        f"（CI 的 quality job 要 fetch-depth: 0）")


# ---------- 判据 1：绑定到不可变对象（仓内字节） ----------

@pytest.mark.parametrize("p", with_mutation(), ids=lambda p: p.name)
def test_字段自洽(p: pathlib.Path):
    d = _load(p)
    m = d["mutation"]
    assert set(m) >= REQUIRED_MUTATION, f"{p.name} mutation 缺 {REQUIRED_MUTATION - set(m)}"
    sha12 = d["sha"][:12]
    assert m["evidence"] == f"evidence/matrix/{sha12}.log", (
        f"{p.name} 的证据路径是 {m['evidence']}，必须是 evidence/matrix/{sha12}.log —— "
        f"文件名自己就是被证明的提交（和回执名同一条判据）")
    assert HEX64.match(str(m["evidence_sha256"])), f"{p.name} evidence_sha256 不是 64 位 hex"
    assert m["matched"] == m["total"], f"{p.name} matched={m['matched']} ≠ total={m['total']}"
    assert m["positive"] + m["negative"] == m["total"], (
        f"{p.name} 正/反 {m['positive']}+{m['negative']} ≠ total={m['total']}")
    assert m["positive"] >= 1 and m["negative"] >= 1, (
        f"{p.name} 对照条数必须两向都有（只有正对照 = 「任何改动都红」的假阳机器；"
        f"只有反向对照 = 没有牙）")
    assert m["shallow_red"] >= 1, f"{p.name} 没记浅检出红条数 ⇒ fetch-depth: 0 的承重性无从复核"


@pytest.mark.parametrize("p", with_mutation(), ids=lambda p: p.name)
def test_证据在仓里且逐字节相符(p: pathlib.Path):
    m = _load(p)["mutation"]
    f = ROOT / m["evidence"]
    assert f.is_file(), f"{p.name} 说证据是 {m['evidence']}，仓里没有这个文件"
    got = hashlib.sha256(f.read_bytes()).hexdigest()
    assert got == m["evidence_sha256"], (
        f"{p.name} 的证据 sha256 不符：回执写 {m['evidence_sha256'][:16]}，仓内是 {got[:16]} ⇒ "
        f"「守卫有牙」的仓内证据被改过（或被换成另一份）")


# ---------- 判据 2：证据自己说的话必须等于回执里的话 ----------

@pytest.mark.parametrize("p", with_mutation(), ids=lambda p: p.name)
def test_证据真在_git_里_没被忽略规则吞掉(p: pathlib.Path):
    """证据必须**真的在 git 里** —— 盘上有 ≠ 入库。

    实测的坑（第八十二批 F2 撞到的）：仓根 `.gitignore` 有一条 `*.log`，
    于是 `evidence/matrix/<sha12>.log` 被**静默**排除：`git add -A` 不报错、本地测试照绿
    （文件在盘上），而 CI 检出后那份文件**根本不存在** —— 「守卫有牙」的证据只在开发者的
    机器上成立。所以这条判据落在 `git ls-files` 上，`is_file()` 那条只管盘上有没有。
    """
    m = _load(p)["mutation"]
    r = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--error-unmatch", m["evidence"]],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, (
        f"{p.name} 的证据 {m['evidence']} 不在 git 索引里 ⇒ 它只是盘上的文件，"
        f"换台机器/CI 检出后就没有了（被忽略规则吞掉的证据等于不存在）")
    ig = subprocess.run(["git", "-C", str(ROOT), "check-ignore", m["evidence"]],
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert ig.returncode != 0, (
        f"{p.name} 的证据 {m['evidence']} 命中忽略规则 `{ig.stdout.strip()}` ⇒ "
        f"入库会被静默跳过（要给它加一条 `!` 例外）")


@pytest.mark.parametrize("p", with_mutation(), ids=lambda p: p.name)
def test_证据的字节不会被检出时的换行归一化改掉(p: pathlib.Path):
    """证据必须显式豁免 EOL 归一化（仓根 `.gitattributes` 是 `* text=auto eol=lf`）。

    证据是按**字节**哈希的（回执里的 `evidence_sha256`）：一旦 git 在入库/检出时做了 EOL 归一，
    同一份证据在 Windows 工作区（CRLF）与 Linux CI（LF）上就是两个不同的 sha256
    ⇒ **本地绿、CI 红**，而且报红的那条判据（逐字节相符）跟真因（换行）八竿子打不着。
    第八十二批 F2 实测撞到过：证据是从 Windows 上 `cp` 过来的 CRLF 文件，`git add` 直接警告
    「CRLF will be replaced by LF」。豁免写进 `.gitattributes`，这条判据负责别让它被删掉。
    """
    m = _load(p)["mutation"]
    r = subprocess.run(["git", "-C", str(ROOT), "check-attr", "text", "--", m["evidence"]],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr.strip()
    got = r.stdout.strip()
    assert got.endswith("text: unset"), (
        f"{p.name} 的证据 {m['evidence']} 的属性是 `{got}` ⇒ 没豁免换行归一化："
        f"它在 Windows 工作区与 Linux CI 上会是两个不同的 sha256"
        f"（要在 `.gitattributes` 里加 `-text`）")


@pytest.mark.parametrize("p", with_mutation(), ids=lambda p: p.name)
def test_证据自述的被证明提交就是被证明的提交(p: pathlib.Path):
    d = _load(p)
    t = evidence_text(p)
    hdr = HDR.search(t)
    assert hdr, f"{p.name} 的证据里没有 `=== matrix… HEAD=<40 位 sha> ===` 头行 ⇒ 无法绑定提交"
    assert hdr.group(1) == d["sha"], (
        f"{p.name} 的证据自称跑在 {hdr.group(1)[:12]}，而回执说被证明的是 {d['sha'][:12]}")


@pytest.mark.parametrize("p", with_mutation(), ids=lambda p: p.name)
def test_证据的终值与回执逐项一致(p: pathlib.Path):
    m = _load(p)["mutation"]
    t = evidence_text(p)
    line = PASS.search(t)
    assert line, (
        f"{p.name} 的证据里没有 `MATRIX PASS: n/m 符合预期（p 条正对照 + q 条反向对照）` 行")
    assert line.group(1) == "PASS", f"{p.name} 的证据终值是 MATRIX FAIL ⇒ 红的矩阵不许当证据入库"
    matched, total, pos, neg = (int(line.group(i)) for i in (2, 3, 4, 5))
    assert (matched, total, pos, neg) == (m["matched"], m["total"], m["positive"], m["negative"]), (
        f"{p.name} 回执写 {m['matched']}/{m['total']}（{m['positive']}+{m['negative']}），"
        f"证据文件写 {matched}/{total}（{pos}+{neg}）⇒ 回执里的数字不是那份文件里的数字")
    s = SHALLOW.search(t)
    assert s, f"{p.name} 的证据里没有 `SHALLOW depth=1: rc=1 红 k 条` 行"
    assert s.group(1) == "1", (
        f"{p.name} 的浅检出对照 rc={s.group(1)} ⇒ 浅检出没过红，"
        f"「fetch-depth: 0 是承重的」没被证明")
    assert int(s.group(2)) == m["shallow_red"], (
        f"{p.name} 回执记浅检出红 {m['shallow_red']} 条，证据文件记 {s.group(2)} 条")


@pytest.mark.parametrize("p", with_mutation(), ids=lambda p: p.name)
def test_证据的表格行数就是对照条数(p: pathlib.Path):
    m = _load(p)["mutation"]
    t = evidence_text(p)
    assert "不 OK" not in t and "INVALID" not in t, (
        f"{p.name} 的证据里有 `不 OK`/`INVALID` 行 ⇒ 矩阵没跑完或没全中，不能当「有牙」的证据")
    assert len(ROW_M.findall(t)) == m["positive"], (
        f"{p.name} 回执说 {m['positive']} 条正对照，证据表格里 {len(ROW_M.findall(t))} 行")
    assert len(ROW_R.findall(t)) == m["negative"], (
        f"{p.name} 回执说 {m['negative']} 条反向对照，证据表格里 {len(ROW_R.findall(t))} 行")


# ---------- 判据 3：载体同源 ----------

@pytest.mark.parametrize("p", with_mutation(), ids=lambda p: p.name)
def test_回执与证据由同一个提交带进来(p: pathlib.Path):
    m = _load(p)["mutation"]
    a_rec = _added_by(p.relative_to(ROOT).as_posix())
    a_evi = _added_by(m["evidence"])
    assert a_rec == a_evi, (
        f"{p.name} 由 {a_rec[:12]} 带进来，而它的证据由 {a_evi[:12]} 带进来 ⇒ 不同源："
        f"证据可以在事后换成另一份（载体必须跟着回执走）")


# ---------- 切点：之后的每一份回执都必须带证据 ----------

def test_切点之后每一份回执都必须带矩阵证据():
    if not receipt_paths():
        pytest.skip("还没有回执")
    missing = [p.name for p in receipt_paths()
               if _after_cut(_load(p)["sha"]) and "mutation" not in _load(p)]
    assert not missing, (
        f"切点 {CUT[:12]} 之后的回执没有 mutation 字段：{missing} ⇒ "
        f"「守卫有牙」又变回仓外断言了（切点之前的历史回执按历史放行，不回填）")
