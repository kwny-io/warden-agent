"""量「跑这条测试要拉起多少次 git 子进程」——成本断言的尺子，随仓走。

**为什么在仓里**（第八十六批的证据）：第八十五批把一条判据的成本数字写进 CHANGELOG 与测试
注释，但那把尺子当时只是**仓外的临时脚本**。按第八十二批的教训（仓外日志的 sha256 不算
证据），这类数字必须能用仓里的工具、在任意检出上重放——否则下一个人只能选择相信或重写。

用法（在**要量的那棵树**里跑；本脚本只观测，不改任何文件）：

    cd <任意检出>
    ./.venv/Scripts/python.exe scripts/count_git_spawn.py                     # 默认整条台账文件
    ./.venv/Scripts/python.exe scripts/count_git_spawn.py \
        tests/test_delivery_ledger.py::test_交付台账干净
    ./.venv/Scripts/python.exe scripts/count_git_spawn.py tests/DELIVERY_LEDGER.yaml

`sys.path` 里会自动补上当前检出的 `src/`，所以同一把尺子可以量旧树与新树（A/B 时两棵树
各跑一次，输出并列即可）。退出码：pytest 通过或**测试红**都算「量到了」⇒ 0；只有
收集/用法错误（pytest rc 2/4）才非 0，免得把「红」误当「尺子坏了」。

实现要点（第一版踩过坑）：**只挂 `subprocess.Popen`，而且必须用子类而不是函数**——任何在
import 期做 `class Popen(subprocess.Popen)` 的第三方库（asyncio.windows_utils → anyio →
pytest 插件）会因为它是普通函数而当场 TypeError。`subprocess.run / check_output / check_call`
内部都走 `Popen`，所以挂一个既唯一又不漏。

边界（别拿它当万灵药）：只数**本进程内**拉起的 git。若被测代码又另起一个 pytest/git 包装
进程，那层看不见；要量那种成本得看墙钟。它量的是成本，不是正确性——pytest 自身红/绿会
原样打在输出里。
"""

from __future__ import annotations

import os
import subprocess
import sys

if len(sys.argv) > 1 and sys.argv[1] in {"-h", "--help"}:
    print(__doc__)
    raise SystemExit(0)

REPO = os.getcwd()
sys.path.insert(0, os.path.join(REPO, "src"))

calls: list[str] = []

_RealPopen = subprocess.Popen


class SpyingPopen(_RealPopen):  # type: ignore[misc,valid-type]
    def __init__(self, *a: object, **k: object) -> None:
        cmd = a[0] if a else k.get("args")
        flat = " ".join(str(x) for x in cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
        if "git" in flat:
            calls.append(flat[:150])
        super().__init__(*a, **k)  # type: ignore[arg-type]


subprocess.Popen = SpyingPopen  # type: ignore[assignment]

import pytest  # noqa: E402  （必须在挂完 Popen 之后再 import：顺序就是判据本身）

TARGET = sys.argv[1] if len(sys.argv) > 1 else "tests/test_delivery_ledger.py"
rc = pytest.main(["-q", "-p", "no:cacheprovider", *TARGET.split()])

print(f"\n=== TARGET: {TARGET}")
print(f"=== git 子进程真实调用次数：{len(calls)}")
seen: dict[str, int] = {}
for flat in calls:
    seen[flat] = seen.get(flat, 0) + 1
for key, n in sorted(seen.items(), key=lambda kv: -kv[1]):
    print(f"  x{n}  {key}")

raise SystemExit(0 if rc in (0, 1) else rc)
