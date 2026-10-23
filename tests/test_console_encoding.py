"""入口在**中文 Windows 的 cp936 控制台**上不许死于打印。

## 为什么单开一个文件（而不是塞进 `test_demo_e2e.py`）

因为 `test_demo_e2e.py` 用的正是让这个缺陷隐形的那种测法：

    with redirect_stdout(io.StringIO()):

`StringIO` 不限制编码 ⇒ `✅` 写进去永远成功 ⇒ 那条用例**结构上必然绿**，无论
`python -m warden_agent.demo_e2e` 在真实控制台上崩不崩。同理 `capsys`（pytest 的捕获流）
也是不限制编码的。所以这里**一律用子进程 + `PYTHONIOENCODING=gbk`**，照
`tests/test_mutant_check_verdicts.py::test_重定向到文件时不许死于字符编码` 的写法——
那是本仓已经验证过「去掉装置后立刻变红」的反向对照。

## 覆盖的三条路（对应三处真会崩的入口）

- `python -m warden_agent.demo_e2e` —— README 第 13 行的「⚡ 30 秒体验」
- `python -m warden_agent.cli audit-verify` —— **不加 `--json`**。加上 `--json` 就绕开了
  `print(("✅ " if ok else "❌ ") + detail)` 那一行，这条用例的全部价值就在于**不走 json 分支**。
- 另外：`settle` 断言输出文件本身按 UTF-8 可解 —— 只让进程不崩还不够，
  重定向产出必须是 UTF-8 字节，否则 `> log.txt` 出来的仍是乱码。

## 反向对照（真做过）

在接上 `force_utf8_stdio()` 之前，前两条都报
`UnicodeEncodeError: 'gbk' codec can't encode character '✅'` 并以 1 退出。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_gbk(module_argv: list[str], tmp_path: Path, name: str) -> tuple[int, str, bytes]:
    """在 `PYTHONIOENCODING=gbk` + 重定向到文件的环境里跑一遍，返回 (rc, 文本, 原始字节)。"""
    out = tmp_path / name
    env = dict(os.environ, PYTHONIOENCODING="gbk")
    env.pop("DEEPSEEK_API_KEY", None)  # 要离线路径：真模型会引入网络不确定
    with out.open("wb") as fh:
        proc = subprocess.run(
            [sys.executable, "-m", *module_argv],
            stdout=fh,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(REPO_ROOT),
            timeout=300,
        )
    raw = out.read_bytes()
    return proc.returncode, raw.decode("utf-8", errors="replace"), raw


def test_demo_e2e在cp936控制台上不崩(tmp_path: Path) -> None:
    """README 的「30 秒体验」必须在中文 Windows 上真的能跑完。"""
    rc, text, raw = _run_gbk(["warden_agent.demo_e2e"], tmp_path, "demo.log")
    assert "UnicodeEncodeError" not in text, text[-800:]
    assert rc == 0, text[-800:]
    # 只不崩不够：重定向出来的字节本身必须是 UTF-8，否则 > 到文件仍是乱码。
    assert "✅" in text or "离线引导演示完成" in text
    raw.decode("utf-8")  # 严格解码，失败就是没钉住


def test_cli审计核验在cp936控制台上不崩(tmp_path: Path) -> None:
    """`audit-verify` **不加 `--json`**：json 分支不打印 `✅`，测它就是绕开出事那一行。

    这个命令是「审计账被动过」的取证入口。它崩在这里的含义是：
    出事时值班的人拿不到结论，只拿到一行 `UnicodeEncodeError`。
    """
    db = tmp_path / "audit.db"
    rc, text, raw = _run_gbk(
        ["warden_agent.cli", "audit-verify", "--db", str(db)], tmp_path, "audit.log"
    )
    assert "UnicodeEncodeError" not in text, text[-800:]
    assert rc == 0, text[-800:]
    assert "链完整" in text, text[-800:]
    raw.decode("utf-8")


def test_force_utf8_stdio幂等且不动已经是utf8的流() -> None:
    """已经是 UTF-8 的流不做无意义重配；重复调用不炸（入口可能被多次进入）。"""
    import io

    from warden_agent.core.console import force_utf8_stdio

    buffer = io.BytesIO()
    fake = io.TextIOWrapper(buffer, encoding="utf-8", errors="strict")
    original = sys.stdout
    sys.stdout = fake
    try:
        force_utf8_stdio()
        force_utf8_stdio()  # 幂等
        print("✅ 已是 UTF-8 就不该被改动")
        fake.flush()
    finally:
        sys.stdout = original
    assert "✅" in buffer.getvalue().decode("utf-8")


def test_force_utf8_stdio能把cp936的流掰过来() -> None:
    """核心装置的正面证明：cp936 流原本写不出 `✅`，接上之后写得出来。"""
    import io

    from warden_agent.core.console import force_utf8_stdio

    buffer = io.BytesIO()
    fake = io.TextIOWrapper(buffer, encoding="cp936", errors="strict")
    original = sys.stdout
    sys.stdout = fake
    try:
        force_utf8_stdio()
        print("✅ 掰过来了")
        fake.flush()
    finally:
        sys.stdout = original
    assert "掰过来了" in buffer.getvalue().decode("utf-8")
