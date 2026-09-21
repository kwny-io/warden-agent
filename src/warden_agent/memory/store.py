"""记忆存储抽象与两种实现。

  - save / find / find_ref / latest / search
  - conflicts：同一引用下不同版本的记忆（冲突消解靠版本号 + 状态）。
  - tombstones：软删除标记，配合 purge 才真正清除。
  - expiry：按 expires_at 判定，purge 时物理清理。
  - audit 保存在 MemoryItem 内部。

两种实现：
  - `InMemoryMemoryStore`：进程内，单副本/测试用（重启即丢）。
  - `SqliteMemoryStore`：落盘，**跨会话的用户级记忆才真的记得住**（产品的默认选择）。
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from warden_agent.memory.models import (
    MemoryActor,
    MemoryAuditEvent,
    MemoryContent,
    MemoryItem,
    MemoryKind,
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


class SqliteMemoryStore:
    """落盘的记忆库：用 SQLite 实现 `MemoryRepository`，**重启不丢、可跨会话**。

    为什么需要它：`InMemoryMemoryStore` 进程一退记忆就没了——那意味着 `USER` 作用域
    （"跨会话的用户级记忆"）在语义上成立、在实现上落空。产品的默认记忆库应该是这个。

    与 `SqliteStore`（Run/对话）共用同一个 db 文件但**各自持有连接**（同 `SqliteAuditStore`
    的做法）：记忆只依赖 `MemoryRepository` 接口，不必反过来耦合 RunStore。
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        if conn is not None:
            self._conn = conn
        elif db_path is not None:
            self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        else:
            raise ValueError("SqliteMemoryStore 需要 db_path 或 conn")
        self._lock = threading.Lock()
        self._init()

    def _init(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS memories (
                uid            TEXT PRIMARY KEY,
                scope          TEXT NOT NULL,
                key            TEXT NOT NULL,
                content_kind   TEXT NOT NULL,
                content_text   TEXT NOT NULL,
                content_data   TEXT NOT NULL,   -- JSON
                status         TEXT NOT NULL,
                actor_kind     TEXT NOT NULL,
                actor_id       TEXT NOT NULL,
                version        INTEGER NOT NULL,
                created_at     TEXT NOT NULL,
                updated_at     TEXT NOT NULL,
                expires_at     TEXT,
                conflicts_with TEXT,
                supersedes     TEXT,
                audit          TEXT NOT NULL    -- JSON 数组
            );
            CREATE INDEX IF NOT EXISTS idx_memories_ref ON memories (scope, key);
            CREATE INDEX IF NOT EXISTS idx_memories_status ON memories (scope, status);
            """
        )
        self._conn.commit()

    # ---- 序列化 ----
    @staticmethod
    def _iso(value: _dt.datetime | None) -> str | None:
        return None if value is None else value.isoformat()

    @staticmethod
    def _parse_iso(value: Any) -> _dt.datetime | None:
        if value is None:
            return None
        try:
            return _dt.datetime.fromisoformat(str(value))
        except ValueError:
            return None

    @staticmethod
    def _row_to_item(row: tuple[Any, ...]) -> MemoryItem:
        return MemoryItem(
            uid=str(row[0]),
            scope=MemoryScope[str(row[1])],
            key=str(row[2]),
            content=MemoryContent(
                kind=MemoryKind[str(row[3])],
                text=str(row[4]),
                data=_json_dict(row[5]),
            ),
            status=MemoryStatus[str(row[6])],
            actor=MemoryActor(kind=str(row[7]), id=str(row[8])),
            version=int(row[9]),
            created_at=SqliteMemoryStore._parse_iso(row[10]) or _now(),
            updated_at=SqliteMemoryStore._parse_iso(row[11]) or _now(),
            expires_at=SqliteMemoryStore._parse_iso(row[12]),
            conflicts_with=None if row[13] is None else str(row[13]),
            supersedes=None if row[14] is None else str(row[14]),
            audit=_audit_from_json(row[15]),
        )

    # ---- 接口实现 ----
    def save(self, item: MemoryItem) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO memories (uid, scope, key, content_kind, content_text,"
                " content_data, status, actor_kind, actor_id, version, created_at,"
                " updated_at, expires_at, conflicts_with, supersedes, audit)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(uid) DO UPDATE SET"
                " scope=excluded.scope, key=excluded.key,"
                " content_kind=excluded.content_kind, content_text=excluded.content_text,"
                " content_data=excluded.content_data, status=excluded.status,"
                " actor_kind=excluded.actor_kind, actor_id=excluded.actor_id,"
                " version=excluded.version, updated_at=excluded.updated_at,"
                " expires_at=excluded.expires_at, conflicts_with=excluded.conflicts_with,"
                " supersedes=excluded.supersedes, audit=excluded.audit",
                (
                    item.uid,
                    item.scope.name,
                    item.key,
                    item.content.kind.name,
                    item.content.text,
                    json.dumps(item.content.data, ensure_ascii=False),
                    item.status.name,
                    item.actor.kind,
                    item.actor.id,
                    item.version,
                    item.created_at.isoformat(),
                    item.updated_at.isoformat(),
                    self._iso(item.expires_at),
                    item.conflicts_with,
                    item.supersedes,
                    json.dumps(_audit_to_json(item.audit), ensure_ascii=False),
                ),
            )
            self._conn.commit()

    def find(self, uid: str) -> MemoryItem | None:
        row = self._conn.execute(
            "SELECT * FROM memories WHERE uid = ?", (uid,)
        ).fetchone()
        return None if row is None else self._row_to_item(row)

    def find_ref(self, scope: MemoryScope, key: str) -> list[MemoryItem]:
        rows = self._conn.execute(
            "SELECT * FROM memories WHERE scope = ? AND key = ?",
            (scope.name, key),
        ).fetchall()
        return [self._row_to_item(r) for r in rows]

    def latest(self, scope: MemoryScope, key: str) -> MemoryItem | None:
        """与内存版语义一致：优先返回有效项（ACTIVE / PENDING），取 updated_at 最新。"""
        items = self.find_ref(scope, key)
        if not items:
            return None
        active = [
            i for i in items
            if i.status in (MemoryStatus.ACTIVE, MemoryStatus.PENDING)
        ]
        candidates = active or items
        return max(candidates, key=lambda i: i.updated_at)

    def search(
        self,
        scope: MemoryScope,
        text_like: str | None = None,
        limit: int = 20,
    ) -> list[MemoryItem]:
        rows = self._conn.execute(
            "SELECT * FROM memories WHERE scope = ? AND status = ?"
            " ORDER BY updated_at DESC",
            (scope.name, MemoryStatus.ACTIVE.name),
        ).fetchall()
        items = [self._row_to_item(r) for r in rows]
        if text_like:
            needle = text_like.lower()
            items = [i for i in items if needle in i.content.text.lower()]
        return items[:limit]

    def close(self) -> None:
        self._conn.close()


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC)


def _json_dict(raw: Any) -> dict[str, Any]:
    try:
        data = json.loads(str(raw))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _audit_to_json(events: list[MemoryAuditEvent]) -> list[dict[str, str]]:
    return [
        {
            "at": e.at.isoformat(),
            "actor_kind": e.actor.kind,
            "actor_id": e.actor.id,
            "action": e.action,
        }
        for e in events
    ]


def _audit_from_json(raw: Any) -> list[MemoryAuditEvent]:
    try:
        rows = json.loads(str(raw))
    except json.JSONDecodeError:
        return []
    if not isinstance(rows, list):
        return []
    events: list[MemoryAuditEvent] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        try:
            at = _dt.datetime.fromisoformat(str(row.get("at")))
        except ValueError:
            at = _now()
        events.append(MemoryAuditEvent(
            at=at,
            actor=MemoryActor(
                kind=str(row.get("actor_kind", "system")),
                id=str(row.get("actor_id", "system")),
            ),
            action=str(row.get("action", "")),
        ))
    return events
