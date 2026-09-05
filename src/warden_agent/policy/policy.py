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

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto


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


class PolicyEngine:
    """把多条 policy 合成一道门禁。取最严格的判定。"""

    def __init__(self) -> None:
        self._policies: list[Policy] = []

    def add(self, policy: Policy) -> None:
        self._policies.append(policy)

    def evaluate(self, tool_name: str, arguments: dict[str, object]) -> PolicyResult:
        """对一次工具调用给出最终判定（DENY > ASK > ALLOW，取最严）。"""
        if not self._policies:
            return PolicyResult(Decision.ALLOW, "没有配置任何策略，默认放行")
        # 严格度排序：DENY(2) > ASK(1) > ALLOW(0)
        rank = {Decision.ALLOW: 0, Decision.ASK: 1, Decision.DENY: 2}
        best: PolicyResult = PolicyResult(Decision.ALLOW)
        for policy in self._policies:
            result = policy(tool_name, arguments)
            if rank[result.decision] > rank[best.decision]:
                best = result
        return best


# ---- 一些常用的现成 policy ----

def deny_when_path_in_protected(
    protected_prefixes: tuple[str, ...],
) -> Policy:
    """禁用：工具参数里的路径属于受保护目录(如 C盘/根目录)时就拒绝。"""
    def policy(tool_name: str, arguments: dict[str, object]) -> PolicyResult:
        path = str(arguments.get("path", ""))
        for prefix in protected_prefixes:
            if path.startswith(prefix):
                return PolicyResult(Decision.DENY, f"禁止访问受保护路径: {path}")
        return PolicyResult(Decision.ALLOW)
    return policy


def ask_when_tool_in(askable: frozenset[str]) -> Policy:
    """询问：属于指定动作集合的工具需要用户拍板。"""
    def policy(tool_name: str, arguments: dict[str, object]) -> PolicyResult:
        if tool_name in askable:
            return PolicyResult(Decision.ASK, f"动作 {tool_name!r} 需要人工批准")
        return PolicyResult(Decision.ALLOW)
    return policy
