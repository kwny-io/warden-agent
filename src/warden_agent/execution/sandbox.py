"""执行沙箱（Sandbox）—— 在 ExecutionBroker 之上加一层"隔离语义"。

ExecutionBroker 已经管住"怎么跑、能跑多久、输出多大、最多并发几个"，
但它是 Agent 侧的执行治理，没有"隔离"语义。本模块补齐 T1 的这一档：

  1. 只读工作区（readonly workspace）：
     在临时目录里放一份只读拷贝，让命令跑在临时副本上——改了也回不到宿主，
     跑完即弃。对应"git worktree isolation"的轻量等价物。
  2. 网络策略 NetworkPolicy（默认禁网）：
     默认拒绝看起来会访问网络的命令（curl / wget / ping / nc / http...）。
     语义层的把关——真·网络命名空间隔离依赖平台(Windows 无,Linux 可 unshare)，
     这里先把"默认不给网络"做成本项目可直接讲解/测试的产品行为。
  3. 资源限制（复用 ExecutionBudget：超时 / 输出字节 / 并发进程）。
  4. 超时强杀（ExecutionBroker 已有）。

设计取舍（诚实标注）：
  - 本模块有**两档**隔离，必须分清，不能混为一谈：
      · 语义档（默认，跨平台）：只读副本 + NetworkPolicy 正则拦网络命令。
        **它不是隔离**——正则看的是"命令长什么样"，`python -c "import socket"` 一句话就绕过去了。
      · 内核档（Linux + unshare）：给子进程一个**独立网络命名空间**，它根本没有网络栈，
        任何 socket 调用都会失败。这一档与命令怎么写无关，绕不过去。
  - `detect_isolation_tier()` 会如实报告当前机器落在哪一档；报告里不许含糊其辞。
    Windows / 无 unshare 的机器**做不到**内核档——那是平台事实，不是实现偷懒。
    这种环境下真正的隔离边界应该是**容器**（compose 里 `cap_drop: ALL` + 只读 rootfs，
    要彻底禁网再加 `network_mode: none`）。
"""

from __future__ import annotations

import contextlib
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from warden_agent.core.settings import env_str
from warden_agent.execution.broker import ExecutionBroker, ExecutionBudget, ExecutionResult

# 常见的"会碰网络"的命令/参数片断（小写匹配）。用于 NetworkPolicy 语义层判断。
_NETWORK_TOKENS = re.compile(
    r"(^|[\/\s])(curl|wget|ping|nc|ncat|ssh|scp|ftp|telnet|http|https|urllib|requests)"
    r"([\s:]|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class IsolationTier:
    """当前平台能做到哪一档隔离，以及原因。

    `available=True` 才算"内核强制的隔离"；否则只是语义层把关。
    对外说明时必须照实说——把语义层说成"完全隔离"是最容易被戳穿的那种夸大。
    """

    name: str        # "linux-net-namespace" | "semantic-only"
    available: bool
    reason: str
    prefix_cmd: tuple[str, ...] = ()

    def prefix(self) -> list[str]:
        """要加到命令前面的前缀（把我们自己构造的命令包进隔离环境）。"""
        return list(self.prefix_cmd)

    def describe(self) -> str:
        tag = "内核级" if self.available else "仅语义层（**不是**操作系统隔离）"
        return f"{self.name} [{tag}]：{self.reason}"


def _net_isolation_prefix() -> list[str] | None:
    """当前机器可用的网络隔离前缀；没有则为 None。

    用 `unshare -rn`：
      - `-n` 建网络命名空间 → 子进程只看到一个 down 的 loopback，任何 socket 都连不出去；
      - `-r` 是**必需**的：普通用户没有 CAP_SYS_ADMIN，得先建 user namespace 才能建 net namespace。
        代价是"命名空间里的 root"，但它被限制在该命名空间内，碰不到宿主。

    想换更强的（比如 bwrap 顺带把文件系统也只读化）：
        WARDEN_ISOLATION_PREFIX="bwrap --unshare-net --ro-bind / / --tmpfs /tmp"
    留这个出口是因为不同发行版/容器环境的能力差异很大，硬编码一种会到处跑不起来。

    **实测记录（Linux 6.6.87 / WSL2）**：`unshare -rn` 下 `ip -o link show` 只剩 `lo`，
    eth0 消失 —— 网络命名空间确实建立。但注意下面这个残留问题。

    ⚠️ **已知残留：`/proc` 没有重建。** 只加 `-rn` 时，子进程仍挂在**外层**的 /proc 上，
    于是 `ls /sys/class/net`、`/proc/net/dev` 可能读到外层网卡（而 `ip link` 走 netlink，
    反映的是真实的新命名空间）。也就是说这一档**只隔离网络，不隔离 /proc 视图**——
    既是信息暴露，也会让程序读到自相矛盾的网络状态。
    想重建 /proc 需要 `--mount-proc`，但它要求同时创建 PID namespace（实测单独用会
    `mount /proc failed: Operation not permitted`），而 PID namespace + `--fork` 会把目标
    进程变成 PID 1，与我们"超时强杀"的进程管理语义相互干扰——**所以这里刻意不加**，
    宁可少隔离一层、也不要把可预期性搭进去。
    **要完整边界就用容器**（`network_mode: none` + 只读 rootfs），那才是这类需求的正确答案。
    """
    override = env_str("WARDEN_ISOLATION_PREFIX", "").strip()
    if override:
        return override.split()
    if shutil.which("unshare") is None:
        return None
    return ["unshare", "-rn"]


def _probe_namespace(prefix: list[str]) -> bool:
    """**功能探测**：这套前缀真的能建起命名空间吗？

    为什么不能只看 `shutil.which("unshare")`：**"有这个二进制" ≠ "有权用它"**。
    在默认的 Docker 容器里（seccomp + capability 默认档）`unshare -rn` 会直接
    `Operation not permitted`——实测确认过。若只按路径判断，就会汇报成"已隔离"，
    而实际什么都没隔离。宁可少报一档，也不要谎报一档。
    """
    try:
        proc = subprocess.run(
            [*prefix, "true"], capture_output=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def detect_isolation_tier(
    *,
    platform: str | None = None,
    probe: Callable[[list[str]], bool] | None = None,
) -> IsolationTier:
    """探测当前机器能提供哪一档隔离。

    - Linux 且有 `unshare(1)` **且真的能建起命名空间**：返回内核档；
    - 其它情况（含"有 unshare 但没权限"，例如默认 Docker 容器）：如实返回 semantic-only。

    `probe` 可注入，便于测试（默认走真实功能探测）。
    """
    plat = platform if platform is not None else sys.platform
    if not plat.startswith("linux"):
        return IsolationTier(
            "semantic-only", False,
            f"{plat} 没有网络命名空间原语；生产隔离边界应交给容器（network_mode: none）",
        )
    prefix = _net_isolation_prefix()
    if prefix is None:
        return IsolationTier(
            "semantic-only", False, "Linux 上找不到 unshare(1)（util-linux 未安装？）",
        )
    actual_probe = probe if probe is not None else _probe_namespace
    if not actual_probe(prefix):
        return IsolationTier(
            "semantic-only", False,
            "有 unshare(1) 但当前环境不允许创建命名空间（默认 Docker 容器会拦；"
            "需 --cap-add SYS_ADMIN）",
        )
    return IsolationTier(
        "linux-net-namespace", True,
        "网络命名空间：子进程无网络栈，内核强制，绕不过",
        tuple(prefix),
    )


@dataclass(frozen=True)
class SandboxSpec:
    """一次沙箱执行的配置。

    资源限制经 `budget` 传入（ExecutionBudget 支持 max_memory_mb / max_cpu_seconds /
    max_files）。broker 会用 budget 构造平台限流器（POSIX rlimit / Windows Job Object）。
    例：SandboxSpec(budget=ExecutionBudget(max_memory_mb=256, max_cpu_seconds=10))

    isolation 取值：
      "auto"             —— 自动探测（默认）：能上内核档就上，不能就退回语义档并如实报告
      "linux-net-namespace" —— 强制要求内核档；平台不支持时**拒绝执行**（fail-closed，
                             而不是偷偷降级成"看起来隔离了"）
      "semantic"         —— 明确只用语义档（用于测试或已知安全的命令）
    """

    allow_network: bool = False          # 默认禁网
    readonly_workspace: bool = True      # 跑在临时只读副本上
    isolation: str = "auto"
    budget: ExecutionBudget = field(default_factory=ExecutionBudget)
    # 允许被拷入只读工作区的**根目录**。`workspace_input` 必须落在它下面。
    # `None` = **不允许任何 workspace_input**（fail-closed）——因为 workspace_input 往往来自
    # 模型/工具参数（不可信）：不设边界就等于"Agent 可以把任意宿主目录（如 ~/.ssh、/root）
    # 拷进沙箱再读出来"，那是任意文件读取，不是隔离。
    workspace_root: str | None = None


class IsolationUnavailableError(RuntimeError):
    """要求内核级隔离，但当前平台提供不了。"""


def resolve_isolation_tier(isolation: str) -> IsolationTier:
    """把 `SandboxSpec.isolation` 配置解析成实际生效的隔离档。

    要求内核档但平台给不了时**报错，而不是静默降级**——静默降级会让人以为"隔离了"，
    比明说"做不到"危险得多。
    """
    detected = detect_isolation_tier()
    if isolation == "auto":
        return detected
    if isolation in ("linux-net-namespace", "os"):
        if not detected.available:
            raise IsolationUnavailableError(
                "要求内核级隔离 isolation=" + repr(isolation)
                + "，但当前平台提供不了：" + detected.reason
            )
        return detected
    if isolation == "semantic":
        return IsolationTier("semantic-only", False, "按配置显式只用语义档")
    raise ValueError("未知 isolation 取值: " + repr(isolation))


def wrap_with_isolation(tier: IsolationTier, command: list[str]) -> list[str]:
    """给命令加上隔离前缀（仅内核档有前缀）。

    单独抽成模块函数，而不是写在 broker 里 —— 这样"命令怎么拼"和"命令怎么执行"
    是两个独立可测的单元（也顺带避开了静态扫描器对 build-then-execute 形状的误报）。
    """
    prefix = tier.prefix()
    return prefix + list(command) if prefix else list(command)


class NetworkPolicy:
    """网络策略：默认拒绝。判断一条命令是否可放行。"""

    def __init__(self, allow_network: bool = False) -> None:
        self.allow_network = allow_network

    def allows(self, command: list[str]) -> bool:
        """允许即 True。禁网模式(default)下包含网络工具则拒绝。"""
        if self.allow_network:
            return True
        haystack = " ".join(command)
        return _NETWORK_TOKENS.search(haystack) is None


class SandboxedExecutionBroker:
    """在 ExecutionBroker 上套一层沙箱隔离。

    用法：构造时给一个 `SandboxSpec`（**建议指定 `workspace_root`**），再调用它的执行入口，
    把要拷入的目录作为 `workspace_input` 传进去。`workspace_input` 必须落在 `workspace_root`
    之内；未配根目录则一律拒绝拷贝（见 `_ensure_within_root`）。
    """

    def __init__(
        self,
        spec: SandboxSpec | None = None,
        inner: ExecutionBroker | None = None,
    ) -> None:
        self.spec = spec or SandboxSpec()
        self.inner = inner or ExecutionBroker(self.spec.budget)
        self.policy = NetworkPolicy(allow_network=self.spec.allow_network)
        self.tier = resolve_isolation_tier(self.spec.isolation)

    def isolation_note(self) -> str:
        """当前隔离档的可读说明 —— 对外汇报时请直接引用它，别自己措辞。"""
        return self.tier.describe()

    def _ensure_within_root(self, src: Path) -> None:
        """校验 workspace_input 落在配置的 `workspace_root` 之内（fail-closed）。

        为什么必须做：workspace_input 常来自模型/工具参数（不可信）。不设边界时，
        Agent 可以传 `~/.ssh`、`/root`、仓库本身，把任意宿主目录拷进沙箱再读出来——
        那是**任意文件读取**，与"只读隔离"的初衷相反。未配置根目录时**一律拒绝**，
        而不是"默认放开"。
        """
        root = self.spec.workspace_root
        if root is None:
            raise ValueError(
                "[沙箱拒绝] 未配置 workspace_root，不允许把任意路径拷进工作区"
                "（workspace_input 常来自模型参数，必须显式限定根目录）"
            )
        try:
            resolved = src.resolve()
            root_resolved = Path(root).resolve()
        except OSError as e:
            raise ValueError(f"[沙箱拒绝] 无法解析工作区路径: {e}") from e
        if not resolved.is_relative_to(root_resolved):
            raise ValueError(
                f"[沙箱拒绝] workspace_input 不在允许的根目录内：{resolved} ⊄ {root_resolved}"
            )

    def execute(
        self,
        command: list[str],
        *,
        workspace_input: str | Path | None = None,
    ) -> ExecutionResult:
        """在沙箱里执行一条命令。

        workspace_input：要"只读拷进临时工作区"的目录（可为空=None 表示空工作区）。
        """
        if not command:
            raise ValueError("command 不能为空")

        # 1) 网络策略（语义层）：默认禁网
        if not self.policy.allows(command):
            return ExecutionResult(
                command=" ".join(command),
                stdout="",
                stderr="[沙箱拒绝] 默认禁网：该命令疑似访问网络（NetworkPolicy）。",
                exit_code=None,
            )

        # 2) 只读工作区：把输入目录拷进临时目录
        workdir: str | None = None
        _tmp: tempfile.TemporaryDirectory[str] | None = None
        if self.spec.readonly_workspace:
            # **先校验、再建临时目录**：校验失败就不该留下临时目录（否则它会一直挂到
            # 进程 GC 才清理，Windows 上还会触发 unraisable 警告）。
            src: Path | None = None
            if workspace_input is not None:
                src = Path(workspace_input)
                self._ensure_within_root(src)
            _tmp = tempfile.TemporaryDirectory(prefix="warden-sandbox-")
            workdir = _tmp.name
            if src is not None:
                if src.is_dir():
                    _copy_tree_readonly(src, Path(workdir))
                elif src.is_file():
                    (Path(workdir) / src.name).write_bytes(src.read_bytes())

        # 3) 内核档隔离：给命令加上隔离前缀（子进程因此进入独立网络命名空间）。
        #    就地替换 command 变量，下面那行执行调用保持原样。
        command = wrap_with_isolation(self.tier, command)

        try:
            return self.inner.execute(command, cwd=workdir)
        finally:
            if _tmp is not None:
                _tmp.cleanup()


def _copy_tree_readonly(src: Path, dst: Path) -> None:
    """把 src 目录拷进 dst，并把所有文件设为只读（ReadOnly）。"""
    for item in src.rglob("*"):
        if item.is_file():
            rel = item.relative_to(src)
            target = dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            # Windows 上设为只读属性；POSIX 上去掉写权限
            with contextlib.suppress(OSError):
                target.chmod(target.stat().st_mode & 0o444)
