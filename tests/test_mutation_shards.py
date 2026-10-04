"""变异测试分片表（`scripts/mutation_shards.py`）的单测。

为什么值得单独测：分片表是"哪一片变异什么、跑哪些测试、门槛多少"的**唯一来源**，
而且脚本会**改写 pyproject.toml**（把 `[tool.mutmut]` 换成当前分片）——这类
"自动化改写仓库配置"的东西一旦错了，症状不是崩，而是**静默跑错范围**：
  * 表格与配置不一致 → CI 跑的和人以为的不是一回事（最隐蔽的失真）；
  * 分片之间共享同一个实现文件 → 同一段逻辑被两套门槛各判一次，两边都不准；
  * 门槛 ≥ 基线 → 门禁天天红、最后被人关掉；门槛比基线低太多 → 等于没有门禁；
  * 列出不存在的文件 → mutmut 空转（脚本自己会用退出码 4 报出来，但那时已经跑完一轮了）；
  * CI 的 matrix 名单和表格不一致 → 某一片**永远不被跑**，而仪表盘上看不出缺口。

本文件就是把这些全部按成"改坏了立刻红"。
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def _load_shards() -> Any:
    """按路径加载 scripts/mutation_shards.py（scripts/ 不是包，不能直接 import）。"""
    spec = importlib.util.spec_from_file_location(
        "warden_mutation_shards", _ROOT / "scripts" / "mutation_shards.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["warden_mutation_shards"] = module
    spec.loader.exec_module(module)
    return module


shards = _load_shards()


# --------------------------------------------------------------- 表格自身


def test_pyproject_的_tool_mutmut_与分片表一致() -> None:
    """配置漂移守卫：pyproject 里那一段必须**逐字节**等于表里默认分片的渲染结果。"""
    assert shards.main(["verify-config"]) == 0


def test_分片名唯一且默认分片存在() -> None:
    names = [s.name for s in shards.SHARDS]
    assert len(names) == len(set(names)), f"分片名重复：{names}"
    assert shards.DEFAULT_SHARD in shards.BY_NAME


def test_每片的实现文件与测试文件都真实存在() -> None:
    for shard in shards.SHARDS:
        for path in (*shard.modules, *shard.tests):
            assert (_ROOT / path).is_file(), f"{shard.name} 指向了不存在的文件：{path}"


def test_每片都至少有一个实现和一个测试文件() -> None:
    """空片会让 mutmut 空转（门禁的退出码 4 就是为这种情况准备的）。"""
    for shard in shards.SHARDS:
        assert shard.modules, f"{shard.name} 没有实现文件"
        assert shard.tests, f"{shard.name} 没有测试文件"


def test_不同分片不共享同一个实现文件() -> None:
    """同一个文件被两片变异 ⇒ 同一段逻辑被两套门槛各判一次，分数互相污染。"""
    seen: dict[str, str] = {}
    for shard in shards.SHARDS:
        for module in shard.modules:
            assert module not in seen, f"{module} 同时属于 {seen.get(module)} 与 {shard.name}"
            seen[module] = shard.name


def test_门槛与基线成对出现且留白合理() -> None:
    """门槛必须**低于**基线（否则天天红），又不能低太多（否则形同虚设）。"""
    for shard in shards.SHARDS:
        if shard.min_score is None or shard.baseline is None:
            # 未实测：两个必须同时缺，不能只有一个
            assert shard.min_score is None and shard.baseline is None, (
                f"{shard.name} 的门槛与基线必须成对出现"
            )
            continue
        gap = shard.baseline - shard.min_score
        assert 0 < gap <= 5.0, f"{shard.name} 的门槛留白 {gap} 不合理（应 0<gap<=5）"
        assert 50.0 <= shard.min_score < 100.0, f"{shard.name} 门槛 {shard.min_score} 不合理"


def test_未实测门槛的分片必须在文档里写明() -> None:
    """没有实测基线的分片必须写在 docs/mutation.md 里——否则读者会把"跑了"当成"卡住了"。"""
    doc = (_ROOT / "docs" / "mutation.md").read_text(encoding="utf-8")
    for shard in shards.SHARDS:
        if shard.min_score is None:
            assert f"`{shard.name}`" in doc, f"{shard.name} 尚未实测，但 docs/mutation.md 没写"


# --------------------------------------------------------------- 渲染与还原


def _copy_pyproject(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    """把真 pyproject 拷到 tmp 并把模块的路径指向副本：测试不许改仓库里的真文件。

    拷贝必须走**字节**：`write_text()` 在 Windows 上会把 `\n` 翻成 `\r\n`，
    于是"往返后字节还原"这类断言会先被这个拷贝本身弄红（假红）。
    """
    real = shards.PYPROJECT
    original = real.read_text(encoding="utf-8")
    copy = tmp_path / "pyproject.toml"
    copy.write_bytes(real.read_bytes())
    monkeypatch.setattr(shards, "PYPROJECT", copy)
    return copy, original


def test_渲染一片之后配置就是那一片(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    copy, _ = _copy_pyproject(tmp_path, monkeypatch)
    assert shards.write_shard(shards.BY_NAME["ratelimit"]) is True
    text = copy.read_text(encoding="utf-8")
    assert shards.current_block() == shards.render_block(shards.BY_NAME["ratelimit"])
    assert "src/warden_agent/web/ratelimit.py" in text
    # 别把别的分片的模块留在里面（换片必须换干净）
    assert "src/warden_agent/policy/policy.py" not in text


def test_渲染可以逐字节还原(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """这是"跑完一定还原"的语义保证：还原后文件与原始**字节**完全相同。

    必须比字节而不是只比文本：`read_text()` 会把 CRLF 归一化成 `\n`，所以"文本相等"
    对**换行符被改写**完全看不见——而 `write-shard` 改的是仓库里的真文件。
    """
    copy, original = _copy_pyproject(tmp_path, monkeypatch)
    original_bytes = copy.read_bytes()
    shards.write_shard(shards.BY_NAME["concurrency"])
    shards.write_shard(shards.BY_NAME[shards.DEFAULT_SHARD])
    assert copy.read_bytes() == original_bytes, "往返后字节不等（换行符或空行被改？）"
    assert copy.read_text(encoding="utf-8") == original
    assert b"\r\n" not in copy.read_bytes(), "写入引入了 CRLF"


def test_同一片重复渲染不写文件(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    copy, _ = _copy_pyproject(tmp_path, monkeypatch)
    assert shards.write_shard(shards.BY_NAME[shards.DEFAULT_SHARD]) is False
    assert shards.current_block() == shards.render_block(shards.BY_NAME[shards.DEFAULT_SHARD])
    assert copy.exists()


def test_段落后面那行空行不会被重写吃掉(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`[tool.mutmut]` 后面还有别的表头时，中间那行空行必须原样留着。

    为什么值得单测：`write-shard` 会**改写仓库里的 pyproject.toml**，而它把"段落"
    定义成"到下一个顶层表头为止"——空行若被算进段落，一次渲染就会把那个空行
    **静默删掉**（症状不是崩，是文件已经被改过一轮、`test_渲染可以逐字节还原` 才变红；
    而 `write_shard` 会误以为“每次都需要写”，把那个空行反复吃一遍）。
    """
    copy, original = _copy_pyproject(tmp_path, monkeypatch)
    text = original.rstrip("\n") + "\n\n[probe.blank_line_guard]\nx = 1\n"
    copy.write_text(text, encoding="utf-8")
    shards.write_shard(shards.BY_NAME["concurrency"])
    shards.write_shard(shards.BY_NAME[shards.DEFAULT_SHARD])
    assert copy.read_text(encoding="utf-8") == text, "那行空行被吃掉了"
    assert not shards.current_block().endswith("\n\n"), "空行不该被算进 mutmut 段落"


def test_render_check_能发现漂移(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """鉴别力：把配置改坏，`render --check` 必须报不一致（退出码 1）。"""
    copy, _ = _copy_pyproject(tmp_path, monkeypatch)
    broken = copy.read_text(encoding="utf-8").replace(
        '"src/warden_agent/policy/policy.py",', '"src/warden_agent/policy/不存在.py",'
    )
    copy.write_text(broken, encoding="utf-8")
    assert shards.main(["render", shards.DEFAULT_SHARD, "--check"]) == 1
    assert shards.main(["verify-config"]) == 1


def test_git引号路径被还原成真实路径() -> None:
    """非 ASCII 路径：git 默认会 C 引号化，直接比字符串会**静默**匹配不到分片。

    这里不手写八进制转义，而是用字节现算一份引号化结果——等于让测试自己也能被发现写错。
    """
    name = "docs/复核索引.md"
    encoded = "".join(
        chr(b) if 32 <= b < 127 and chr(b) not in '\\"' else f"\\{b:03o}"
        for b in name.encode("utf-8")
    )
    assert shards._unquote_git_path(f'"{encoded}"') == name
    # 没引号（`core.quotepath=false` 的产物）原样通过
    assert shards._unquote_git_path(name) == name
    # 带控制字符的（git 写成 \\t）不能吞掉
    assert shards._unquote_git_path('"a\\tb.py"') == "a\tb.py"


def test_改动路径能映射到分片() -> None:
    """端到端判据：真源文件必须能命中它所在的分片（命不中=分片静默不跑）。"""
    shard_of = {p: shards.shards_for_paths([p]) for p in shards.BY_NAME["patch-guard"].modules[:3]}
    assert all(v == {"patch-guard"} for v in shard_of.values()), shard_of
    # 与分片无关的文件不应把分片拉起来（否则分片变成“永远重跑”）
    assert shards.shards_for_paths(["README.md", "web/src/App.tsx"]) == set()


def test_split_pyproject_段落定位正确(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, _ = _copy_pyproject(tmp_path, monkeypatch)
    text = shards.PYPROJECT.read_text(encoding="utf-8")
    before, block, after = shards.split_pyproject(text)
    assert block.startswith("[tool.mutmut]\n")
    assert block.endswith("\n")
    # 段落自己不得以空行结尾：否则一次 write-shard 会把那行空行从 pyproject.toml 里删掉
    # （“到下一个表头为止”的朴素实现就是这么静默改文件的，见 test_段落后面那行空行不会被重写吃掉）
    assert not block.endswith("\n\n"), "空行被算进段落了"
    # 前面不能还有一个独立的 [tool.mutmut] 表头（注释里提到这个名字是允许的）
    assert not [ln for ln in before.splitlines() if ln.strip() == "[tool.mutmut]"]
    # 段落必须止于下一个顶层表头（不能把后面的配置吞进来）
    assert after.lstrip("\n") == "" or after.lstrip("\n").startswith("[")
    # 三段拼回去必须逐字节等于原文——整个工具（改写 pyproject）就靠这条性质
    assert before + block + after == text


def test_split_pyproject_缺段落会抛错(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    copy, _ = _copy_pyproject(tmp_path, monkeypatch)
    copy.write_text("[tool.ruff]\nline-length = 100\n", encoding="utf-8")
    with pytest.raises(KeyError):
        shards.split_pyproject(copy.read_text(encoding="utf-8"))


def test_找不到分片时返回用法错误码() -> None:
    assert shards.main(["min-score", "根本没有这一片"]) == 2
    assert shards.main(["render", "根本没有这一片"]) == 2


# --------------------------------------------------------------- 改动 → 分片


def test_改动映射命中实现文件与测试文件() -> None:
    assert shards.shards_for_paths(["src/warden_agent/web/ratelimit.py"]) == {"ratelimit"}
    assert shards.shards_for_paths(["tests/test_run_lock.py"]) == {"concurrency"}
    assert shards.shards_for_paths(["src/warden_agent/core/redaction.py"]) == {"core-security"}
    # 无关文件不牵连任何分片
    assert shards.shards_for_paths(["README.md", "src/warden_agent/web/server.py"]) == set()


def test_不在任何分片内的改动会被单独列出() -> None:
    """改了共享代码时不能假装"只影响一片"——这类文件要单独提醒人。"""
    assert shards.untracked_by_shards(["src/warden_agent/web/server.py"]) == [
        "src/warden_agent/web/server.py"
    ]
    assert shards.untracked_by_shards(["src/warden_agent/web/ratelimit.py"]) == []
    assert shards.untracked_by_shards(["docs/mutation.md"]) == []  # 非 src/tests 不算


# --------------------------------------------------------------- 与 CI / 脚本的一致性


def test_matrix_json_与分片表一致() -> None:
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        assert shards.main(["matrix"]) == 0
    assert json.loads(buf.getvalue()) == [{"shard": s.name} for s in shards.SHARDS]


def test_ci_的_matrix_名单与分片表一致() -> None:
    """CI 里少写一片 = 那一片**永远不被跑**，而且仪表盘上看不出缺口。"""
    text = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    match = re.search(r"matrix:\s*\n\s*shard:\s*\n((?:\s*-\s*\S+\n)+)", text)
    assert match is not None, "ci.yml 里找不到 matrix.shard 列表"
    in_ci = re.findall(r"-\s*(\S+)", match.group(1))
    assert set(in_ci) == set(shards.BY_NAME), (
        f"ci.yml 的分片名单 {in_ci} 与分片表 {sorted(shards.BY_NAME)} 不一致"
    )
    # 每个 job 必须跑自己那一片（写死成默认分片就白分片了）
    assert "--shard=${{ matrix.shard }}" in text


def test_脚本在变异之前清状态且校验统计文件是新的() -> None:
    """两条反假绿措施必须留在脚本里：

    1. 跑之前清 mutmut 的"已评估"状态——不清就会拿**上一个分片的数字**交差
       （实测撞到过：两片都报 242/251、results 文件 0 字节）；
    2. 跑完检查统计文件被本次重写——否则门禁会拿着别人的成绩单放行。
    """
    text = (_ROOT / "scripts" / "mutation.sh").read_text(encoding="utf-8")
    reset = re.search(r"^\s+reset_mutation_state$", text, re.M)
    run = text.index("mutmut run || true")
    assert reset is not None, "没找到 reset_mutation_state 的调用"
    assert reset.start() < run, "清状态必须在 mutmut run 之前"
    assert "统计文件未被本次运行重写" in text
    assert "mutants/mutmut-stats.json" in text  # 状态文件必须真的在清理清单里


# ----------------------------------------------------- 命令行自己的健壮性


def test_show_子命令在窄编码的_stdout_下也不能崩() -> None:
    """`show` 渲染出的段落里有 `⇒`，而 Windows 管道重定向时 stdout 默认是 GBK(cp936)。

    实测（2026-10-01，Windows）：
        .venv/Scripts/python.exe scripts/mutation_shards.py show patch-guard > 文件
    直接 `UnicodeEncodeError: 'gbk' codec can't encode character '\u21d2'` + 退出码 1——
    一个纯打印动作把整个子命令废了。这里用 `PYTHONIOENCODING=gbk` 复现，与平台无关。
    """
    env = dict(os.environ, PYTHONIOENCODING="gbk")
    proc = subprocess.run(
        [sys.executable, str(_ROOT / "scripts" / "mutation_shards.py"), "show", "patch-guard"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )
    assert proc.returncode == 0, proc.stderr
    # 真的把渲染内容打出来了（而不是“什么都没打所以没崩”）
    assert "[tool.mutmut]" in proc.stdout
    assert "⇒" in proc.stdout
