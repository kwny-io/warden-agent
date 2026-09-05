"""记忆存储抽象与内存实现。

  - save / find_by_ref / find_authorized / latest / search
  - conflicts：同一引用下不同版本的记忆（冲突消解靠版本号 + 状态）。
  - tombstones：软删除标记，配合 purge 才真正清除。
  - expiry：按 expires_at 判定，purge 时物理清理。
  - audit 保存在 MemoryItem 内部。
"""

from __future__ import annotations

import threading
from typing import Protocol

from warden_agent.memory.models import (
    MemoryItem,
    MemoryScope,
    MemoryStatus,
    make_ref,
)


class MemoryRepository(Protocol):
    """任何"能存记忆"的存储都要实现这个接口。"""

    def save(self, item: MemoryItem) -> None: ...
    def find(self, uid: str) -> MemoryItem | None: ...
    def find_ref(self, scope: MemoryScope, key: str) -> list[MemoryItem]: ...
    def latest(self, scope: MemoryScope, key: str) -> MemoryItem | None: ...
    def search(
        self, scope: MemoryScope, text_like: str | None = None, limit: int = 20
    ) -> list[MemoryItem]: ...


class InMemoryMemoryStore:
    """进程内记忆库。含写锁（可换到 SqliteStore 同款接口）。"""

    def __init__(self) -> None:
        self._items: dict[str, MemoryItem] = {}
        self._lock = threading.Lock()

    def save(self, item: MemoryItem) -> None:
        with self._lock:
            self._items[item.uid] = item

    def find(self, uid: str) -> MemoryItem | None:
        return self._items.get(uid)

    def find_ref(self, scope: MemoryScope, key: str) -> list[MemoryItem]:
        ref = make_ref(scope, key)
        return [i for i in self._items.values() if make_ref(i.scope, i.key) == ref]

    def latest(self, scope: MemoryScope, key: str) -> MemoryItem | None:
        items = self.find_ref(scope, key)
        if not items:
            return None
        # 按 updated_at 取最新，排除已过期/已删除的「优先返回有效项」
        active = [i for i in items if i.status in (MemoryStatus.ACTIVE, MemoryStatus.PENDING)]
        candidates = active or items
        return max(candidates, key=lambda i: i.updated_at)

    def search(
        self,
        scope: MemoryScope,
        text_like: str | None = None,
        limit: int = 20,
    ) -> list[MemoryItem]:
        items = [
            i for i in self._items.values()
            if i.scope == scope and i.status == MemoryStatus.ACTIVE
        ]
        if text_like:
            items = [i for i in items if text_like.lower() in i.content.text.lower()]
        items.sort(key=lambda i: i.updated_at, reverse=True)
        return items[:limit]
