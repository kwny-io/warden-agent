"""审批策略：AI 想做高危动作前，先按规则决定要不要拦下来问用户。



白话解释：
模型(大脑)可能会提出"我要删 C 盘文件"、"我要 git push 到线上"这种有风险的动作。
我们不能让 AI 想干嘛就干嘛，于是设一道门禁——每次工具调用前都过这道门槛：

    DENY  （禁） ：这种动作绝对禁止，不需要问，直接拒绝。
    ASK   （问） ：有风险但要用户拍板 → 先挂起等用户批准，同意才执行。
    ALLOW （放）：没风险，直接放行执行。

优先级：DENY 最大，一票否决；其次是 ASK；最后才是 ALLOW。

实现方式：每种风险动作提供一个"判定函数(policy)"，系统把多个 policy 的结果
合起来，取最严格的那个(按 DENY > ASK > ALLOW 排序)。
"""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto

logger = logging.getLogger(__name__)


class Decision(Enum):
    ALLOW = auto()
    ASK = auto()
    DENY = auto()


# 每条 policy 就是一个函数：给它工具名和参数，它返回一个 Decision，并说明理由
Policy = Callable[[str, dict[str, object]], "PolicyResult"]


@dataclass(frozen=True)
class PolicyResult:
    decision: Decision
    reason: str = ""


class PolicyDenied(Exception):
    """策略 DENY：该工具被永久禁止，任务直接失败。

    统一放在这里（policy 层）：loop 和 session 都从这 import，不再各自定义一份，
    避免"跨模块 `except PolicyDenied` 捕不到对方抛的"这种隐患。
    """


class ApprovalRequired(Exception):
    """策略 ASK：该工具需要人工批准，调用方必须**挂起**、绝不能执行。

    与 AgentSession 的 `NeedsApproval` 语义一致：会话侧把 ASK 转成 WAITING_APPROVAL，
    loop（同步 API）用抛出本异常表达"挂起"——调用方捕获后走审批，批准后再执行。
    """

    def __init__(self, tool_name: str, arguments: dict[str, object], reason: str = "") -> None:
        super().__init__(f"工具 {tool_name!r} 需要人工批准: {reason}")
        self.tool_name = tool_name
        self.arguments = arguments
        self.reason = reason


class PolicyEngine:
    """把多条 policy 合成一道门禁。取最严格的判定。"""

    def __init__(self) -> None:
        self._policies: list[Policy] = []

    def add(self, policy: Policy) -> None:
        self._policies.append(policy)

    def evaluate(self, tool_name: str, arguments: dict[str, object]) -> PolicyResult:
        """对一次工具调用给出最终判定（DENY > ASK > ALLOW，取最严）。

        零规则默认：**默认放行（ALLOW）**——门禁是"额外加一道锁"，不是默认锁死；
        没有配置任何策略就等于没有任何限制（与 build_agent 的默认策略一致）。
        这个默认是刻意固定的，见 test_零规则_默认放行。

        逐规则隔离：某条规则自身抛异常（写错了/依赖坏了）不能击穿整个门禁。
        异常规则按**最严处理（DENY）**——门禁是安全组件，"判定不出来"绝不等价于
        "放行"（fail-closed）；同时记日志，方便定位是哪条规则坏了。
        """
        if not self._policies:
            return PolicyResult(Decision.ALLOW, "没有配置任何策略，默认放行")
        # 严格度排序：DENY(2) > ASK(1) > ALLOW(0)
        rank = {Decision.ALLOW: 0, Decision.ASK: 1, Decision.DENY: 2}
        best: PolicyResult = PolicyResult(Decision.ALLOW)
        for policy in self._policies:
            try:
                result = policy(tool_name, arguments)
            except Exception as e:  # noqa: BLE001 - 单条规则故障必须隔离，不能击穿门禁
                logger.warning(
                    "策略规则执行异常，按最严(DENY)处理: %s", e, exc_info=True)
                result = PolicyResult(
                    Decision.DENY,
                    f"策略规则执行异常: {type(e).__name__}: {e}",
                )
            if rank[result.decision] > rank[best.decision]:
                best = result
        return best


# ---- 一些常用的现成 policy ----

def _path_parts(path: str) -> tuple[str, ...]:
    """把路径拆成组件（同时兼容 / 与 \\），忽略空段与 `.`。

    Windows 大小写不敏感（`C:\\Users` 与 `c:/users` 视为同一路径）。
    """
    parts = tuple(p for p in re.split(r"[\\/]+", path) if p not in ("", "."))
    if os.name == "nt":
        return tuple(p.casefold() for p in parts)
    return parts


def deny_when_path_in_protected(
    protected_prefixes: tuple[str, ...],
) -> Policy:
    """禁用：工具参数里的路径**位于**受保护目录(如 C盘/根目录)之下时就拒绝。

    路径判定按**组件**比较，而不是 `startswith` 字符串前缀——否则
    `/data-evil` 会被误判成命中 `/data`（前缀相同但不是子目录）。
    """
    def policy(tool_name: str, arguments: dict[str, object]) -> PolicyResult:
        raw = str(arguments.get("path", ""))
        if not raw:
            return PolicyResult(Decision.ALLOW)
        parts = _path_parts(raw)
        for prefix in protected_prefixes:
            pre = _path_parts(prefix)
            if pre and len(parts) >= len(pre) and parts[: len(pre)] == pre:
                return PolicyResult(Decision.DENY, f"禁止访问受保护路径: {raw}")
        return PolicyResult(Decision.ALLOW)
    return policy


def ask_when_tool_in(askable: frozenset[str]) -> Policy:
    """询问：属于指定动作集合的工具需要用户拍板。"""
    def policy(tool_name: str, arguments: dict[str, object]) -> PolicyResult:
        if tool_name in askable:
            return PolicyResult(Decision.ASK, f"动作 {tool_name!r} 需要人工批准")
        return PolicyResult(Decision.ALLOW)
    return policy
