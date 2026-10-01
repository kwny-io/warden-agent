"""变异分数门禁的单测（`scripts/mutation_gate.py`）。

为什么值得单独测：这个脚本的输出**直接决定 CI 红绿**，而它自己有四种"不通过"，
必须彼此可区分——

| 退出码 | 含义 | 维护者该做什么 |
| --- | --- | --- |
| 1 | 分数低于门槛 | 补测试，或说明这段逻辑为什么测不了 |
| 2 | 统计文件不存在 / 用法错 | 检查为什么没产出统计文件 |
| 3 | 统计文件读不出来 | 检查导出步骤/文件是否被截断 |
| 4 | 可评估突变体为 0 | 检查 `[tool.mutmut]` 配置是否指向真实文件 |

一旦把 2/3/4 混成"没过门禁"，维护者会去补**根本不缺**的测试——找错方向最费时间。
所以这里把每一种都固定住，包括"恰好等于门槛算过、差 0.1 个百分点不算过"。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def _load_gate() -> Any:
    """按路径加载 scripts/mutation_gate.py（scripts/ 不是包，不能直接 import）。"""
    spec = importlib.util.spec_from_file_location(
        "warden_mutation_gate", _ROOT / "scripts" / "mutation_gate.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["warden_mutation_gate"] = module
    spec.loader.exec_module(module)
    return module


gate = _load_gate()


def _stats(**over: object) -> dict[str, object]:
    """一份"正常形状"的 mutmut 统计：可评估 100 个（108 总数 − 3 跳过 − 5 无测试）。"""
    base: dict[str, object] = {
        "killed": 90,
        "survived": 8,
        "timeout": 2,
        "no_tests": 5,
        "skipped": 3,
        "total": 108,
    }
    base.update(over)
    return base


def _write(tmp_path: Path, data: object, name: str = "mutmut-cicd-stats.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_退出码与文档一致() -> None:
    assert (
        gate.OK,
        gate.BELOW_THRESHOLD,
        gate.USAGE_ERROR,
        gate.BAD_STATS,
        gate.EMPTY_MUTANTS,
        gate.NOTHING_RAN,
    ) == (0, 1, 2, 3, 4, 5)


def test_一个突变体都没跑完不能算通过(tmp_path: Path) -> None:
    """"没跑"与"跑了但分数低"是两回事，必须区分。

    实测现场：容器里 mutmut 先生成 206 个突变体，然后跑**基线测试**收统计，
    基线里一条用例在慢文件系统上有竞态而失败 → 整轮中止，206 个突变体全部停在
    "not checked"；export-cicd-stats 导出 total=206 而 killed/survived/timeout 全 0。
    当时该分片还没有实测基线，门槛是 0.0，于是 `0.0 >= 0.0` **直接放行**——假绿。
    """
    stats = _stats(killed=0, survived=0, timeout=0, no_tests=0, skipped=0, total=206)
    result = gate.evaluate(stats, 0.0)
    assert result.total == 206
    assert result.ran is False, "一个突变体都没执行完，不能声称跑过"
    # 关键：分数算出来是 0.0，而门槛也是 0.0（无基线的分片）——单单看 passed 会通过
    assert result.passed is True
    # 所以拦住它必须是 main 的单独一道门，且退出码要区别于"分数不够"与"配置空转"
    path = _write(tmp_path, stats)
    assert gate.main(["0", str(path)]) == gate.NOTHING_RAN


def test_只跑出一堆存活突变体仍算跑过() -> None:
    """反例：全部存活（杀死 0）是真的差，不是"没跑"，得报分数低而不是报运行失败。"""
    stats = _stats(killed=0, survived=100, timeout=0, no_tests=0, skipped=0, total=100)
    result = gate.evaluate(stats, 94.0)
    assert result.ran is True
    assert result.passed is False


def test_分数等于杀死数除以可评估数() -> None:
    result = gate.evaluate(_stats(), 80.0)
    assert result.tested == 100
    assert result.score == pytest.approx(90.0)
    assert result.passed is True


def test_超时不进分子_不会把分数抬上去() -> None:
    """同一个突变体从"杀死"变成"超时"，分数必须**降低**。

    改坏了会怎样：把 timeout 也算成 killed（看起来很合理——"测试挂住说明发现了问题"），
    那台慢机器或环境抖动就会凭空抬高分数，门禁失去意义。
    """
    killed = gate.evaluate(_stats(killed=90, timeout=2), 0.0)
    timed_out = gate.evaluate(_stats(killed=85, survived=8, timeout=7), 0.0)
    assert timed_out.killed == 85
    assert timed_out.score < killed.score


def test_跳过与无测试的突变体不进分母() -> None:
    """总数变大但都是"没法评估"的突变体时，分数不该被稀释。"""
    result = gate.evaluate(_stats(total=200, no_tests=95, skipped=5), 0.0)
    assert result.tested == 100
    assert result.score == pytest.approx(90.0)


def test_恰好等于门槛算通过_差一点点不算() -> None:
    assert gate.evaluate(_stats(killed=94, survived=4, timeout=2), 94.0).passed is True
    just_below = _stats(killed=939, survived=59, timeout=2, total=1008)
    assert gate.evaluate(just_below, 94.0).passed is False


def test_可评估数为零不能算通过() -> None:
    """配置写坏导致"一个突变体都没评估"时，门禁必须拦，不能静默空转。"""
    result = gate.evaluate(_stats(killed=0, total=10, skipped=4, no_tests=6), 0.0)
    assert result.tested == 0
    assert result.passed is False


def test_渲染里同时给出分数与门槛() -> None:
    text = gate.evaluate(_stats(), 94.4).render()
    assert "90/100" in text
    assert "90.0%" in text
    assert "94.4%" in text


def test_main_达标返回0(tmp_path: Path) -> None:
    assert gate.main(["80", str(_write(tmp_path, _stats()))]) == 0


def test_main_低于门槛返回1(tmp_path: Path) -> None:
    assert gate.main(["95", str(_write(tmp_path, _stats()))]) == gate.BELOW_THRESHOLD


def test_main_统计文件缺失返回2而不是抛异常(tmp_path: Path) -> None:
    """以前这里会把 FileNotFoundError 抛出去——退出码 1，和"分数不够"长得一模一样。"""
    assert gate.main(["80", str(tmp_path / "nope.json")]) == gate.USAGE_ERROR


def test_main_没参数返回2() -> None:
    assert gate.main([]) == gate.USAGE_ERROR


def test_main_最低分不是数字返回2(tmp_path: Path) -> None:
    assert gate.main(["abc", str(_write(tmp_path, _stats()))]) == gate.USAGE_ERROR


def test_main_内容不是JSON对象返回3(tmp_path: Path) -> None:
    assert gate.main(["80", str(_write(tmp_path, [1, 2, 3]))]) == gate.BAD_STATS


def test_main_JSON语法错返回3(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{ 这不是 json", encoding="utf-8")
    assert gate.main(["80", str(broken)]) == gate.BAD_STATS


def test_main_配置空转返回4(tmp_path: Path) -> None:
    empty = _write(tmp_path, {"killed": 0, "survived": 0, "timeout": 0,
                              "no_tests": 6, "skipped": 4, "total": 10})
    assert gate.main(["0", str(empty)]) == gate.EMPTY_MUTANTS
