"""`scripts/freeze_receipt.py` 的判据：回执里的数字必须**算出来**，不能是作者给的。

为什么值这一批（而不是"顺手加个脚本"）：
    `tests/test_delivery_receipts.py` 头部自己承认的缺口是「回执住在仓库外，**可以在事后补写**」。
    第七十八批第九处的现场更狠：回执写着 tree `e1ddf6a3…`，实际交付的是 `83891221…`
    ——回执指到了**被作废的那次冻结**，而语法、文件、提交全都在，任何文本守卫都抓不到。
    所以这一层只认两件事：**谁算的这个数**、**算的时候树有没有变**。

这里测的是纯函数与拒绝路径（真跑 pytest 的部分由 `--run` 覆盖，不在单测里跑第二遍全套）。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "freeze_receipt", ROOT / "scripts" / "freeze_receipt.py"
)
assert _SPEC is not None and _SPEC.loader is not None
fr = importlib.util.module_from_spec(_SPEC)
sys.modules["freeze_receipt"] = fr
_SPEC.loader.exec_module(fr)

TREE = "a" * 40
FULL_SHA = "c299ef6d3c6f0ddbe17804d0a2dc6defc89b67c3"


def receipt(**over: object) -> dict[str, object]:
    kw: dict[str, object] = {
        "sha": FULL_SHA,
        "tree": TREE,
        "phase": "f1",
        "log_name": "freeze-c299ef6d3c6f.log",
        "log_sha256": "b" * 64,
        "counts": {"tests": 2184, "failures": 0, "errors": 0, "skipped": 44, "passed": 2140},
        "rc": 0,
        "coverage": 86.99,
        "tree_before": TREE,
        "tree_after": TREE,
        "shape_before": fr.EMPTY_SHAPE,
        "shape_after": fr.EMPTY_SHAPE,
        "keys_before": "0123456789abcdef",
        "keys_after": "0123456789abcdef",
    }
    kw.update(over)
    return fr.build_receipt(**kw)  # type: ignore[arg-type]


def test_三对账都真才绿() -> None:
    d = receipt()
    assert d["verdict"] == "green"
    assert d["freeze"]["tree_same"] is True
    assert d["freeze"]["shape_same"] is True
    assert d["freeze"]["keys_same"] is True
    assert d["pytest"]["passed"] == 2140, "passed 必须是算出来的，不是作者写的"


def test_跑完之后树变了就红() -> None:
    d = receipt(tree_after="c" * 40)
    assert d["freeze"]["tree_same"] is False
    assert d["verdict"] == "red", "冻结期间树变了，回执不许声称绿"


def test_工作区变脏也算树变了() -> None:
    d = receipt(shape_after="f" * 16)
    assert d["freeze"]["shape_same"] is False
    assert d["verdict"] == "red"


def test_对外形状变了就红() -> None:
    d = receipt(keys_after="fedcba9876543210")
    assert d["freeze"]["keys_same"] is False
    assert d["verdict"] == "red", "路由/OpenAPI 变了 ⇒ 回执背的书不是这个形状"


@pytest.mark.parametrize(
    ("over", "why"),
    [
        ({"counts": {"tests": 1, "failures": 1, "errors": 0, "skipped": 0, "passed": 0}}, "失败"),
        ({"counts": {"tests": 1, "failures": 0, "errors": 1, "skipped": 0, "passed": 0}}, "错误"),
        ({"rc": 1}, "退出码"),
        ({"rc": 5}, "没收集到用例"),
    ],
)
def test_跑红了就不许写绿回执(over: dict[str, object], why: str) -> None:
    assert receipt(**over)["verdict"] == "red", why


def test_覆盖率抽不到就是抽不到() -> None:
    assert fr.COVERAGE_RE.search("Total coverage: 86.99%") is not None
    assert fr.COVERAGE_RE.search("no coverage report here") is None, "抽不到就是抽不到"


def test_只支持_run_模式(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    """不接受「拿现成 junit 生成回执」——那种模式等于把数字交回给作者。"""
    assert fr.main([]) == 2
    assert "只支持 --run" in capsys.readouterr().err


def test_工作区脏就拒绝跑(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(fr, "_git", lambda *a: " M src/x.py")
    monkeypatch.setattr(fr, "_is_tracked", lambda rel: True)
    assert fr.main(["--run"]) == 2
    assert "工作区不干净" in capsys.readouterr().err


def test_未跟踪文件必须被写进回执(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--allow-untracked` 不是静默放过：那个文件名必须落进 receipt。"""
    d = fr.build_receipt(
        sha=FULL_SHA,
        tree=TREE,
        phase="f1",
        log_name="freeze-c299ef6d3c6f.log",
        log_sha256="b" * 64,
        counts={"tests": 1, "failures": 0, "errors": 0, "skipped": 0, "passed": 1},
        rc=0,
        coverage=86.99,
        tree_before=TREE,
        tree_after=TREE,
        shape_before=fr.EMPTY_SHAPE,
        shape_after=fr.EMPTY_SHAPE,
        keys_before="k",
        keys_after="k",
        workspace_untracked=["scripts/freeze_receipt.py"],
    )
    assert d["freeze"]["workspace_untracked"] == ["scripts/freeze_receipt.py"]  # type: ignore[index]


def test_未跟踪文件不会被当成已跟踪() -> None:
    """这条是**真跑出来的教训**：`git ls-files --error-unmatch <未跟踪文件>` 返回 1，
    而 `_git()` 带 `check=True` ⇒ 第一次真跑直接 `CalledProcessError` 崩在
    `--allow-untracked` 分支上。
    判据必须能在「文件确实未跟踪」时返回 False，而不是抛。
    """
    assert fr._is_tracked("CHANGELOG.md") is True
    assert fr._is_tracked("scripts/__no_such_tool__.py") is False
    assert fr._is_tracked("") is False


def test_跑的是_CI_同一组参数() -> None:
    """回执背的书必须是 **CI 会重复的那一次**：`--cov` 不在就等于覆盖率门禁没跑。

    实测教训：第一次真跑时我用了 `-q tests`，`--cov` 没带 ⇒ 输出里没有 `Total coverage:`
    ⇒ 脚本按设计拒绝写回执（不猜数字）。这正说明「抽不到就拒绝」比「抽不到就填上一次的值」值钱。
    """
    flags = list(fr.CI_PYTEST_FLAGS)
    assert "--cov=warden_agent" in flags, "少了 --cov ⇒ 覆盖率门禁不会执行"
    assert "--junitxml" not in flags, "junit 路径每次不同，不能写死在常量里"
    d = receipt()
    assert d["pytest"]["args"] == flags


def test_回执里的_sha_必须是_40_位全_sha() -> None:
    """实测踩过：回执写 12 位前缀，对账判据按 40 位全 sha 去找 ⇒ 被判成「那份回执不在 receipts/」。

    同一个对象写成前缀还是全写，对人没区别，对判据是「找得到 / 找不到」。所以：
    ① 生产者必须写全 sha；② 仓里**已有的每一份**回执都不许留前缀（否则判据只在下次复发时才有牙）。
    """
    import re

    full = re.compile(r"^[0-9a-f]{40}$")
    built = receipt()
    assert full.match(str(built["sha"])), f"生产者写的不是全 sha：{built['sha']!r}"
    assert full.match(str(built["tree"])), f"tree 不是全 sha：{built['tree']!r}"

    bad = []
    for p in sorted((ROOT / "receipts").glob("*.yaml")):
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
        sha = str(d.get("sha") or "")
        if not full.match(sha):
            bad.append(f"{p.name}: sha={sha!r} 不是 40 位全 sha")
        elif not sha.startswith(p.stem):
            bad.append(f"{p.name}: 文件名前缀与回执 sha 对不上（{sha[:12]} vs {p.stem}）")
    assert not bad, "回执里的 sha 有前缀或对不上文件名：\n  " + "\n  ".join(bad)
