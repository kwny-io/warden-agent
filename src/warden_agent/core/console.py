"""把进程的 stdout/stderr 钉成 UTF-8 —— 给**每个会自己打印东西的入口**用。

## 为什么需要这个模块

2026-10-23 真踩：中文 Windows 的 `sys.stdout` 是 **cp936**（GBK）。任何 GBK 装不下的字符
（`✅` U+2705、`❌` U+274C、`⚠` U+26A0、`⇒` U+21D2、`µ` U+00B5……）一旦 `print`，
就抛 `UnicodeEncodeError` 并以 1 退出。实测：

    $ python -m warden_agent.demo_e2e
    File ".../demo_e2e.py", line 254, in _line
        print(text)
    UnicodeEncodeError: 'gbk' codec can't encode character '✅'

而 README 第 13 行把这条命令写成「⚡ 30 秒体验」。也就是说**陌生人拿到仓库跑的
第一个命令**，在中文 Windows 上必然失败。

## 为什么它一直没被抓到（两个结构性的盲区）

1. **CI 全是 `ubuntu-latest`**，那里的 stdout 本来就是 UTF-8 ⇒ 这条路在 CI 上永远绿。
2. **测试用 `redirect_stdout(io.StringIO())` 或 pytest 的 `capsys`**，两者都不限制编码
   ⇒ `✅` 写进去永远成功。`tests/test_demo_e2e.py` 就是这么测的：功能全覆盖，那一行没覆盖。

本仓库对这件事**一直是清醒的**——`scripts/check_delivery_push.py`、`scripts/mutant_check.py`
各有同一份装置（后者还配了子进程 + `PYTHONIOENCODING=gbk` 的反向对照测试）。但两份都住在
`scripts/` 里、只保护脚本自己，**从没进过 `src/warden_agent/` 的入口**。这个模块补的就是那个洞。

## 为什么是 `errors="replace"` 而不只是换编码

换编码解决「本来能写但被 cp936 挡住」；`errors="replace"` 解决「换了编码仍写不出」。
宁可打一个 `?`，也不能让**测量/取证装置**死于打印——这正是 `mutant_check` 踩过的那个坑：
矩阵在 #[34/75] 死掉，日志恰恰是最需要它生成的那份产物。

## 边界

- 已经是 UTF-8 的流（Linux / CI / pytest 捕获流）**不做重配**，重配是一次无操作。
- 被包装过 / 已关闭的流不接受 `reconfigure`：吞掉 `ValueError`/`OSError`，**不因此失败**。
  （`capsys` 替换掉的流就是这一类，它本来也没这个毛病。）
- `scripts/` 下那两份**保留自己的副本**，不改成 import 这个模块：那些脚本的用法是
  `python scripts/xxx.py`，不能假设 `warden_agent` 已被安装。
"""
from __future__ import annotations

import contextlib
import sys


def force_utf8_stdio() -> None:
    """把 `sys.stdout` / `sys.stderr` 重配成 UTF-8（幂等，可重复调用）。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        encoding = (getattr(stream, "encoding", "") or "").replace("-", "").lower()
        if encoding == "utf8":
            continue
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="replace")
