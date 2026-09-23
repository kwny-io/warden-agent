#!/usr/bin/env python3
"""防「PG 集成测试静默跳过」的守卫：读 pytest 的 junit 报告，断言没有 PG 测试被跳过。

背景（为什么不能只靠"红/绿"）：
  PG 集成测试用 `skipif` 保护——没有数据库时跳过，保证本地/无库 CI 不红。
  副作用是：**CI 里 service 没连上时它们会静默跳过、CI 依然绿**，
  于是"在 CI 里验证 PG"这句话就是空的。

为什么不用"手写文件清单"：
  早先的守卫把 3 个文件名写死在 workflow 里。但 `skipif` 会随代码扩散——
  谁新增一个走真实 PG 的测试文件（或已有的 `test_postgres_audit_memory.py`、
  `test_backup_retention.py` 里的 pg_dump 端到端），手写清单不会自动跟上，
  又会退化成"漏一个就静默跳过"。这里改成**按识别标记自动发现** PG 依赖文件。

识别标记（新增 PG 测试时沿用其一即可被自动纳入）：
  - `_pg_available` / `_postgres_available`：收集期探测库是否可用的约定函数名；
  - `_HAS_PG`：pg_dump / pg_restore 等外部工具的可用性探测（备份端到端）；
  - 或同时出现 `WARDEN_TEST_PG_HOST` 与 `skipif`。

数据来源：**主 pytest 步骤产出的 junit 报告**（不再单独跑一遍子集——
  主测试已经跑过一次，重复跑既慢又没有新增信息）。junit 的每个 testcase 带
  `file` 属性，据此判断该用例属于哪个文件、有没有 `<skipped>` 子节点。

只在 CI 里生效：本地没有 PG 时测试本就该跳过，守卫不该把本地跑红。
  判定"在 CI"：环境变量 `CI` 为真，或设置了 `WARDEN_TEST_PG_HOST`。

用法：
    # CI：放在主 pytest 步骤之后（主步骤需带 --junitxml）
    CI=true uv run --frozen python scripts/check_pg_tests_ran.py --junitxml /tmp/pytest-junit.xml
    # 本地非 CI：直接放行
    uv run --frozen python scripts/check_pg_tests_ran.py

退出码：0 通过（或非 CI 放行）；1 有 PG 测试被跳过 / 找不到 junit / 一个 PG 用例都没收集到。
"""
from __future__ import annotations

import argparse
import os
import sys
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = ROOT / "tests"
DEFAULT_JUNIT = "/tmp/pytest-junit.xml"

# 见模块 docstring：这些是"这个文件依赖真实 PG"的约定标记。
_PG_MARKERS = ("_pg_available", "_postgres_available", "_HAS_PG")
_PG_ENV = "WARDEN_TEST_PG_HOST"


def discover_pg_test_files(tests_dir: Path = TESTS_DIR) -> list[Path]:
    """扫描 tests/，自动找出所有带 PG 依赖标记的测试文件（不手写清单）。"""
    found: list[Path] = []
    for path in sorted(tests_dir.glob("test_*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover - 读不了就跳过，不该让守卫自己崩
            continue
        is_pg = any(marker in text for marker in _PG_MARKERS) or (
            _PG_ENV in text and "skipif" in text
        )
        if is_pg:
            found.append(path)
    return found


def in_ci(env: Mapping[str, str] | None = None) -> bool:
    """是否处于"必须验证 PG 真跑了"的环境（CI 或显式配了 PG service）。"""
    env = os.environ if env is None else env
    return env.get("CI", "").strip().lower() in {"1", "true", "yes"} or bool(env.get(_PG_ENV))


def _norm(path_str: str) -> str:
    """把 junit 里的路径规范化成 posix 相对路径，便于比对。"""
    return path_str.replace("\\", "/").lstrip("./")


def check_junit(junit_path: Path, pg_files: list[Path]) -> list[str]:
    """返回问题描述列表；空列表 = 通过（PG 文件里的用例全部执行、零跳过）。"""
    expected = {_norm(str(p.relative_to(ROOT))) for p in pg_files}
    expected |= {p.name for p in pg_files}
    root = ET.parse(junit_path).getroot()

    total = 0
    skipped: list[str] = []
    for case in root.iter("testcase"):
        file_attr = _norm(case.get("file") or "")
        classname = case.get("classname") or ""
        # classname 形如 tests.test_postgres_integration → tests/test_postgres_integration.py
        module = _norm(classname.replace(".", "/") + ".py") if classname else ""
        if not (file_attr in expected or module in expected or any(
            file_attr.endswith(name) for name in expected if name.endswith(".py")
        )):
            continue
        total += 1
        node = case.find("skipped")
        if node is not None:
            reason = node.get("message") or ""
            skipped.append(f"{file_attr or classname}::{case.get('name')} — {reason}")

    problems: list[str] = []
    if total == 0:
        problems.append(
            "junit 里没有任何 PG 相关测试用例——识别标记失效或跑错了文件？"
        )
    problems.extend(f"PG 相关测试被跳过：{s}" for s in skipped)
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="断言 PG 集成测试零跳过（防静默跳过）")
    parser.add_argument(
        "--junitxml",
        default=os.environ.get("PG_GUARD_JUNITXML", DEFAULT_JUNIT),
        help="主 pytest 步骤产出的 junit 报告路径",
    )
    parser.add_argument("--tests-dir", default=str(TESTS_DIR), help="测试目录（默认 tests/）")
    args = parser.parse_args(argv)

    if not in_ci():
        print("非 CI 环境（未设 CI / WARDEN_TEST_PG_HOST）：跳过防静默跳过守卫")
        return 0

    pg_files = discover_pg_test_files(Path(args.tests_dir))
    print(f"自动识别出 {len(pg_files)} 个 PG 依赖测试文件：")
    for path in pg_files:
        print(f"  - {path.relative_to(ROOT)}")

    if not pg_files:
        print("::error::没有识别出任何 PG 依赖测试文件——识别标记可能被改名了")
        return 1

    junit_path = Path(args.junitxml)
    if not junit_path.exists():
        print(
            f"::error::找不到 junit 报告 {junit_path}——主 pytest 步骤是不是漏了 --junitxml？"
        )
        return 1

    problems = check_junit(junit_path, pg_files)
    if problems:
        for problem in problems:
            print(f"::error::{problem}")
        return 1

    print("PG 相关测试全部执行、零跳过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
