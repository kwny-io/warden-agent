"""记忆模型与存储 —— 让 Agent 真的"记得"事，而不是每条对话都被忘掉。

  - MemoryScope / MemoryScopeType ：按作用域分（Run 一次任务 / Session 一次会话 / User 用户）。
  - MemoryKind / MemoryStatus    ：记忆的类型与生命周期状态。
  - MemoryContent (Text)         ：记忆内容。
  - MemoryActor / audit          ：谁写的、什么时候、审计轨迹。
  - MemoryRef / version / tombstone：冲突消解与删除（软删除）。
  - candidate 流                 ：先"提议"再"人工确认"，不轻易污染记忆。

白话解释：
  RAG 是"查外部资料"（知识库），Memory 是"记住这轮干的事"（比如用户偏好、
  上次的结论、某个 Agent 调研到一半的进度）。两者不一样：
    - RAG：模型问"XX 是什么"，去文档里找。
    - Memory：模型记得"用户上次说过他喜欢简洁"，这轮就不用再问。
"""
from __future__ import annotations

import datetime as _dt
import secrets
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any


class MemoryScope(Enum):
    RUN = auto()       # 单次任务内有效
    SESSION = auto()   # 一次会话内共享
    USER = auto()      # 跨会话的用户级记忆
    WORKSPACE = auto() # 一次协作工作区内共享（多 Agent 共用的"工作记忆"）


class MemoryKind(Enum):
    TEXT = auto()     # 普通文本事实
    STRUCTURED = auto()  # 结构化条目
    DERIVED = auto()  # 派生（比如总结）


class MemoryStatus(Enum):
    ACTIVE = auto()       # 有效
    PENDING = auto()      # 候选，等确认
    CONFLICTED = auto()   # 与已有记忆冲突
    TOMBSTONED = auto()   # 软删除
    EXPIRED = auto()      # 过期


@dataclass(frozen=True)
class MemoryContent:
    """记忆内容。Text：一段文本；structured：dict。"""

    kind: MemoryKind = MemoryKind.TEXT
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryActor:
    """谁写的这条记忆（审计用）。"""

    kind: str = "system"
    id: str = "system"


@dataclass(frozen=True)
class MemoryAuditEvent:
    """一条审计轨迹。"""

    at: _dt.datetime
    actor: MemoryActor
    action: str  # create / propose / approve / reject / expire / purge


@dataclass
class MemoryItem:
    """一条记忆。含版本（冲突消解）、状态（软删除/冲突/过期）、审计。"""

    # 唯一引用（用 scope + key 派生，或用 uid）
    uid: str
    scope: MemoryScope
    key: str
    content: MemoryContent
    status: MemoryStatus = MemoryStatus.ACTIVE
    actor: MemoryActor = MemoryActor()
    version: int = 1
    created_at: _dt.datetime = field(default_factory=lambda: _dt.datetime.now(_dt.UTC))
    updated_at: _dt.datetime = field(default_factory=lambda: _dt.datetime.now(_dt.UTC))
    expires_at: _dt.datetime | None = None
    # 冲突消解
    conflicts_with: str | None = None
    supersedes: str | None = None  # 这条替代了哪条
    audit: list[MemoryAuditEvent] = field(default_factory=list)

    @property
    def is_active(self) -> bool:
        return self.status == MemoryStatus.ACTIVE

    def record(self, action: str, actor: MemoryActor | None = None) -> None:
        self.audit.append(MemoryAuditEvent(
            at=_dt.datetime.now(_dt.UTC),
            actor=actor or self.actor,
            action=action,
        ))


def make_ref(scope: MemoryScope, key: str) -> str:
    """记忆引用：scope:key，作为稳定坐标。"""
    return f"{scope.name}:{key}"


def new_uid() -> str:
    return secrets.token_hex(8)
