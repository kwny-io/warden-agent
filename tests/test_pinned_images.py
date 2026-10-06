"""守卫 `scripts/check_pinned_images.py` 的回归测试。

它守的失败模式只有一类：**镜像"看起来钉住了、其实没钉"**。

- 浮动 tag：今天拉到 A、明天拉到 B，而演练记录上写的都是"同一个环境"；
- 占位 digest（全 0 / 全 f）：语法合法、`kubectl apply` 也不拦，但拉取时
  `manifest unknown`——**Pod 从未真正起来过**；
- 变量插值：守卫只能检查字面量，`${X}` 直接把它绕过去；
- 台账条目过期 / 变成孤儿：于是"等等再钉"变成"永远不钉"。

本文件里每个用例都对应上面某一条，没有"跑一下不报错"的凑数用例。
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
GUARD = REPO_ROOT / "scripts" / "check_pinned_images.py"

# 一个“格式合法但不存在”的 digest：只测**格式与流程**，不测拉取。
# 注：故意用真实 hash 的样子——早先写成 `"1" * 64` 时被守卫判成占位值，
# 这是**守卫对了、用例错了**（64 位同一个字符的真 digest 不存在）。
DIGEST = "sha256:" + hashlib.sha256(b"pinned-images-test").hexdigest()
_COMPOSE = "services:\n  app:\n    image: {image}\n"
# 故意坏掉的清单（未闭合的流式序列）：守卫必须报红，不能静默跳过这个文件。
BROKEN_YAML = "services:\n  app:\n    image: [未闭合\n"


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GUARD), "--root", str(root)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        # 本机控制台是 cp936：不钉 UTF-8 的话脚本里的 ✅/❌ 会变成乱码，
        # 断言就等于在比乱码（脚本自己会 reconfigure，这里再钉一次）。
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )


def _tree(tmp_path: Path, image: str, *, where: str = "docker-compose.yml") -> Path:
    """搭一棵最小仓库树：一个 compose 文件，里面一个镜像引用。"""
    root = tmp_path / "repo"
    target = root / where
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_COMPOSE.format(image=image), encoding="utf-8")
    return root


def _entry(image: str, **overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "image": image,
        "added": "2026-10-05",
        "reason": "本机取不到上游 digest",
        "review_by": "2027-10-05",
    }
    entry.update(overrides)
    return entry


def _ledger(root: Path, entries: list[dict[str, object]]) -> None:
    path = root / "deploy" / "IMAGE_LEDGER.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"entries": entries}, allow_unicode=True), encoding="utf-8")


# --------------------------------------------------------------------------- #
# 0. 现状：真仓库现在是合格的（这条同时防止"守卫自己把仓库判红"的假红）
# --------------------------------------------------------------------------- #
def test_真仓库当前是合格的() -> None:
    proc = _run(REPO_ROOT)
    assert proc.returncode == 0, f"真仓库应当合格：\n{proc.stdout}\n{proc.stderr}"
    assert "处外部镜像引用" in proc.stdout, (
        "必须把扫描到多少处说出来（否则无从判断守卫是否真在干活）"
    )
    assert "✅" in proc.stdout


# --------------------------------------------------------------------------- #
# 1. 浮动 tag：最常见、也最容易被当成"已经钉了"
# --------------------------------------------------------------------------- #
def test_浮动tag会红并说清修法(tmp_path: Path) -> None:
    proc = _run(_tree(tmp_path, "nginx:1.27"))
    assert proc.returncode == 1, f"浮动 tag 必须红：\n{proc.stdout}"
    assert "nginx:1.27" in proc.stdout, "要点名是哪个引用"
    assert "台账" in proc.stdout, "要告诉人两条修法：钉 digest，或进台账"
    assert "sha256:<digest>" in proc.stdout, "钉 digest 的形式要写出来"
    assert "台账文件还不存在" in proc.stdout, "树里没有台账时要顺带说清这一点"


def test_deploy下的清单同样在范围内(tmp_path: Path) -> None:
    root = _tree(tmp_path, "nginx:1.27", where="deploy/observability/docker-compose.yml")
    proc = _run(root)
    assert proc.returncode == 1, f"deploy/** 里的清单也必须被扫到：\n{proc.stdout}"
    assert "deploy/observability/docker-compose.yml" in proc.stdout, "报的是相对路径，要能直接去改"


# --------------------------------------------------------------------------- #
# 2. 钉住的形式：只有 `<ref>@sha256:<64 位>` 才算钉住
# --------------------------------------------------------------------------- #
def test_钉住真digest就合格且不需要台账文件(tmp_path: Path) -> None:
    root = _tree(tmp_path, f"nginx:1.27@{DIGEST}")
    proc = _run(root)
    # 台账文件不存在不该是红：一个全钉住的仓库根本不需要台账，
    # 那种红是**假红**（本仓口径：假红比漏检更坏，它教人忽略门禁）。
    assert proc.returncode == 0, f"钉住就应当合格，且不要求台账存在：\n{proc.stdout}\n{proc.stderr}"
    assert "钉住 1" in proc.stdout


def test_占位digest不算钉住(tmp_path: Path) -> None:
    root = _tree(tmp_path, "nginx:1.27@sha256:" + "0" * 64)
    proc = _run(root)
    assert proc.returncode == 1, f"全 0 的 digest 必须红：\n{proc.stdout}"
    assert "64 位全都相同" in proc.stdout, "要说清它为什么不算钉住"
    assert "manifest unknown" in proc.stdout, "要说清它到底怎么坏的，而不是只说格式不对"


def test_全f的占位digest同样不算(tmp_path: Path) -> None:
    root = _tree(tmp_path, "nginx:1.27@sha256:" + "f" * 64)
    proc = _run(root)
    assert proc.returncode == 1, f"全 f 的 digest 也要红：\n{proc.stdout}"


def test_全1的digest也当占位(tmp_path: Path) -> None:
    """防止有人把规则收窄成“只怼全 0/全 f”：64 位同一个字符**都不可能是真 digest**。"""
    root = _tree(tmp_path, "nginx:1.27@sha256:" + "1" * 64)
    proc = _run(root)
    assert proc.returncode == 1, f"全 1 的 digest 也是占位值：\n{proc.stdout}"


def test_digest长度不对会红(tmp_path: Path) -> None:
    root = _tree(tmp_path, "nginx:1.27@sha256:abc123")
    proc = _run(root)
    assert proc.returncode == 1, f"短 digest 不是合法摘要：\n{proc.stdout}"
    assert "64 位" in proc.stdout


# --------------------------------------------------------------------------- #
# 3. 变量插值：守卫只能检查字面量，所以它必须**拒绝**看不见的引用
# --------------------------------------------------------------------------- #
def test_变量拼镜像引用会红(tmp_path: Path) -> None:
    proc = _run(_tree(tmp_path, "${REGISTRY}/app:1.0"))
    assert proc.returncode == 1, f"变量引用等于绕开守卫，必须红：\n{proc.stdout}"
    assert "变量" in proc.stdout


# --------------------------------------------------------------------------- #
# 4. 台账：能抵，但抵得有据、有时限、有清理
# --------------------------------------------------------------------------- #
def test_台账覆盖后合格(tmp_path: Path) -> None:
    root = _tree(tmp_path, "nginx:1.27")
    _ledger(root, [_entry("nginx:1.27")])
    proc = _run(root)
    assert proc.returncode == 0, f"登记了就应当合格：\n{proc.stdout}\n{proc.stderr}"
    assert "台账 1" in proc.stdout


def test_过期台账条目会红(tmp_path: Path) -> None:
    root = _tree(tmp_path, "nginx:1.27")
    _ledger(root, [_entry("nginx:1.27", added="2020-01-01", review_by="2020-06-01")])
    proc = _run(root)
    assert proc.returncode == 1, f"复核期限过了必须红（否则台账就是永久豁免）：\n{proc.stdout}"
    assert "已过" in proc.stdout
    assert "钉住" in proc.stdout, "要给出出路：要么钉住，要么重新评估并更新日期"


def test_孤儿台账条目会红(tmp_path: Path) -> None:
    root = _tree(tmp_path, f"nginx:1.27@{DIGEST}")
    _ledger(root, [_entry("nginx:1.27")])
    proc = _run(root)
    assert proc.returncode == 1, f"台账里已经没人用的条目要删，否则台账会腐化：\n{proc.stdout}"
    assert "没有任何清单引用" in proc.stdout


def test_台账缺字段会红(tmp_path: Path) -> None:
    root = _tree(tmp_path, "nginx:1.27")
    entry = _entry("nginx:1.27")
    del entry["reason"]
    _ledger(root, [entry])
    proc = _run(root)
    assert proc.returncode == 1, f"缺字段的台账条目不算登记：\n{proc.stdout}"
    assert "缺字段" in proc.stdout
    assert "reason" in proc.stdout


def test_台账重复登记会红(tmp_path: Path) -> None:
    root = _tree(tmp_path, "nginx:1.27")
    _ledger(root, [_entry("nginx:1.27"), _entry("nginx:1.27")])
    proc = _run(root)
    assert proc.returncode == 1, f"重复登记说明有人在盲改台账：\n{proc.stdout}"
    assert "重复登记" in proc.stdout


# --------------------------------------------------------------------------- #
# 5. 读不懂的清单必须红，不能静默跳过（静默跳过 = 守卫漏检）
# --------------------------------------------------------------------------- #
def test_坏YAML会红而不是静默跳过(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "docker-compose.yml").write_text(BROKEN_YAML, encoding="utf-8")
    proc = _run(root)
    assert proc.returncode == 1, f"解析不了的清单必须红：\n{proc.stdout}"
    assert "docker-compose.yml" in proc.stdout, "要点名哪个文件读不了"
