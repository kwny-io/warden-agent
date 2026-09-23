"""子进程环境变量策略 —— 不把宿主环境整包交给子进程。

## 为什么需要这个模块（这不是洁癖，是一个真实被测出来的洞）

`subprocess.Popen(...)` / `subprocess.run(...)` **不传 `env=` 时，子进程继承父进程的
全部环境变量**。而本项目的宿主进程（服务端）环境里装着：

    WARDEN_API_KEY          服务端鉴权密钥（拿到它就能以任意用户调 /audit 等接口）
    WARDEN_CREDENTIAL_KEY   凭证加密主密钥（拿到它能解开所有存储的凭证）
    DEEPSEEK_API_KEY 等     模型厂商密钥（拿到它能直接烧钱）
    WARDEN_VAULT_TOKEN      KMS/Vault token
    HTTP_PROXY / HTTPS_PROXY 代理（绕过 Web 层的 SSRF 校验与出站限速）

而**模型能间接决定子进程跑什么命令**（`shell.run` 工具 → 沙箱 → `ExecutionBroker`）。
于是模型只要跑一句 `env`，或者 `python -c "import os;print(os.environ)"`，
就能把上面这些原样读出来 —— 沙箱、审批、审计全都绕不过这一步。

2026-09-24 修复前实测（真实子进程，不是推演）：

    >>> os.environ["WARDEN_API_KEY"] = "secret-should-not-leak"
    >>> ExecutionBroker().execute([sys.executable, "-c", "print(os.environ['WARDEN_API_KEY'])"])
    exit_code = 0
    stdout    = 'secret-should-not-leak'

## 两条策略：按"这个子进程是谁"选，不按方便选

1. `minimal_child_env()` —— **最小白名单**。
   给"模型能影响的命令"用：沙箱执行（`ExecutionBroker`）、git 只读探测。
   只放行"进程能起来 + 能把结果写回"所必需的键（PATH、Windows 的
   SystemRoot / PATHEXT / TEMP、语言与时区）。
   方向是 **fail-closed**：想让子进程多看一个变量，必须**显式加进白名单并说明理由**；
   而不是"默认都能看见、出事再拉黑名单"（黑名单永远漏）。
   刻意不放行：任何密钥、厂商配置、代理变量。

2. `env_without_warden()` —— **继承但摘掉本项目自己的密钥**。
   给"操作者装配的外部组件"用（MCP server CLI）。这类子进程可能真的需要自己的
   厂商密钥（它自己的 `OPENAI_API_KEY` 之类），一刀切白名单会让它没法工作；
   但 `WARDEN_*` 是本进程自己的服务端配置，外部组件没有任何正当理由需要它，一律摘掉。

Windows 注意：`os.environ` 的键在 Windows 上是大写的（`SYSTEMROOT`、`PATH`），
所以比对一律走 `.upper()`，而键值原样保留（不改写子进程看到的大小写）。
"""

from __future__ import annotations

import os
from collections.abc import Mapping

# 最小白名单：只有这些键会进"模型能影响的子进程"。
# 每一项都是"不给就起不来 / 拿不到结果"，不是"给了更方便"。
MINIMAL_ENV_KEYS: frozenset[str] = frozenset({
    # ---- POSIX / 通用 ----
    "PATH",         # 找可执行文件（不给就等于命令全都不存在）
    "HOME",         # git 等工具找用户配置
    "LANG", "LC_ALL", "TZ",   # 语言与时区（影响输出编码/时间），无密钥价值
    "SHELL", "TMPDIR", "USER", "LOGNAME",
    # ---- Windows ----
    "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE",   # 系统 DLL 与 winsock 依赖它
    "PATHEXT",      # 解析 `python` → `python.exe`
    "COMSPEC", "OS",
    "TEMP", "TMP",  # 建临时文件
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
})

# 本项目自己的变量前缀：任何外部子进程都不需要它。
PROJECT_ENV_PREFIX = "WARDEN_"


def minimal_child_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """构造给子进程的**最小**环境（白名单）。

    `extra`：显式追加的键值（覆盖同名项）。用它来放行"某个工具确实需要的"变量，
    并且调用处必须写清楚为什么 —— 这是唯一的放行入口，方便 review。
    """
    env = {k: v for k, v in os.environ.items() if k.upper() in MINIMAL_ENV_KEYS}
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


def env_without_warden(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """继承环境，但摘掉 `WARDEN_*`（本进程自己的服务端配置与密钥）。

    给"操作者装配的外部组件"用：它自己的厂商密钥要留下，本项目的密钥一律不给。
    """
    source = os.environ if base is None else base
    return {
        k: v for k, v in source.items()
        if not k.upper().startswith(PROJECT_ENV_PREFIX)
    }
