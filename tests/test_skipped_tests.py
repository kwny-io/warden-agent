"""`scripts/check_skipped_tests.py`（防「跳过即静默」守卫）的回归。

为什么要有这些测试：这条守卫的价值全在「它会不会红」上。如果它永远返回 0，那就是
第六十批那条教训的翻版——**守卫自己成了摆设**。所以这里逐条构造「它必须红的场景」：

  1. junit 里出现一条没签字的跳过             ⇒ 红（逼人当场表态）
  2. CI 里跳过了 allowed_in=[local] 的用例     ⇒ 红（门禁里变空的那条路）
  3. 同一条用例在本地跳过                     ⇒ 绿（本地条件不满足是正当的）
  4. 参数化用例名（`test_x[参数]`）也要能被签字命中 ⇒ 绿
     （这个坑我当场踩过：`…::test_x[p]` 不以 `…::test_x::` 开头，只按 `::` 前缀匹配会漏）
  5. 文件级签字命中该文件下所有用例           ⇒ 绿
  6. 台账缺字段 / allowed_in 非法 / 含 ci 却没 review_by ⇒ 红（豁免必须写清、且带期限）
  7. CI 里没有 junit 报告                     ⇒ 红（「守卫自己没数据」不是通过）；本地放行

外加两条「守卫的守卫」：
  8. 真仓库的台账现在就能通过（防 YAML/字段被改坏）
  9. `ci.yml` 里「预热规则求值镜像」那一步必须仍在 **Pytest 之前**、签字守卫在 **之后**
     —— 这两条命令的位置就是这个批次承诺的全部机制：删掉预热步骤，求值用例就会在
     CI 里静默跳过（守卫那时才会红，但红得晚了一步：那是事后）。所以位置本身要被钉住。
    第六十五批又补两条同形状的：
  10. 「安装 ts 客户端依赖」那一步也必须在 **Pytest 之前**：少了它，`tests/test_mcp.py` 里
      4 条真实连接用例**每天在 CI 里跳过**（日志里的原因：`ts 客户端依赖未安装`），
      而本机装了依赖所以本地永远看不到——这就是本批那个坑的形状。
  11. 「上传 junit」那一步必须存在、且带 `if: always()`：守卫要守的是 **CI**，而跳过集
      按环境变（本机 4 条 / CI 8 条）。Pytest 红的时候恰好最需要那份 junit。
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "check_skipped_tests.py"
LEDGER = ROOT / "tests" / "SKIP_LEDGER.yaml"

_PROM_CASE = "tests/test_alert_rules_evaluated.py::test_promtool真值求值通过[warden.rules.yml]"


def _junit(tmp_path: Path, cases: list[tuple[str, str]]) -> Path:
    """按 (case_id, 跳过原因) 造一份 junit；case_id 用 `模块路径::用例名` 形态。"""
    path = tmp_path / "junit.xml"
    rows = []
    total = 0
    for case_id, reason in cases:
        module, _, name = case_id.partition("::")
        classname = module.removesuffix(".py").replace("/", ".")
        rows.append(
            f'<testcase classname="{classname}" name="{name}" time="0.01">'
            f'<skipped message="{reason}" /></testcase>'
        )
        total += 1
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<testsuites><testsuite name="pytest" '
        f'tests="{total}" skipped="{total}">' + "".join(rows) + "</testsuite></testsuites>\n",
        encoding="utf-8",
    )
    return path


def _write_ledger(tmp_path: Path, entries: list[dict[str, object]]) -> Path:
    path = tmp_path / "SKIP_LEDGER.yaml"
    path.write_text(yaml.safe_dump({"entries": entries}, allow_unicode=True), encoding="utf-8")
    return path


def _entry(
    case_id: str, allowed_in: list[str], *, review_by: str | None = None
) -> dict[str, object]:
    entry: dict[str, object] = {
        "id": case_id,
        "allowed_in": allowed_in,
        "reason": "测试构造的签字：本机条件不满足",
        "added": "2026-10-05",
    }
    if review_by:
        entry["review_by"] = review_by
    return entry


def _run(
    tmp_path: Path,
    cases: list[tuple[str, str]] | None = None,
    *,
    entries: list[dict[str, object]] | None = None,
    ci: bool = False,
    junit: bool = True,
) -> tuple[int, str]:
    args = [sys.executable, str(GUARD)]
    if junit:
        if cases is None:
            cases = []
        args += ["--junitxml", str(_junit(tmp_path, cases))]
    if entries is not None:
        args += ["--ledger", str(_write_ledger(tmp_path, entries))]
    env = {k: v for k, v in os.environ.items() if not k.startswith("WARDEN_TEST_PG_")}
    # Windows 控制台默认 cp936，编不了（与守卫无关，只为断言好读）
    env["PYTHONIOENCODING"] = "utf-8"
    if ci:
        env["CI"] = "1"
    else:
        env.pop("CI", None)
    done = subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8", env=env, timeout=120
    )
    return done.returncode, done.stdout + done.stderr


def test_未签字的跳过会被拦下(tmp_path: Path) -> None:
    """junit 里多出一条没人签字的跳过 ⇒ 红，而且消息要点名是哪一条、修法是什么。"""
    rc, out = _run(
        tmp_path,
        [("tests/test_brand_new.py::test_新加的用例", "本机没有 XX 工具")],
        entries=[],
    )
    assert rc == 1, out
    assert "未签字的跳过" in out and "tests/test_brand_new.py::test_新加的用例" in out
    assert "SKIP_LEDGER.yaml" in out, "消息里必须说清去哪儿签字"


def test_CI里跳过只允许本地跳的用例会被拦下(tmp_path: Path) -> None:
    """**本批的核心回归**：预热镜像那一步没了 ⇒ 求值用例在 CI 里跳过 ⇒ 必须红。"""
    rc, out = _run(
        tmp_path,
        [(_PROM_CASE, "没有 prom/prometheus:v2.55.0 镜像")],
        entries=[_entry(_PROM_CASE, ["local"])],
        ci=True,
    )
    assert rc == 1, out
    assert "CI 里跳过了「只允许本地跳过」的用例" in out
    assert _PROM_CASE in out


def test_同一条用例在本地跳过是允许的(tmp_path: Path) -> None:
    """本地没预热镜像是正当的（测试故意不联网拉镜像）——同一个 junit，本地必须放行。"""
    rc, out = _run(
        tmp_path,
        [(_PROM_CASE, "没有 prom/prometheus:v2.55.0 镜像")],
        entries=[_entry(_PROM_CASE, ["local"])],
    )
    assert rc == 0, out


def test_参数化用例名也能被签字命中(tmp_path: Path) -> None:
    """`test_x[参数]` 紧跟中括号、没有 `::`——只按 `::` 前缀匹配会静默漏掉（我踩过）。"""
    rc, out = _run(
        tmp_path,
        [(_PROM_CASE, "没有镜像")],
        entries=[_entry(_PROM_CASE, ["local", "ci"], review_by="2027-01-03")],
    )
    assert rc == 0, out


def test_文件级签字命中该文件下所有用例(tmp_path: Path) -> None:
    """文件级签字（PG 那一套）要能覆盖文件里所有用例；别的文件不能跟着沾光。"""
    entries = [_entry("tests/test_postgres_integration.py", ["local"])]
    rc, out = _run(
        tmp_path,
        [("tests/test_postgres_integration.py::test_甲", "需要可用的 PostgreSQL"),
         ("tests/test_postgres_integration.py::test_乙[参数]", "需要可用的 PostgreSQL")],
        entries=entries,
    )
    assert rc == 0, out

    rc, out = _run(
        tmp_path,
        [("tests/test_other.py::test_丙", "需要可用的 PostgreSQL")],
        entries=entries,
    )
    assert rc == 1 and "未签字的跳过" in out, out


def test_台账字段不合法会被拦下(tmp_path: Path) -> None:
    """豁免必须写清理由、allowed_in 只能是 ci/local；含 ci 的必须带 review_by（空洞有期限）。"""
    rc, out = _run(tmp_path, [], entries=[{"id": "tests/test_x.py", "allowed_in": ["local"]}])
    assert rc == 1 and "缺必填字段" in out, out

    rc, out = _run(tmp_path, [], entries=[_entry("tests/test_x.py", ["anywhere"])])
    assert rc == 1 and "allowed_in 只能是 ci / local" in out, out

    rc, out = _run(tmp_path, [], entries=[_entry("tests/test_x.py", ["ci"])])
    assert rc == 1 and "必须写 review_by" in out, out

    rc, out = _run(
        tmp_path, [], entries=[_entry("tests/test_x.py", ["local"], review_by="明年")]
    )
    assert rc == 1 and "review_by 不是 YYYY-MM-DD" in out, out


def test_CI里没有junit报告是失败_本地放行(tmp_path: Path) -> None:
    """「守卫自己没数据」在门禁里必须是失败——否则它会很安静地什么都验不了。"""
    rc, out = _run(tmp_path, None, entries=[], ci=True, junit=False)
    assert rc == 1 and "必须是失败" in out, out

    rc, out = _run(tmp_path, None, entries=[], junit=False)
    assert rc == 0 and "本地放行" in out, out


def test_真仓库的台账现在就能通过() -> None:
    """防把台账改坏：YAML 合法、字段齐全、id 都能解析。"""
    done = subprocess.run(
        [sys.executable, str(GUARD), "--ledger", str(LEDGER)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=120,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_所有PG门控都走同一个可用性判据() -> None:
    """PG 门控只能有一处实现：测试里每次 `psycopg.connect(...)` 都必须带上 `**_pg_params()`。

    `tests/test_store_interface.py` 曾经自己拼连接（`host="localhost", password=""`，
    第六十五批实录）。两个方向都错：
      · `WARDEN_TEST_PG_PORT` 这类旋钮对它无效 ⇒ 切「无 PG 口径」时它**还在连真库**（口径不纯）；
      · PG 不在默认端口时（远端库 / CI 换端口）它连不上 ⇒ **静默跳过**，整套照样报绿
        —— 第五十二批修过的那个形状，漏了这一个文件。

    所以把判据钉成单一事实源（`tests/test_postgres_integration.py::_pg_params` / `_pg_available`）：
    静态扫描测试目录，出现不带 `**_pg_params()` 的 psycopg 连接就红。
    """
    offenders: list[str] = []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and func.attr == "connect"
                and isinstance(func.value, ast.Name)
                and func.value.id == "psycopg"
            ):
                continue
            expands = any(
                kw.arg is None
                and isinstance(kw.value, ast.Call)
                and (getattr(kw.value.func, "id", None) or getattr(kw.value.func, "attr", None))
                == "_pg_params"
                for kw in node.keywords
            )
            if not expands:
                offenders.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}")
    assert not offenders, (
        "有测试自己拼了 PG 连接（没带 `**_pg_params()`）："
        + "、".join(offenders)
        + " —— 是否可用只能由 tests/test_postgres_integration.py::_pg_available 判（它认环境变量、"
        "含端口旋钮）。自己拼的话：切「无 PG 口径」时它还在连真库，而 PG 换端口时它会静默跳过。"
    )


def test_预热镜像那一步必须留在Pytest之前() -> None:
    """位置本身就是机制：预热步骤删了/挪到后面，求值用例就会在 CI 里静默跳过。

    这条测试钉住 `ci.yml` 里的先后顺序 —— 守卫（事后）与预热（事前）互为补充：
    预热保证「能真跑」，守卫保证「真跑了」。
    """
    lines = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8").splitlines()
    # 只认**行内代码**（`#` 之后是注释）：注释里会引用这些文件名，不能拿注释当证据。
    markers = {
        "pull": "docker pull prom/prometheus:",
        "junit": "--junitxml=/tmp/pytest-junit.xml",
        "guard": "check_skipped_tests.py",
    }
    marks: dict[str, int] = {}
    for index, raw in enumerate(lines, start=1):
        code = raw.split("#", 1)[0]
        for key, needle in markers.items():
            if needle in code:
                marks.setdefault(key, index)
    assert set(marks) == set(markers), f"ci.yml 里少了某一步：{marks}"
    assert marks["pull"] < marks["junit"], (
        f"预热镜像那一步必须排在产出 junit 的 Pytest 之前（现在 pull 在 {marks['pull']} 行、"
        f"pytest 在 {marks['junit']} 行）——否则求值用例照样静默跳过"
    )
    assert marks["junit"] < marks["guard"], (
        "签字守卫必须排在产出 junit 的那一步之后，否则它没有数据可验"
    )


def test_ts客户端依赖必须在Pytest之前() -> None:
    """少了这一步，`tests/test_mcp.py` 的 4 条真实连接用例**每天在 CI 里跳过**。

    第六十五批实录：本机装了 `ts/mcp-client/node_modules`，所以这 4 条在本机是跑的，
    而 CI 从来没装过 ⇒ 它们每天在 CI 日志里以「ts 客户端依赖未安装」跳着、CI 每天绿。
    这正是「跳过即静默」的通用形状：**前置条件没在 workflow 里备好**。
    """
    lines = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8").splitlines()
    markers = {
        "npm": "working-directory: ts/mcp-client",
        "junit": "--junitxml=/tmp/pytest-junit.xml",
    }
    marks: dict[str, int] = {}
    for index, raw in enumerate(lines, start=1):
        code = raw.split("#", 1)[0]
        for key, needle in markers.items():
            if needle in code:
                marks.setdefault(key, index)
    assert set(marks) == set(markers), f"ci.yml 里少了某一步：{marks}"
    assert marks["npm"] < marks["junit"], (
        f"安装 ts 客户端依赖必须排在 Pytest 之前（现在 npm 在 {marks['npm']} 行、"
        f"pytest 在 {marks['junit']} 行）——否则那 4 条真实 MCP 集成用例在 CI 里永远静默跳过"
    )


def test_CI必须上传junit且Pytest红了也要传() -> None:
    """守卫要守的是 **CI**，而跳过集按环境变（本机 4 条 / CI 8 条（第六十五批实测））。

    没有这份产物，本地复核就只能拿自己的 junit 去核台账 —— 那正是本批踩的坑：
    台账是用「我机器上的观察」建的，而它要守的环境是 CI。`if: always()` 保证
    Pytest 红的时候（恰好最需要核台账的时刻）产物依然在。
    """
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "actions/upload-artifact@" in text, "ci.yml 里没有上传 junit 的步骤"
    # 取**这一整个步骤**：从它的 `- name:` 到下一条 `- name:` 为止。
    # （我第一版是从 `actions/upload-artifact@` 往后切，结果丢了写在 `uses:` **前面**的
    #   `if: always()` —— 检查自己的窗口也得对着真文件看一眼。）
    _, _, after_first = text.partition("上传 junit")
    block = after_first.split("- name:", 1)[0]
    assert "if: always()" in block, (
        "上传 junit 必须带 `if: always()`：Pytest 失败时最需要这份 junit"
        "（守卫要按 CI 的真数据核台账，而 CI 才是它要守的环境）"
    )
    assert "name: pytest-junit" in block and "/tmp/pytest-junit.xml" in block, (
        "上传的必须是 Pytest 产出那份 junit（名字/路径对不上就等于没传）"
    )
    assert "if-no-files-found: error" in block, "junit 没了就大声红——不静默降级"
