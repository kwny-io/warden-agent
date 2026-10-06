#!/usr/bin/env python3
"""防「跳过即静默」的通用守卫：读 junit，断言**每一条被跳过的用例都有人签字**。

为什么需要它（一条真实事故 + 已有守卫的边界）：
  `scripts/check_pg_tests_ran.py` 守的是 **PG 一条轴**：CI 里 PG 用例不许跳过。它挡不住
  别的轴上的同一种事故——只要一条用例自己判断「本机条件不满足就 skip」，CI 就可能在
  全绿的情况下什么都没验。本仓真实的那条：
  `tests/test_alert_rules_evaluated.py::test_promtool真值求值通过` 需要本机已有
  `prom/prometheus:v2.55.0` 镜像（测试**故意**不联网拉镜像），而 `quality` job 里没人拉
  这个镜像、也没人断言它跑过 ⇒ 它在 CI 里每天跳过、CI 每天绿。于是「告警规则被真求值过」
  这句话在 CI 里是空的（第 1、2 步静态守卫仍然有效，但「表达式语义真的对」只有第 3 步能证）。

本守卫的规则（两条，都不是「手写文件清单」）：
  1. **未签字的跳过 = 红**。junit 里每一条 `<skipped>` 用例都必须在 `tests/SKIP_LEDGER.yaml`
     里被某个 `id`（`文件` 或 `文件::用例名前缀`）命中。谁新增一条「本机有才跑」的用例，
     它会自动出现在 junit 里 ⇒ 守卫自动变红 ⇒ 逼人当场表态（修成真跑，或签字+写理由）。
  2. **台账说了「CI 里不许跳」就不许跳**。CI 环境下，命中条目的 `allowed_in` 必须含 `ci`；
     否则红。`allowed_in: [local]` 是一条**承诺**：本地可以跳，CI 必须真跑。
     允许在门禁里留空洞的条目（含 `ci`）额外要写 `review_by`——空洞必须带期限。

台账条目的形状（与 `deploy/IMAGE_LEDGER.yaml` 同一套思路：不许静默豁免）：
  - id            必填：`tests/test_x.py` 或 `tests/test_x.py::test_y`（前缀匹配）
  - allowed_in    必填：`ci` / `local` 的任意非空子集
  - reason        必填：为什么这条可以跳
  - added         必填：YYYY-MM-DD
  - review_by     当 `allowed_in` 含 `ci` 时必填：什么时候必须回头复核

用法：
    # CI：放在主 pytest 步骤之后（主步骤需带 --junitxml）
    uv run --frozen python scripts/check_skipped_tests.py --junitxml /tmp/pytest-junit.xml
    # 本地：不传 --junitxml 就只做台账自检（字段/日期）；传了就按「本地」口径判
    .venv/Scripts/python.exe scripts/check_skipped_tests.py

缺 junit 时：本地放行（并说明「本次没有数据」）；CI 报红——**「守卫自己没数据」必须是失败，
不是默认通过**。

退出码：0 通过；1 有问题（未签字跳过 / CI 里跳过了「CI 不许跳」的条目 / 台账字段不合法 /
CI 缺 junit）。
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import os
import sys
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER = ROOT / "tests" / "SKIP_LEDGER.yaml"
DEFAULT_JUNIT = "/tmp/pytest-junit.xml"
_PG_ENV = "WARDEN_TEST_PG_HOST"
_ALLOWED_ENVS = {"ci", "local"}


@dataclass(frozen=True)
class Entry:
    """台账里的一条签字：允许某个（或某个文件里的）用例在指定环境里跳过。"""

    id: str
    allowed_in: frozenset[str]
    reason: str
    added: str
    review_by: str = ""

    def matches(self, case_id: str) -> bool:
        """前缀匹配。`tests/test_x.py` 命中它下面所有用例；`…::test_y` 命中该用例本身。

        参数化用例的名字是 `test_y[参数]`（中括号直接接在名字后、**没有 `::`**），
        所以还得认 `id + "["`——否则“给某条用例签字”在参数化时会静默失效。
        """
        if case_id == self.id:
            return True
        return case_id.startswith(self.id + "::") or case_id.startswith(self.id + "[")


@dataclass(frozen=True)
class Skipped:
    case_id: str
    reason: str


def in_ci(env: Mapping[str, str] | None = None) -> bool:
    """是否处于「必须检验真实覆盖」的环境。与 PG 守卫同一判据（CI / 显式配了 PG）。"""
    env = os.environ if env is None else env
    return env.get("CI", "").strip().lower() in {"1", "true", "yes"} or bool(env.get(_PG_ENV))


def _norm(path_str: str) -> str:
    return path_str.replace("\\", "/").lstrip("./")


def _is_iso_date(value: str) -> bool:
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def load_ledger(path: Path) -> tuple[list[Entry], list[str]]:
    """读台账并校验字段。返回（条目, 问题描述）。"""
    if not path.exists():
        return [], [f"找不到台账 {path}——未签字的跳过会全部变成红，先建它"]
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:  # pragma: no cover - 语法错走 yaml 自己的报错
        return [], [f"台账不是合法 YAML：{exc}"]

    problems: list[str] = []
    entries: list[Entry] = []
    for index, item in enumerate(raw.get("entries") or [], start=1):
        where = f"台账第 {index} 条"
        if not isinstance(item, dict):
            problems.append(f"{where}不是映射（键值对）")
            continue
        missing = [key for key in ("id", "allowed_in", "reason", "added") if not item.get(key)]
        if missing:
            problems.append(f"{where}缺必填字段：{', '.join(missing)}")
            continue
        allowed_raw = item["allowed_in"]
        if isinstance(allowed_raw, str):
            allowed_raw = [allowed_raw]
        allowed = {str(value).strip().lower() for value in allowed_raw}
        unknown = allowed - _ALLOWED_ENVS
        if not allowed or unknown:
            problems.append(
                f"{where}的 allowed_in 只能是 ci / local 的非空子集，现在出现：{sorted(unknown)}"
            )
            continue
        added = str(item["added"])
        if not _is_iso_date(added):
            problems.append(f"{where}的 added 不是 YYYY-MM-DD：{added!r}")
            continue
        review_by = str(item.get("review_by") or "")
        if "ci" in allowed and not review_by:
            problems.append(f"{where}允许在 CI 里跳过（allowed_in 含 ci），必须写 review_by")
            continue
        if review_by and not _is_iso_date(review_by):
            problems.append(f"{where}的 review_by 不是 YYYY-MM-DD：{review_by!r}")
            continue
        entries.append(
            Entry(
                id=_norm(str(item["id"])),
                allowed_in=frozenset(allowed),
                reason=str(item["reason"]).strip(),
                added=added,
                review_by=review_by,
            )
        )
    return entries, problems


def read_skipped(junit_path: Path) -> list[Skipped]:
    """把 junit 里所有被跳过的用例读出来（case_id 形如 `tests/test_x.py::test_y[参数]`）。"""
    root = ET.parse(junit_path).getroot()
    skipped: list[Skipped] = []
    for case in root.iter("testcase"):
        node = case.find("skipped")
        if node is None:
            continue
        file_attr = _norm(case.get("file") or "")
        classname = case.get("classname") or ""
        module = _norm(classname.replace(".", "/") + ".py") if classname else ""
        skipped.append(
            Skipped(
                case_id=f"{file_attr or module}::{case.get('name')}",
                reason=(node.get("message") or "").strip(),
            )
        )
    return skipped


def check(
    skipped: list[Skipped], entries: list[Entry], *, ci: bool
) -> tuple[list[str], list[str]]:
    """返回（问题, 提示）。问题非空即红。"""
    problems: list[str] = []
    notes: list[str] = []
    matched: set[int] = set()
    unused: list[str] = []
    for case in skipped:
        hits = [index for index, entry in enumerate(entries) if entry.matches(case.case_id)]
        if not hits:
            problems.append(
                f"未签字的跳过：{case.case_id}（跳过原因：{case.reason or '未写明'}）\n"
                "    要么让它真跑（比如在 workflow 里先把镜像/工具准备好），"
                "要么在 tests/SKIP_LEDGER.yaml 里签字并写清为什么。"
            )
            continue
        matched.update(hits)
        if ci and not any("ci" in entries[index].allowed_in for index in hits):
            problems.append(
                f"CI 里跳过了「只允许本地跳过」的用例：{case.case_id}\n"
                f"    台账签字：{entries[hits[0]].reason}\n"
                "    这等于这条用例在门禁里从没跑过。修法：在 CI 里补前置条件让它真跑，"
                "或把 allowed_in 改成含 ci 并写 review_by（= 承认这个空洞并有期限）。"
            )
    for index, entry in enumerate(entries):
        if index not in matched:
            unused.append(entry.id)
    if unused:
        head = "、".join(unused[:3])
        more = f"（另 {len(unused) - 3} 条）" if len(unused) > 3 else ""
        notes.append(
            f"{len(unused)} 条签字本次没派上用场（跨环境正常："
            f"只允许本地跳的条目在 CI 里就该用不上）：{head}{more}"
        )
    return problems, notes


def main(argv: list[str] | None = None) -> int:
    # 任何控制台都要能跑完：Windows 的 cp936 控制台编不了 `⇒`/`—` 这类字符，
    # 而本守卫的消息里会**引用 junit 里的跳过原因**（那是仓库自己写的字符）。
    # 打印到一半崩掉 = 守卫自己成了新的失败模式，这里降级成 `?` 也要把结论说出来。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            with contextlib.suppress(ValueError, OSError):
                stream.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description="断言每条被跳过的用例都有人签字（防静默跳过）")
    parser.add_argument(
        "--junitxml",
        default=os.environ.get("SKIP_GUARD_JUNITXML", ""),
        help="主 pytest 步骤产出的 junit 报告；不传则只做台账自检",
    )
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER), help="跳过台账路径")
    args = parser.parse_args(argv)

    ci = in_ci()
    entries, problems = load_ledger(Path(args.ledger))
    for problem in problems:
        print(f"::error::{problem}")
    if problems:
        return 1

    print(f"跳过台账：{args.ledger}（{len(entries)} 条签字；环境：{'CI' if ci else '本地'}）")

    junit_path = Path(args.junitxml) if args.junitxml else None
    if junit_path is None or not junit_path.exists():
        message = f"没有 junit 报告（{junit_path or '未指定'}）——本次只做了台账自检"
        if ci:
            print(f"::error::{message}；CI 里「守卫自己没数据」必须是失败")
            return 1
        print(f"{message}（本地放行）")
        return 0

    skipped = read_skipped(junit_path)
    print(f"junit 里被跳过的用例：{len(skipped)} 条")
    problems, notes = check(skipped, entries, ci=ci)
    for note in notes:
        print(f"提示：{note}")
    if problems:
        for problem in problems:
            print(f"::error::{problem}")
        return 1

    print("每一条被跳过的用例都有签字，且没有「CI 不许跳」的条目被跳过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
