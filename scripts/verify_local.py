#!/usr/bin/env python
"""本地验证总闸：一条命令跑完本地能跑的门禁，**并且把每个阶段的退出码累计起来**。

## 为什么要有这个文件

第八十七批的真实教训：交付时我用 shell 把几条验证命令串起来跑（`a & b & c`），
结果只看到**最后一条**的退出码 —— 中间 pytest 红着，整条命令照样退 0，我据此报了「全绿」，
把红灯当绿灯推了一次。同一批里还有第二个坑：手工挑了「受影响的那几个守卫文件」跑，
而 CI 的 quality job 有 17 个命名步骤，挑着跑就等于**让本地比 CI 少跑东西却不说话**。

所以这里的规矩只有两条：

1. **任何阶段非 0 ⇒ 整体非 0**（逐个跑、退出码累计，不靠 shell 串联）。
2. **本地覆盖不到 CI 的哪些步骤，必须逐条登记理由**（`EXEMPT`），
   由 `tests/test_verify_local.py` 对着 `.github/workflows/ci.yml` 的 quality job 双向对账 ——
   没覆盖也不登记的步骤会让判据红，而不是安静地少跑。

## 它**不是**什么

- 不能替代 CI：容器构建、kubeconform、trivy、gitleaks 反向对照、前端门禁、Playwright e2e、
  变异测试分片这些要么需要外部资源、要么在别的 job 上（见 `EXEMPT` 的理由）。
- 不产生交付证据：证据的权威口径是 GitHub Actions 的逐 job 结论（`scripts/ci_evidence.py`）。

用法：
    python scripts/verify_local.py                # 全套本地能跑的门禁
    python scripts/verify_local.py --base <sha>   # 额外跑「推前形状守卫」
    python scripts/verify_local.py --list         # 只看阶段与覆盖对账表

判据：`tests/test_verify_local.py`（累计退出码 / 与 CI 步骤双向对账 / 豁免理由不许是占位符）。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 用当前解释器：裸 `python` 在 Windows 上可能落到 Microsoft Store 占位符（第八十七批踩过）
PY = sys.executable

_SHAPE_STEP = "推前形状守卫（这次交付范围的形状）"

# CI quality job 的命名步骤 → 为什么本地不跑。键必须与 ci.yml 的 `- name:` 逐字一致。
# 这些不是「不重要」，而是「本机不是那些步骤的环境」——权威口径是 CI。
EXEMPT: dict[str, str] = {
    "Install (按 uv.lock 固定版本，含 embodiment 可选依赖)":
        "CI 用它把依赖钉到 uv.lock 并装上 mujoco；本机 .venv 由同一条 uv sync 对齐过"
        "（含 embodiment），所以本地会真的跑那 5 条物理用例——这里的差异只剩「谁执行安装」",
    "安装 postgresql-client（让 pg_dump 端到端测试真的跑）":
        "需要 apt 装 pg 客户端；本机没有 ⇒ 本机 PG 用例按台账跳过，CI 才是权威",
    "预热规则求值镜像（让 promtool 真值求值真的跑）":
        "需要 docker 拉 prom/prometheus 镜像；本机不保证有 docker",
    "前端门禁（lint + 类型检查 + 单测与覆盖率 + 构建）":
        "需要 npm ci 装前端依赖；前端本批没动，权威口径是 CI",
    "真浏览器 e2e（Playwright：CSP 在真引擎里不挡自己的资源）":
        "需要 playwright 浏览器与 npm 依赖；权威口径是 CI",
    "安装 ts 客户端依赖（让真实 MCP 集成在 CI 里真的跑）":
        "需要 npm ci 装 MCP 客户端依赖；权威口径是 CI；同上的外部资源理由",
    '上传 junit（Pytest 红了也要有，供本地用 CI 的真数据核"跳过台账"）':
        "只上传构建产物，不含断言；本地 junit 直接落在临时目录里给下面两条用",
    "K8s manifests 校验（kubeconform，官方 schema 严格模式）":
        "需要 kubeconform 二进制；权威口径是 CI",
}


def _stages(junit: Path, base: str | None = None) -> list[dict[str, object]]:
    """本地阶段清单：`ci_step` 必须与 ci.yml 的步骤名逐字一致；本地专有的要写 `local_only`。

    形状守卫**始终在清单里**（否则与 CI 步骤的对账会把它当成「本地未覆盖也不登记」）；
    只是 `base` 为空时 `run_stages` 会把它记为「未跑」而不是绿。
    """
    return [
        {
            "name": "ruff",
            "ci_step": "Ruff (lint)",
            "argv": [PY, "-m", "ruff", "check", "src", "tests", "scripts"],
        },
        {
            "name": "mypy",
            "ci_step": "Mypy (strict)",
            "argv": [PY, "-m", "mypy", "src/warden_agent"],
        },
        {
            "name": "pytest（全量 + 覆盖率门槛，与 CI 同一条命令）",
            "ci_step": "Pytest（含覆盖率门槛）",
            "argv": [
                PY, "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider",
                "--cov=warden_agent", f"--junitxml={junit}",
            ],
        },
        {
            "name": "PG 集成测试真跑了（防静默跳过）",
            "ci_step": "断言 PostgreSQL 集成测试真的执行了（防静默跳过）",
            "argv": [PY, "scripts/check_pg_tests_ran.py", "--junitxml", str(junit)],
        },
        {
            "name": "被跳过的用例都有人签字",
            "ci_step": "断言被跳过的用例都有人签字（防静默跳过，覆盖所有轴）",
            "argv": [PY, "scripts/check_skipped_tests.py", "--junitxml", str(junit)],
        },
        {
            "name": "镜像引用（钉 digest 或进台账）",
            "ci_step": "交付/演练清单的镜像引用（钉 digest 或进台账）",
            "argv": [PY, "scripts/check_pinned_images.py"],
        },
        {
            "name": "文档声明的反引号与「路径:行号」",
            "ci_step": "交付文档里的反引号转义与「路径:行号」（文档说了不代表说得对）",
            "argv": [PY, "scripts/check_doc_claims.py"],
        },
        {
            "name": "Markdown 表格竖线",
            "ci_step": "Markdown 表格里的竖线（未转义会被 GFM 当分隔符）",
            "argv": [PY, "scripts/check_md_tables.py"],
        },
        {
            "name": "变异锚点（--dry）",
            "local_only":
                "变异测试本身是另一个 job（push 上 skipped）；锚点校验便宜且不需要跑测试",
            "argv": [PY, "scripts/mutant_check.py", "--dry"],
        },
        _shape_stage(base),
    ]


def _shape_stage(base: str | None) -> dict[str, object]:
    return {
        "name": _SHAPE_STEP,
        "ci_step": _SHAPE_STEP,
        "argv": [PY, "scripts/check_delivery_push.py", "--base", base or ""],
    }


def run_stages(
    stages,
    base: str | None,
    *,
    runner=subprocess.run,
) -> tuple[int, list[tuple[str, int, float]]]:
    """逐个跑阶段并累计退出码。

    `runner(argv, env) -> int` 可注入（判据用假 runner，不真起子进程）。
    返回 `(整体退出码, [(阶段名, 退出码, 秒)])`；退出码 -1 表示「本次没跑」（要看得见，不是绿）。
    """
    results: list[tuple[str, int, float]] = []
    failed = 0
    for st in stages:
        name = str(st["name"])
        if str(st.get("ci_step", "")) == _SHAPE_STEP and not base:
            results.append((name, -1, 0.0))
            continue
        argv = [str(a) for a in st["argv"]]  # type: ignore[index]
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        t0 = time.monotonic()
        rc = int(runner(argv, env))
        results.append((name, rc, time.monotonic() - t0))
        if rc != 0:
            failed += 1
    return (1 if failed else 0), results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="本地验证总闸：累计退出码；与 CI 的覆盖面差异登记在 EXEMPT",
    )
    ap.add_argument("--base", help="额外跑推前形状守卫（交付推送时给上一提交）")
    ap.add_argument("--list", action="store_true", help="只列阶段与覆盖对账表，不跑")
    args = ap.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="warden-verify-") as td:
        stages = _stages(Path(td) / "junit.xml", args.base)
        if args.list:
            for st in stages:
                if st.get("ci_step"):
                    tag = f"← CI 步骤：{st['ci_step']}"
                else:
                    tag = f"← 本地专有：{st['local_only']}"
                print(f"  - {st['name']}  {tag}")
            print(f"\nCI 有、本地不跑（已登记理由）：{len(EXEMPT)} 条")
            for step, why in EXEMPT.items():
                print(f"  - {step}：{why}")
            return 0

        def _run(argv: list[str], env: dict[str, str]) -> int:
            return subprocess.run(argv, env=env).returncode

        rc, results = run_stages(stages, args.base, runner=_run)
        print("\n=== 本地验证汇总（每段退出码都在这，不靠 shell 串联）===")
        for name, code, dt in results:
            flag = "未跑" if code < 0 else ("绿" if code == 0 else "红")
            print(f"  [{flag}] {code:>3}  {dt:6.1f}s  {name}")
        if rc:
            print("\n结论：有阶段红着 ⇒ 整体非 0（不要拿「其余都绿」抵消它）")
        else:
            print("\n结论：本地能跑的都绿。CI 仍是权威口径（未覆盖步骤见 EXEMPT）。")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
