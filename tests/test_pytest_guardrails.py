"""守住 pytest **自身配置**里那两条"让问题现形"的开关。

为什么要专门写测试管配置：这两条开关的价值不在功能，而在**故障时的可观测性**，
所以它们没有任何调用方——被谁顺手删掉、或"本地为了跑快一点"改成 0 都无人察觉，
直到下一次真的挂住（那时又一次是"整个套件静默停在 45%，没有失败信息"）。

真实事故（2026-09-28）：一条用裸 `threading.Barrier` 同步的并发用例，某个工作线
抛异常后其余 7 个永久等待，整个套件**静默卡在 45%**，直到外部超时被杀，
日志里看不出是哪一条、卡在哪一行。`pytest-timeout` 就是那次事故的补丁：
配上它之后，同一条用例会打印**所有线程的调用栈**并以非 0 退出。
"""

from __future__ import annotations

import pytest


def test_单条用例超时已配置(pytestconfig: pytest.Config) -> None:
    """超时必须开，且必须用 thread 法（Windows 上没有 SIGALRM）。"""
    timeout = pytestconfig.getini("timeout")
    # ini 里可能被解析成字符串，转一下再比，避免"看起来配了其实是 0/空串"
    assert float(timeout) > 0, (
        f"pytest 的 timeout 必须 > 0（当前 {timeout!r}）：没有它，卡死与跑得慢无法区分"
    )
    # thread 法两边行为一致（signal 法在 Windows 上根本不存在）：
    # 用默认值会变成"本地没保护、CI 才有保护"这种最糟糕的组合。
    assert pytestconfig.getini("timeout_method") == "thread"


def test_警告被当成错误(pytestconfig: pytest.Config) -> None:
    """`filterwarnings = ["error"]`：警告在 CI 里必须变成失败，不能被刷过去。"""
    assert "error" in pytestconfig.getini("filterwarnings")
