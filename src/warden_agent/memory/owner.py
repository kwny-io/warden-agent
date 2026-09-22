"""当前"记忆归属者"（用户 id）的上下文。

为什么需要它：`memory.remember` / `memory.recall` 这两张技能卡是**装配期一次性建好**的
（工具目录全局共享、被所有会话所有用户复用），工具函数拿不到"当前是谁在用"这个运行期信息。
而记忆**必须带归属**——否则 scope 只区分"哪一类"（RUN/SESSION/USER/WORKSPACE）、不区分"谁的"，
同一个 SESSION 作用域是全部署共享的：A 写的记忆会被所有人的对话召回（跨租户投毒），
`/memory/{scope}` 也能一次读到全租户的记忆。

所以会话在驱动时把 run 的归属者放进 ContextVar，工具执行时读出来——不必为每个用户重建目录。
（与链路追踪用 ContextVar 承载 trace 上下文是同一个理由：运行期的"当前上下文"。）

**归属者的唯一来源是会话/凭证（`run.user_id`）**，绝不来自请求参数或工具入参。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_current_owner: ContextVar[str] = ContextVar("warden_memory_owner", default="")


def current_owner() -> str:
    """当前会话的归属者（未设置时为空串 = 部署级共享）。"""
    return _current_owner.get()


@contextmanager
def owner_scope(owner: str | None) -> Iterator[None]:
    """在这段作用域内把"当前归属者"设为 `owner`，结束后还原。"""
    token = _current_owner.set(owner or "")
    try:
        yield
    finally:
        _current_owner.reset(token)
