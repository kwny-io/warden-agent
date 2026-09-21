"""配置面守卫：用 AST 扫描源码，强制"环境变量必须登记、且只能被授权模块读"。

为什么需要这条测试（这是真实踩坑换来的）：
  配置读取原先散在 8 个文件、24 处 `os.environ.get(...)`，**没有任何地方声明
  "这个变量谁有权读"**。于是长出两类静默出错的 bug：

  - **一名两用**：`WARDEN_API_KEY` 被 HTTP 鉴权与 custom 模型同时读 →
    模型把服务端鉴权密钥发给第三方网关；`WARDEN_BASE_URL` 被 CLI 与 custom 模型同时读 →
    `warden chat` 把请求发到模型网关。两个都已修（拆成不同变量），但**根因是缺一份声明**。
  - **拼错静默忽略**：`WARDEN_RATE_LIMIT` 敲成 `WARDEN_RATELIMIT` → 悄悄用默认值。

  所以本文件把 `core/settings.py` 的注册表变成**可执行的约束**：
    1. 代码里读的每个环境变量，都必须在注册表里（否则新增变量会漏登记）；
    2. 读它的模块，不能超出注册表声明的 `consumers`（否则就是"越权读别人的变量"，
       也就是上面「一名两用」的形状）；
    3. 拼错的 `WARDEN_*` 变量要能被识别出来。

  没有这三条，注册表就只是文档——而文档是不会自己变红的。

口径说明：扫描规则是「`.get("字面量")` / `["字面量"]` 且字面量形如环境变量名」。
所以代码里读配置要写成 `.get("WARDEN_XXX")` 这种**字面量**形式；
把变量名当参数传给 helper（`helper(env, "WARDEN_XXX")`）会扫不到，本项目已约定不用那种写法。
若将来出现同形状、但**不是**环境变量的全大写常量，加进 `_NON_ENV_CONSTANTS` 即可。

注：本文件里的"密钥"全是**运行时拼出来的假值**，源码里刻意不出现任何像凭据的字面量。
"""

from __future__ import annotations

import ast
import re
import secrets
from pathlib import Path

import pytest

from warden_agent.core.settings import (
    ENV_SPECS,
    describe,
    registered_env_names,
    unknown_warden_variables,
    validate_env,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "warden_agent"

# 形如环境变量名的字符串常量
_ENV_LIKE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")
# 同形状但确实不是环境变量的常量（逃生舱；目前为空）
_NON_ENV_CONSTANTS: frozenset[str] = frozenset()


def _fake_value(tag: str) -> str:
    """运行时拼出的假值（避免在源码里写任何像凭据/密钥的字面量）。"""
    return f"faux-{tag}-" + secrets.token_hex(8)


def _collect_env_reads() -> dict[str, set[str]]:
    """扫描源码，返回 {变量名: {读它的模块相对路径}}。

    覆盖两种读法：`x.get("NAME")` 与 `x["NAME"]`（`x` 可能是 `os.environ`，
    也可能是 `resolve_auth(env)` 这种注入进来的 Mapping）。
    """
    reads: dict[str, set[str]] = {}
    for path in SRC.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - 语法错误由 ruff/mypy 先拦
            continue
        rel = path.relative_to(SRC).as_posix()
        for node in ast.walk(tree):
            literal: str | None = None
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                literal = node.args[0].value
            elif (
                isinstance(node, ast.Subscript)
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
            ):
                literal = node.slice.value
            if literal and _ENV_LIKE.match(literal) and literal not in _NON_ENV_CONSTANTS:
                reads.setdefault(literal, set()).add(rel)
    return reads


@pytest.fixture(scope="module")
def env_reads() -> dict[str, set[str]]:
    return _collect_env_reads()


def test_代码读取的环境变量都已登记(env_reads: dict[str, set[str]]) -> None:
    """新增环境变量必须在 `core/settings.py` 的 ENV_SPECS 里登记。

    没登记 = 没人知道它存在、没人知道谁该读它 —— 正是 bug 的温床。
    """
    unregistered = sorted(set(env_reads) - registered_env_names())
    assert not unregistered, (
        "以下环境变量被代码读取，但没在 core/settings.py 的 ENV_SPECS 里登记：\n"
        + "\n".join(f"  - {name}（读取模块：{sorted(env_reads[name])}）" for name in unregistered)
        + "\n请补上登记（用途 / 归属模块 / 允许读取的模块）。"
    )


def test_只有授权模块能读某个变量(env_reads: dict[str, set[str]]) -> None:
    """**冲突检测的落点**：读某变量的模块必须 ⊆ 注册表声明的 consumers。

    这条就是用来拦住「一名两用」的——当初 `cli.py` 去读 model 的 `WARDEN_BASE_URL`、
    `deepseek.py` 去读鉴权的 `WARDEN_API_KEY`，都会在这里被拦下。
    """
    violations: list[str] = []
    for spec in ENV_SPECS:
        actual = env_reads.get(spec.name, set())
        allowed = set(spec.consumers)
        extra = sorted(actual - allowed)
        if extra:
            violations.append(
                f"  - {spec.name}（归属 {spec.owner}）：被未授权的 {extra} 读取；"
                f"已声明的 consumers={sorted(allowed)}"
            )
    assert not violations, (
        "有模块读取了「不属于它」的环境变量——这就是同名两用 bug 的形状。\n"
        "要么改代码（用正确的变量），要么在 ENV_SPECS 里显式加进 consumers 并说明理由：\n"
        + "\n".join(violations)
    )


def test_注册表里声明的变量确实被代码用到(env_reads: dict[str, set[str]]) -> None:
    """反向检查：登记了却没人读，说明要么拼错了、要么代码已经不用了。"""
    stale = sorted(name for name in registered_env_names() if name not in env_reads)
    assert not stale, f"以下变量已登记但代码里没有任何读取点（拼错？或已废弃？）：{stale}"


def test_注册表无重复且消费者非空() -> None:
    names = [spec.name for spec in ENV_SPECS]
    assert len(names) == len(set(names)), "ENV_SPECS 里有重复登记"
    for spec in ENV_SPECS:
        assert spec.consumers, f"{spec.name} 没有声明任何 consumers"
        assert spec.purpose and spec.owner, f"{spec.name} 缺 purpose/owner"


# ---------- 校验与拼写检测 ----------


def test_限流格式写错会在启动时暴露() -> None:
    errors = validate_env({"WARDEN_RATE_LIMIT": "600次/分钟"})
    assert len(errors) == 1 and "WARDEN_RATE_LIMIT" in errors[0]


def test_整数写错会报错() -> None:
    errors = validate_env({"WARDEN_OUTBOUND_MAX_CONCURRENCY": "many"})
    assert len(errors) == 1 and "整数" in errors[0]


def test_布尔写错会报错() -> None:
    errors = validate_env({"WARDEN_AUDIT": "ture"})  # 拼错的 true
    assert len(errors) == 1 and "WARDEN_AUDIT" in errors[0]


def test_合法配置不报错() -> None:
    ok = {
        "WARDEN_RATE_LIMIT": "600/60",
        "WARDEN_OUTBOUND_MAX_CONCURRENCY": "8",
        "WARDEN_OUTBOUND_DAILY_QUOTA": "0",
        "WARDEN_AUDIT": "1",
        "WARDEN_ALLOW_ANON": "off",
        "WARDEN_STABILITY": "0",
        "PORT": "8000",
    }
    assert validate_env(ok) == []


def test_拼错的WARDEN变量会被识别出来() -> None:
    """`WARDEN_RATELIMIT` 少一个下划线 —— 原先会被静默忽略、悄悄用默认值。"""
    suspects = unknown_warden_variables(
        {"WARDEN_RATE_LIMIT": "600/60", "WARDEN_RATELIMIT": "10/60"}
    )
    assert suspects == ["WARDEN_RATELIMIT"]


def test_第三方变量不误报() -> None:
    """DEEPSEEK_API_KEY 这类非 WARDEN_ 前缀的不该被当成拼写错误。"""
    assert unknown_warden_variables({"DEEPSEEK_API_KEY": "placeholder", "PATH": "/usr/bin"}) == []


def test_敏感值在日志里打码() -> None:
    fake = _fake_value("token")  # 运行时生成，源码里没有像凭据的字面量
    joined = "\n".join(describe({"WARDEN_API_KEY": fake}))
    assert fake not in joined, "敏感值不得出现在给日志用的配置描述里"
    assert "WARDEN_API_KEY" in joined


def test_校验口径与运行时解析器一致() -> None:
    """防止两处漂移：settings 的格式校验不能比真正的解析器更严或更松。

    自包含校验是为了守住 core 的层级约束（不能 import 上层 web 模块），
    但两边对同一批取值的"接受/拒绝"必须一致。
    """
    from warden_agent.web.ratelimit import parse_rate_limit

    samples = ["600/60", "0", "off", "", "120/60", "10/60", "abc", "600", "600/", "1/2/3"]
    for value in samples:
        accepted_by_settings = not validate_env({"WARDEN_RATE_LIMIT": value})
        try:
            parse_rate_limit(value)
            accepted_by_runtime = True
        except ValueError:
            accepted_by_runtime = False
        assert accepted_by_settings == accepted_by_runtime, (
            f"取值 {value!r}：settings 校验={'接受' if accepted_by_settings else '拒绝'}，"
            f"但运行时解析器={'接受' if accepted_by_runtime else '拒绝'}"
        )
