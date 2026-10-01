"""沙箱隔离档的**声明门禁**测试：声明与事实不符时必须拒绝，而不是静默降级。

背景（这是审计里的 P0-3）：`execution/sandbox.py` 有两档——
  · 语义档：只读工作区副本 + 用正则拦"看起来会联网"的命令。**它不是隔离**
    （`python -c "import socket"` 一句话就绕过，见 NetworkPolicy 的 docstring）；
  · 内核档：`unshare -rn` 给子进程一个没有网络栈的命名空间，与命令怎么写无关。

问题是"声明"这一层：默认 `isolation="auto"` 时，在 Windows / 默认 Docker 容器里
会**安静地**退回语义档，于是"我们有沙箱隔离"这句话在报告里成立、在运行时是假的。
本文件的断言就是那条底线：**声称内核档却给不了 → 必须报错**。
"""

from __future__ import annotations

import pytest

from warden_agent.agent import augment_catalog
from warden_agent.execution.sandbox import (
    IsolationTier,
    IsolationUnavailableError,
    NetworkPolicy,
    SandboxedExecutionBroker,
    SandboxSpec,
    default_sandbox_isolation,
    detect_isolation_tier,
    isolation_tier_from_env,
    resolve_enforced_isolation,
    sandbox_isolation_policy,
)
from warden_agent.tool.catalog import ToolCatalog

# 一个"给不了内核档"的环境（Windows / WSL / 默认 Docker 容器都是这个形态）


def _kernel_probe_yes(prefix: list[str]) -> bool:
    """注入用的探针替身："这个环境能建命名空间"。"""
    return True


def _kernel_probe_no(prefix: list[str]) -> bool:
    """注入用的探针替身："有 unshare 但没权限"（默认 Docker 容器 / Windows）。"""
    return False


def _tier(available: bool = False) -> IsolationTier:
    return IsolationTier(
        "linux-net-namespace" if available else "semantic-only",
        available,
        "测试替身",
        ("unshare", "-rn") if available else (),
    )


# --------------------------------------------------------------------------
# 声明解析
# --------------------------------------------------------------------------
def test_未声明时档位取值默认是auto() -> None:
    assert sandbox_isolation_policy({}) == ("auto", False)
    assert default_sandbox_isolation({}) == "auto"


def test_声明取值大小写与空白都归一() -> None:
    assert sandbox_isolation_policy({"WARDEN_SANDBOX_ISOLATION": " Kernel "})[0] == "kernel"
    assert sandbox_isolation_policy({"WARDEN_SANDBOX_ISOLATION": "SEMANTIC"})[0] == "semantic"


def test_声明取值写错时直接报错而不是按默认值跑() -> None:
    """写错不猜：静默按 auto 跑会让人以为隔离生效了——那是"看起来安全"。"""
    with pytest.raises(ValueError, match="auto / kernel / semantic"):
        sandbox_isolation_policy({"WARDEN_SANDBOX_ISOLATION": "kernal"})  # 拼错


def test_布尔写法要求内核档() -> None:
    assert sandbox_isolation_policy({"WARDEN_SANDBOX_REQUIRE_KERNEL": "1"}) == ("auto", True)
    assert sandbox_isolation_policy({"WARDEN_SANDBOX_REQUIRE_KERNEL": "off"}) == ("auto", False)


# --------------------------------------------------------------------------
# 声明 × 环境能力 → 实际档位
# --------------------------------------------------------------------------
def test_声明内核档但环境给不了时拒绝而不是降级() -> None:
    """本文件的核心断言：fail-closed。"""
    with pytest.raises(IsolationUnavailableError) as exc:
        isolation_tier_from_env(
            {"WARDEN_SANDBOX_ISOLATION": "kernel"},
            probe=_kernel_probe_no,
            platform="linux",
        )
    message = str(exc.value)
    assert "WARDEN_SANDBOX_ISOLATION=kernel" in message
    # 报错必须给可执行的出路，否则运维只能去读源码
    assert "network_mode: none" in message
    assert "shell.run" in message


def test_布尔开关同样触发拒绝() -> None:
    with pytest.raises(IsolationUnavailableError, match="REQUIRE_KERNEL=1"):
        isolation_tier_from_env(
            {"WARDEN_SANDBOX_REQUIRE_KERNEL": "true"},
            probe=_kernel_probe_no,
            platform="linux",
        )


def test_声明内核档且环境支持时给出内核档(monkeypatch: pytest.MonkeyPatch) -> None:
    # 用真实存在的配置口子把“这台机器有 unshare”注入进来（不去 monkeypatch 私有函数）：
    # 这个变量就是给不同发行版/容器环境留的替换出口。
    monkeypatch.setenv("WARDEN_ISOLATION_PREFIX", "unshare -rn")
    tier = isolation_tier_from_env(
        {"WARDEN_SANDBOX_ISOLATION": "kernel"},
        probe=_kernel_probe_yes,
        platform="linux",
    )
    assert tier.available is True
    assert tier.name == "linux-net-namespace"


def test_auto声明给不了内核档时如实退回语义档() -> None:
    """auto 允许退回（开发机做不到内核档），但**不许声称是隔离**。"""
    tier = isolation_tier_from_env({}, probe=_kernel_probe_no, platform="linux")
    assert tier.available is False
    assert "不是" in tier.describe() or "仅语义层" in tier.describe()


def test_显式声明语义档时尊重声明且说明不是隔离() -> None:
    tier = isolation_tier_from_env(
        {"WARDEN_SANDBOX_ISOLATION": "semantic"},
        probe=_kernel_probe_yes,
        platform="linux",
    )
    assert tier.available is False
    assert "semantic" in tier.reason


def test_参数层与部署声明合并时取更严的那个(monkeypatch: pytest.MonkeyPatch) -> None:
    """参数写了 semantic、环境能给内核档 → 用内核档（多一层不是坏事）。"""
    monkeypatch.setenv("WARDEN_ISOLATION_PREFIX", "unshare -rn")
    tier = resolve_enforced_isolation(
        "semantic",
        {},
        probe=_kernel_probe_yes,
        platform="linux",
    )
    assert tier.available is True


def test_参数层auto不会把部署声明当空气() -> None:
    """`SandboxSpec()` 的默认 isolation="auto" 曾经会让 REQUIRE_KERNEL=1 失效。"""
    with pytest.raises(IsolationUnavailableError):
        resolve_enforced_isolation(
            "auto",
            {"WARDEN_SANDBOX_REQUIRE_KERNEL": "1"},
            probe=_kernel_probe_no,
            platform="linux",
        )


def test_参数层写死内核档时同样拒绝() -> None:
    with pytest.raises(IsolationUnavailableError):
        resolve_enforced_isolation("kernel", {}, probe=_kernel_probe_no, platform="linux")


def test_真实平台探测如实报告而且不抛异常() -> None:
    """Windows 上必须老老实实说"没有网络命名空间原语"。"""
    tier = detect_isolation_tier()
    assert tier.available is False
    assert tier.name == "semantic-only"


def test_构建期就按声明解析档位(monkeypatch: pytest.MonkeyPatch) -> None:
    """`SandboxedExecutionBroker` 构造时就该按环境声明解析（而不是等到执行时）。"""
    monkeypatch.setenv("WARDEN_SANDBOX_ISOLATION", "kernel")
    monkeypatch.setenv("WARDEN_SANDBOX_REQUIRE_KERNEL", "1")
    if detect_isolation_tier().available:
        pytest.skip("本机有内核档，这条断言的场景（给不了内核档）不成立")
    with pytest.raises(IsolationUnavailableError):
        SandboxedExecutionBroker(spec=SandboxSpec(workspace_root="."))


# --------------------------------------------------------------------------
# 与工具目录的接线：shell.run 的注册与"确认档位"绑定
# --------------------------------------------------------------------------
def test_注册shell_run时声明内核档但给不了会失败(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WARDEN_SANDBOX_REQUIRE_KERNEL", "1")
    if detect_isolation_tier().available:
        pytest.skip("本机有内核档，这条断言的场景（给不了内核档）不成立")
    catalog = ToolCatalog()
    with pytest.raises(IsolationUnavailableError):
        augment_catalog(catalog, sandbox=True)
    # fail-closed 的落地：宁可没有这个工具，也不要一个"看起来隔离了"的 shell.run
    assert "shell.run" not in {t.name for t in catalog.all()}


def test_注册成功时extra里如实记录隔离档(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WARDEN_SANDBOX_REQUIRE_KERNEL", raising=False)
    monkeypatch.setenv("WARDEN_SANDBOX_ISOLATION", "semantic")
    catalog = ToolCatalog()
    extra = augment_catalog(catalog, sandbox=True)
    assert "shell.run" in {t.name for t in catalog.all()}
    assert extra["sandbox"] is True
    assert extra["sandbox_isolation_available"] is False
    assert extra["sandbox_isolation"] == "semantic-only"
    # 说明文字必须自己就写明"不是操作系统隔离"
    assert "语义层" in extra["sandbox_isolation_note"]


# --------------------------------------------------------------------------
# 绊线不是边界：把已知绕过写成测试，免得日后有人把它当隔离
# --------------------------------------------------------------------------
def test_网络策略只是绊线可以绕过() -> None:
    """这条测试**故意断言绕过存在**。

    它存在的意义：任何人想拿 NetworkPolicy 当"网络隔离"的证据时，
    先看看这条测试——正则看的是命令长相，动态构造的命令它看不见。
    真边界只有内核档或容器层。
    """
    policy = NetworkPolicy(allow_network=False)
    assert policy.allows(["curl", "https://example.com"]) is False  # 拦得住
    assert policy.allows(["python", "-c", "import socket; print(1)"]) is True  # 拦不住
