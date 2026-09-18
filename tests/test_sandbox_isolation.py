"""执行沙箱的**隔离档**测试：把"哪一档"和"绕不绕得过"都钉死。

这份测试有两个不可替代的价值：

1. `test_语义层拦不住_socket_一句话就绕过去` —— 用可执行的证据说明
   **NetworkPolicy 正则不是安全边界**。不写这条，就很容易在汇报里把它说成"禁网"。
2. `test_内核档下网络调用真的失败` —— 在内核档上跑一次真实的 socket 连接并断言它失败。
   这条只在 Linux + unshare 可用时执行（Windows/macOS 自动跳过）；
   仓库的 CI 跑在 ubuntu-latest 上，所以**它会真的被执行**。
"""

from __future__ import annotations

import sys

import pytest

from warden_agent.execution import sandbox as sb
from warden_agent.execution.sandbox import (
    IsolationTier,
    IsolationUnavailableError,
    NetworkPolicy,
    SandboxedExecutionBroker,
    SandboxSpec,
    detect_isolation_tier,
    resolve_isolation_tier,
    wrap_with_isolation,
)

# 收集期探测一次，用于 skipif（跑在 CI 的 ubuntu 上时 available=True）
_DETECTED = detect_isolation_tier()

_SEMANTIC = IsolationTier("semantic-only", False, "测试用：语义档")
_OS_TIER = IsolationTier("linux-net-namespace", True, "测试用：内核档",
                         ("unshare", "-rn"))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认清掉前缀覆盖，避免外部环境干扰档位探测。"""
    monkeypatch.delenv("WARDEN_ISOLATION_PREFIX", raising=False)


# ---------------- 档位探测：必须如实，不许含糊 ----------------


@pytest.mark.parametrize("plat", ["win32", "darwin"])
def test_非linux平台如实报告没有内核档(plat: str) -> None:
    tier = detect_isolation_tier(platform=plat)
    assert tier.available is False
    assert tier.name == "semantic-only"
    # 说明里必须点出真正的隔离边界在哪 —— 不能只说"不支持"就完事
    assert "容器" in tier.reason


def test_linux且缺unshare时也如实报告(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sb.shutil, "which", lambda _name: None)
    tier = detect_isolation_tier(platform="linux")
    assert tier.available is False
    assert "unshare" in tier.reason


def test_linux且有unshare且真能用时才给内核档(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sb.shutil, "which", lambda _name: "/usr/bin/unshare")
    tier = detect_isolation_tier(platform="linux", probe=lambda _p: True)
    assert tier.available is True
    assert tier.name == "linux-net-namespace"
    # -r 不能少：普通用户得先建 user namespace 才能建 net namespace
    assert tier.prefix() == ["unshare", "-rn"]


def test_有unshare但没权限时如实降级到语义档(monkeypatch: pytest.MonkeyPatch) -> None:
    """**"有这个二进制" ≠ "有权用它"**。

    默认 Docker 容器里 `unshare -rn` 会 `Operation not permitted`（实测过）。
    若只看路径就汇报"已隔离"，那是在说谎；所以探测必须是功能性的。
    """
    monkeypatch.setattr(sb.shutil, "which", lambda _name: "/usr/bin/unshare")
    tier = detect_isolation_tier(platform="linux", probe=lambda _p: False)
    assert tier.available is False
    assert tier.name == "semantic-only"
    assert "SYS_ADMIN" in tier.reason  # 得给出可操作的线索，不是干巴巴一句"不支持"


def test_前缀可被环境变量覆盖(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WARDEN_ISOLATION_PREFIX", "bwrap --unshare-net")
    monkeypatch.setattr(sb.shutil, "which", lambda _name: "/usr/bin/unshare")
    tier = detect_isolation_tier(platform="linux", probe=lambda _p: True)
    assert tier.prefix() == ["bwrap", "--unshare-net"]


def test_档位说明会区分内核级与语义层() -> None:
    assert "内核级" in _OS_TIER.describe()
    assert "不是" in _SEMANTIC.describe()  # 语义层必须明确写"不是操作系统隔离"


# ---------------- 档位解析：不给就报错，不静默降级 ----------------


def test_auto档跟随探测结果() -> None:
    assert resolve_isolation_tier("auto").name == detect_isolation_tier().name


def test_显式要求语义档() -> None:
    tier = resolve_isolation_tier("semantic")
    assert tier.available is False
    assert tier.prefix() == []


def test_要求内核档但平台给不了时_报错而不是降级(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """这是本模块最重要的一条安全语义：静默降级会让人以为"隔离了"。"""
    monkeypatch.setattr(
        sb, "detect_isolation_tier",
        lambda **_kw: IsolationTier("semantic-only", False, "模拟：平台不支持"),
    )
    with pytest.raises(IsolationUnavailableError) as ei:
        resolve_isolation_tier("os")
    assert "平台提供不了" in str(ei.value)


def test_未知档位取值报错() -> None:
    with pytest.raises(ValueError):
        resolve_isolation_tier("whatever")


# ---------------- 命令包装：不改原列表 ----------------


def test_内核档会给命令加前缀() -> None:
    assert wrap_with_isolation(_OS_TIER, ["ls"]) == ["unshare", "-rn", "ls"]


def test_语义档不加前缀() -> None:
    assert wrap_with_isolation(_SEMANTIC, ["ls"]) == ["ls"]


def test_包装不修改传入的原列表() -> None:
    original = ["ls", "-l"]
    wrap_with_isolation(_OS_TIER, original)
    assert original == ["ls", "-l"]


# ---------------- 诚实记录：语义层的缺口 ----------------


def test_语义层拦不住_socket_一句话就绕过去() -> None:
    """NetworkPolicy 是**词面匹配**：命令里没有 curl/wget 这些词就放行了。

    这就是"禁网靠正则"不成立的原因 —— 换成内核档，命令怎么写都连不出去。
    """
    bypass = [sys.executable, "-c",
              "import socket; socket.socket().connect(('1.1.1.1', 80))"]
    assert NetworkPolicy(allow_network=False).allows(bypass) is True  # 没拦住


def test_broker会如实报出当前档位() -> None:
    broker = SandboxedExecutionBroker(spec=SandboxSpec(isolation="semantic"))
    assert "semantic-only" in broker.isolation_note()
    assert "不是" in broker.isolation_note()


# ---------------- 实证：内核档真的把网络关掉了（仅 Linux） ----------------


@pytest.mark.skipif(
    not _DETECTED.available,
    reason=f"当前平台没有内核档：{_DETECTED.reason}",
)
def test_内核档下网络调用真的失败() -> None:
    """在内核档里跑一次真实 socket 连接，必须**明确报告被阻断**。

    断言刻意不写成"没有 CONNECTED"——那是**假通过**：万一 `unshare` 因权限失败，
    探针根本没跑起来、stdout 为空，那种断言照样绿。所以这里要求两件事：
    ① 退出码为 0（说明隔离环境起来了、python 真的跑到了）；
    ② 打印出 BLOCKED 标记（说明连接是被阻断的，而不是压根没执行）。
    """
    broker = SandboxedExecutionBroker(spec=SandboxSpec(isolation="os"))
    probe = [
        sys.executable, "-c",
        "import socket\n"
        "try:\n"
        "    s = socket.socket(); s.settimeout(3); s.connect(('1.1.1.1', 80))\n"
        "    print('RESULT=CONNECTED')\n"
        "except OSError as e:\n"
        "    print('RESULT=BLOCKED', e.errno)\n",
    ]
    result = broker.execute(probe)
    assert result.exit_code == 0, f"隔离环境没起来：{result.stderr!r}"
    assert "RESULT=BLOCKED" in (result.stdout or ""), (
        f"预期连接被阻断，实际 stdout={result.stdout!r}"
    )
