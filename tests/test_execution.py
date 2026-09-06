"""受控执行引擎测试：输出缓冲、超时预算、错误处理。"""
from __future__ import annotations

import sys

import pytest

from warden_agent.execution.broker import (
    BoundedOutputBuffer,
    ExecutionBroker,
    ExecutionBudget,
)


def test_successful_command() -> None:
    broker = ExecutionBroker()
    result = broker.execute([sys.executable, "-c", "print('hello world')"])
    assert result.success
    assert "hello world" in result.stdout


def test_command_not_found() -> None:
    broker = ExecutionBroker()
    result = broker.execute(["definitely-not-a-real-cmd-xyz", "--x"])
    assert result.exit_code == 127
    assert result.stdout == ""


def test_timeout_kills_process() -> None:
    """一个会无限跑的命令，超时预算必须拦住它。"""
    # 睡眠 60 秒 >> 预算 1 秒，应被强制超时终止
    code = "import time; time.sleep(60)"
    broker = ExecutionBroker(ExecutionBudget(timeout_seconds=1))
    result = broker.execute([sys.executable, "-c", code])
    assert result.timed_out
    assert result.exit_code is None  # 被终止，没有正常退出码


def test_output_buffer_truncates() -> None:
    """输出超过上限时必须被截断，而不是无限堆积。"""
    buf = BoundedOutputBuffer(max_bytes=10)
    ok = buf.append("a" * 5)
    assert ok
    ok2 = buf.append("b" * 10)  # 只放得下 5 字节
    assert not ok2
    assert buf.truncated
    assert len(buf.text) <= 10


def test_output_buffer_normal() -> None:
    buf = BoundedOutputBuffer(max_bytes=100)
    buf.append("hello")
    buf.append(" world")
    assert buf.text == "hello world"
    assert not buf.truncated


def test_budget_validation() -> None:
    with pytest.raises(ValueError):
        ExecutionBudget(timeout_seconds=0)
    with pytest.raises(ValueError):
        ExecutionBudget(max_output_bytes=0)


def test_stdout_captured_and_limited() -> None:
    """大量输出时 stdout 被截断标记，但执行本身正常完成。"""
    code = "print('x' * 10000)"
    broker = ExecutionBroker(ExecutionBudget(max_output_bytes=100, timeout_seconds=10))
    result = broker.execute([sys.executable, "-c", code])
    assert result.success
    assert result.stdout_truncated
    assert len(result.stdout) <= 100
