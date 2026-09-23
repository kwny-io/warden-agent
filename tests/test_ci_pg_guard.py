"""守卫 `scripts/check_pg_tests_ran.py` 的回归测试。

它本身就是 CI 门禁的一环（见 .github/workflows/ci.yml 的"防静默跳过"步骤）：
这里锁住它的两条关键行为——**按标记自动发现 PG 文件**（不靠手写清单）与
**只看 PG 文件的跳过**（别的文件跳过不误伤）。

注意：本文件本身不能被守卫误识别为 PG 依赖文件，所以**不出现** PG 识别标记的
字面量（环境变量名用拼接构造）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_guard() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_pg_tests_ran", ROOT / "scripts" / "check_pg_tests_ran.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_GUARD = _load_guard()
_PG_ENV_KEY = "WARDEN_TEST_" + "PG_HOST"   # 拼接构造：不让本文件出现该字面量


def _junit(path: Path, *cases: str) -> Path:
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite>'
        + "".join(cases)
        + "</testsuite></testsuites>",
        encoding="utf-8",
    )
    return path


def test_按标记自动发现PG依赖文件() -> None:
    """发现必须覆盖所有带 PG 依赖标记的文件——漏一个就等于留一个静默跳过口。"""
    names = {p.name for p in _GUARD.discover_pg_test_files()}
    expected = {
        "test_postgres_integration.py",
        "test_postgres_audit_memory.py",
        "test_notify_event_bus.py",
        "test_store_interface.py",
        "test_backup_retention.py",   # pg_dump 端到端
    }
    assert expected <= names, f"漏发现：{expected - names}"
    # 静态契约测试不连库，不应被当成 PG 集成测试
    assert "test_postgres_contract.py" not in names


def test_非CI环境直接放行() -> None:
    """本地没有 PG 时测试本就该跳过；守卫不能把本地跑红。"""
    assert _GUARD.in_ci({}) is False
    assert _GUARD.main([]) == 0


def test_junit里PG用例零跳过则通过(tmp_path: Path) -> None:
    xml = _junit(
        tmp_path / "ok.xml",
        '<testcase classname="tests.test_postgres_integration" name="t1" />',
    )
    assert _GUARD.check_junit(xml, _GUARD.discover_pg_test_files()) == []


def test_junit里PG用例被跳过则判失败(tmp_path: Path) -> None:
    xml = _junit(
        tmp_path / "skip.xml",
        '<testcase classname="tests.test_postgres_integration" name="t1">'
        '<skipped message="需要可用的 PostgreSQL 服务器才会运行" /></testcase>',
    )
    problems = _GUARD.check_junit(xml, _GUARD.discover_pg_test_files())
    assert problems and "被跳过" in problems[0]


def test_只跑PG文件之外跳过不报错(tmp_path: Path) -> None:
    """别的文件跳过（MCP/前端等）不该被这条守卫误伤。"""
    xml = _junit(
        tmp_path / "other.xml",
        '<testcase classname="tests.test_mcp" name="t1">'
        '<skipped message="需要 node" /></testcase>'
        '<testcase classname="tests.test_postgres_integration" name="t2" />',
    )
    assert _GUARD.check_junit(xml, _GUARD.discover_pg_test_files()) == []


def test_junit里没有PG用例也判失败(tmp_path: Path) -> None:
    """用例一个都没收集到 = 识别标记失效或跑错文件，必须报出来。"""
    xml = _junit(
        tmp_path / "empty.xml",
        '<testcase classname="tests.test_run_lease" name="t1" />',
    )
    problems = _GUARD.check_junit(xml, _GUARD.discover_pg_test_files())
    assert problems and "没有任何 PG 相关测试" in problems[0]


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"CI": "true"}, True),
        ({"CI": "1"}, True),
        ({_PG_ENV_KEY: "localhost"}, True),
        ({}, False),
        ({"CI": "false"}, False),
    ],
)
def test_in_ci判定(env: dict[str, str], expected: bool) -> None:
    assert _GUARD.in_ci(env) is expected
