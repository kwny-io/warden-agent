"""演示脚本的回归：**它必须一直能跑通，并且一直能拒绝那三次**。

演示脚本最容易坏的地方不是逻辑，而是"改了实现之后没人再跑它"。
这条测试把演示当成一个可执行契约：脚本模型七步走完，事件流里必须出现
limit / estop / estop 三个拒绝码，且最终急停状态为 False（复位过）。
"""

from __future__ import annotations

import pytest

from warden_agent.demo_robot import main


def test_演示脚本跑通且拒绝次数对得上(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--sim"]) == 0
    out = capsys.readouterr().out
    assert "sim（手写二阶系统" in out
    assert "模型调用 7 次" in out
    # 三次拒绝：越界一次、急停一次、急停锁存本身一次
    assert "command/limit" in out
    assert "command/estop" in out
    assert "safety/estop" in out
    assert "急停状态：False" in out


def test_串口后端也能跑通并且报总线计数(capsys: pytest.CaptureFixture[str]) -> None:
    # 走的是同一套帧协议/CRC/重试，对端是环回设备：没有真机也能回归"总线"这条路。
    assert main(["--serial"]) == 0
    out = capsys.readouterr().out
    assert "serial（帧协议" in out
    assert "总线计数：发 " in out
    assert "command/limit" in out
    assert "CRC 坏 0" in out
