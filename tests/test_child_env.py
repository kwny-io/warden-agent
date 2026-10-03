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


def test_摘WARDEN是前缀规则不是手写清单() -> None:
    """锁的是**规则**，不是「今天有哪几个变量」。

    上面那条只断言了已知名字（`WARDEN_API_KEY`）。如果哪天有人把它改写成「显式列出
    要摘的变量」（或者有人为了“更明确”而写死一长串），今天的用例**全都还是绿的**，
    而下一个新命名的 `WARDEN_*` 就会静默泄给操作者装配的外部组件。
    所以这里用今天**不存在**的名字来测规则本身（含 Windows 上的大小写不敏感）。
    """
    base = {
        "WARDEN_BRAND_NEW_THING": "x",
        "warden_lower_case": "y",
        "WARDEN_": "z",
        "OPENAI_API_KEY": "vendor",
        "PATH": "/bin",
    }
    env = env_without_warden(base)
    assert [k for k in env if k.upper().startswith("WARDEN_")] == [], env
    assert env == {"OPENAI_API_KEY": "vendor", "PATH": "/bin"}


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


# ---- 输出编码：两侧必须对齐（2026-10-01 实测过的静默失败）------------------------
#
# 口口：`communicate()` 解码失败发生在读取线程里，异常被线程吞掉，于是
#   exit_code=0 而 stdout=''。模型无法区分“命令真的没输出”和“我们解码炸了”。
# 实测：本地化 Windows（宿主 cp936）上，子进程写 GBK 的中文，父进程跑在
#   PYTHONUTF8=1 下按 UTF-8 解码 → `UnicodeDecodeError: 'utf-8' codec can't
#   decode byte 0xce in position 1: invalid continuation byte`，stdout 变空串。


def test_子进程写入编码被强制为utf8() -> None:
    """契约：Python 子进程的 std 流一律 UTF-8。"""
    assert minimal_child_env()["PYTHONIOENCODING"] == "utf-8"


def test_宿主locale不能把子进程编码带歪(monkeypatch) -> None:
    """宿主自己带着 gbk 的 PYTHONIOENCODING 时，子进程仍然必须写 UTF-8。

    不断言“大概能解码”，而是 **字节级对得上**：拿到的就是原字。
    （源码里不写中文，用 \\u 转义，免得 argv 的编码又成为一个变量。）
    """
    monkeypatch.setenv("PYTHONIOENCODING", "gbk")
    assert minimal_child_env()["PYTHONIOENCODING"] == "utf-8"

    result = ExecutionBroker().execute(
        [sys.executable, "-c", r"print('\u4e2d\u6587 ok')"])

    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "\u4e2d\u6587 ok", repr(result.stdout)  # 不是空串、不是乱码


def test_非utf8字节不会被整段丢弃() -> None:
    """非 Python 子进程按自己的编码写字节：允许看不懂，**不允许整段消失**。

    `errors="replace"` 是这条的兜底：'' 意味着“命令没输出”，而 U+FFFD 意味着
    “有输出但我们没解出来” —— 后者可查，前者不可查。
    """
    snippet = "import sys;sys.stdout.buffer.write(bytes.fromhex('d6d0cec4'))"
    result = ExecutionBroker().execute([sys.executable, "-c", snippet])

    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() != "", "输出被解码异常吞了：exit_code=0 但 stdout 为空"
    assert "\ufffd" in result.stdout
