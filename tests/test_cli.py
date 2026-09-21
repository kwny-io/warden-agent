"""CLI（warden 命令）测试：验证命令路由与参数解析。

这些测试不连接真实 HTTP 服务，只验证 CLI 的命令分派、参数解析、
以及各子命令 handler 存在且可路由。
"""

from __future__ import annotations

from warden_agent import cli

# 期望存在的子命令及其 handler
_EXPECTED = {
    "chat": "_cmd_chat",
    "stream": "_cmd_stream",
    "approvals": "_cmd_approvals",
    "approve": "_cmd_approve",
    "reject": "_cmd_reject",
    "health": "_cmd_health",
    "caps": "_cmd_caps",
    "coding": "_cmd_coding",
    "recover": "_cmd_recover",
    "backup": "_cmd_backup",
    "restore": "_cmd_restore",
    "stuck": "_cmd_stuck",
}


def test_子命令_handler_全部存在() -> None:
    for cmd, handler in _EXPECTED.items():
        assert callable(getattr(cli, handler, None)), f"缺 {cmd} 的 handler: {handler}"


def test_命令行可解析() -> None:
    p = cli._build_parser()  # noqa: SLF001
    sub = {a.dest: a for a in p._actions if getattr(a, "choices", None)}  # noqa: SLF001
    assert sub["cmd"].choices.keys() == set(_EXPECTED.keys())


def test_default_base_url() -> None:
    assert cli.DEFAULT_BASE.startswith("http://127.0.0.1")


def test_服务地址用WARDEN_SERVER_URL覆盖() -> None:
    assert cli.default_base_url({"WARDEN_SERVER_URL": "http://10.0.0.5:9000"}) == (
        "http://10.0.0.5:9000"
    )


def test_服务地址不读模型端点变量() -> None:
    """回归：`WARDEN_BASE_URL` 是 custom 模型的端点，**不能**被 CLI 当服务地址用。

    曾经同名 → 配了自建模型网关后，`warden chat` 会把请求发到那个模型网关上去。
    """
    assert cli.default_base_url({"WARDEN_BASE_URL": "https://my-llm-gw.example.com/v1"}) == (
        "http://127.0.0.1:8000"
    )
    # 两个都设时，只有 WARDEN_SERVER_URL 生效
    both = cli.default_base_url(
        {"WARDEN_SERVER_URL": "http://srv:8000", "WARDEN_BASE_URL": "https://gw/v1"}
    )
    assert both == "http://srv:8000"


def test_client_trust_env_false() -> None:
    """CLI 访问本地服务必须禁用系统代理（否则 127.0.0.1 被代理转发 → 502）。"""
    with cli._client() as c:  # noqa: SLF001
        assert c.trust_env is False
