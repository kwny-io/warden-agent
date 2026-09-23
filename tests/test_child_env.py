"""子进程环境变量策略：密钥不进子进程（真跑一个子进程验，不是看代码）。

为什么单独测这个：`subprocess` 不传 `env=` 时子进程**继承父进程全部环境变量**，
而模型能通过 `shell.run` → 沙箱 → `ExecutionBroker` 影响子进程跑什么命令。
修复前实测 `print(os.environ['WARDEN_API_KEY'])` 能原样打出服务端鉴权密钥。
"""

from __future__ import annotations

import sys

from warden_agent.execution.broker import ExecutionBroker
from warden_agent.execution.child_env import env_without_warden, minimal_child_env


def test_最小白名单不含任何密钥(monkeypatch) -> None:
    monkeypatch.setenv("WARDEN_API_KEY", "secret")
    monkeypatch.setenv("WARDEN_CREDENTIAL_KEY", "master-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-vendor")
    monkeypatch.setenv("WARDEN_VAULT_TOKEN", "vault-token")

    env = minimal_child_env()

    assert "WARDEN_API_KEY" not in env
    assert "WARDEN_CREDENTIAL_KEY" not in env
    assert "DEEPSEEK_API_KEY" not in env
    assert "WARDEN_VAULT_TOKEN" not in env


def test_最小白名单保留PATH() -> None:
    """PATH 必须留下 —— 不给子进程 PATH，"安全"就变成"命令全都找不到"。"""
    env = minimal_child_env()
    assert any(k.upper() == "PATH" for k in env), env


def test_最小白名单在Windows保留SystemRoot() -> None:
    """Windows 上少了 SystemRoot 子进程可能起不来（系统 DLL / winsock 依赖它）。

    这条只在 Windows 上能真验证"留下了"，其他平台跳过（没有这个变量可留）。
    """
    import os

    env = minimal_child_env()
    if os.name == "nt":
        assert any(k.upper() == "SYSTEMROOT" for k in env), env


def test_代理变量刻意不进子进程(monkeypatch) -> None:
    """代理刻意不放行：出网必须由 Web 层收口（SSRF 校验 + 出站限速），
    子进程自己走系统代理等于绕过那层治理。"""
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")

    env = minimal_child_env()

    assert "HTTP_PROXY" not in env
    assert "HTTPS_PROXY" not in env


def test_extra_是唯一显式放行入口() -> None:
    env = minimal_child_env({"MY_TOOL_FLAG": "1"})
    assert env["MY_TOOL_FLAG"] == "1"


def test_外部组件保留自己的密钥_只摘WARDEN前缀() -> None:
    base = {"WARDEN_API_KEY": "s", "OPENAI_API_KEY": "vendor", "PATH": "/bin"}
    env = env_without_warden(base)
    assert "WARDEN_API_KEY" not in env
    assert env["OPENAI_API_KEY"] == "vendor"
    assert env["PATH"] == "/bin"


def test_真实子进程读不到服务端密钥(monkeypatch) -> None:
    """端到端：走 ExecutionBroker（模型能走的那条真实路径）跑一个真实子进程。

    这条测试在修复前**会失败并打印出真密钥** —— 它守的就是那个洞。
    """
    monkeypatch.setenv("WARDEN_API_KEY", "secret-should-not-leak")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-vendor-should-not-leak")
    snippet = (
        "import os;"
        "print(os.environ.get('WARDEN_API_KEY','<无>'),"
        "os.environ.get('DEEPSEEK_API_KEY','<无>'))"
    )

    result = ExecutionBroker().execute([sys.executable, "-c", snippet])

    assert result.exit_code == 0, result.stderr
    assert "secret-should-not-leak" not in result.stdout
    assert "sk-vendor-should-not-leak" not in result.stdout
    assert result.stdout.strip() == "<无> <无>"


def test_真实子进程仍然能正常跑起来(monkeypatch) -> None:
    """反面对照：最小环境不能把子进程跑废（沙箱仍要能用）。

    只测"进了白名单的变量还说在"是不够的 —— 这条直接验证：给了最小环境后，
    子进程依然能拿到 PATH 找到解释器、能正常执行并回传输出。
    """
    monkeypatch.setenv("WARDEN_API_KEY", "secret")

    result = ExecutionBroker().execute(
        [sys.executable, "-c", "import os,sys;print(os.getcwd() and sys.version_info.major)"])

    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "3"
