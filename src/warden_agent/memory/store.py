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

import contextlib
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
    """任何"能存记忆"的存储都要实现这个接口。

    `owner` 是**归属者过滤**（用户 id）：
      - 传字符串 → 只返回该归属者的记忆（严格隔离，这是产品路径的用法）；
      - 传 None   → 不过滤（管理/清理/测试用；不要用在面向用户的读路径上）。
    """

    def save(self, item: MemoryItem) -> None: ...
    def find(self, uid: str) -> MemoryItem | None: ...
    def find_ref(
        self, scope: MemoryScope, key: str, owner: str | None = None
    ) -> list[MemoryItem]: ...
    def latest(
        self, scope: MemoryScope, key: str, owner: str | None = None
    ) -> MemoryItem | None: ...
    def search(
        self, scope: MemoryScope, text_like: str | None = None, limit: int = 20,
        owner: str | None = None,
    ) -> list[MemoryItem]: ...


def _owned(item: MemoryItem, owner: str | None) -> bool:
    """归属过滤：owner=None 放行全部；否则要求精确匹配。"""
    return owner is None or item.owner == owner


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

    def find_ref(
        self, scope: MemoryScope, key: str, owner: str | None = None
    ) -> list[MemoryItem]:
        ref = make_ref(scope, key)
        return [
            i for i in self._items.values()
            if make_ref(i.scope, i.key) == ref and _owned(i, owner)
        ]

    def latest(
        self, scope: MemoryScope, key: str, owner: str | None = None
    ) -> MemoryItem | None:
        items = self.find_ref(scope, key, owner)
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
        owner: str | None = None,
    ) -> list[MemoryItem]:
        items = [
            i for i in self._items.values()
            if i.scope == scope and i.status == MemoryStatus.ACTIVE and _owned(i, owner)
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
                audit          TEXT NOT NULL,   -- JSON 数组
                owner          TEXT NOT NULL DEFAULT ''   -- 归属者（用户 id）；''=部署级共享
            );
            CREATE INDEX IF NOT EXISTS idx_memories_ref ON memories (scope, key);
            CREATE INDEX IF NOT EXISTS idx_memories_status ON memories (scope, status);
            """
        )
        self._conn.commit()
        # 老库补列（迁移）：`CREATE TABLE IF NOT EXISTS` 不会给已存在的表加列，所以要显式 ALTER；
        # 重复加列会报错（列已存在），忽略即可（幂等）。
        # ⚠️ 索引必须在 ALTER **之后单独建**：旧库上若把 `CREATE INDEX ... (owner, scope)` 放进上面的
        #    脚本，会在"列还不存在"时先报错；而放进同一个 try 里则 ALTER 一失败就跳过了索引。
        with contextlib.suppress(sqlite3.OperationalError):
            self._conn.execute(
                "ALTER TABLE memories ADD COLUMN owner TEXT NOT NULL DEFAULT ''"
            )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_owner ON memories (owner, scope)"
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
            # owner 列在迁移里后加，老行读出来是 ''；行长度不足时按 '' 处理
            owner=str(row[16]) if len(row) > 16 and row[16] is not None else "",
        )

    # ---- 接口实现 ----
    def save(self, item: MemoryItem) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO memories (uid, scope, key, content_kind, content_text,"
                " content_data, status, actor_kind, actor_id, version, created_at,"
                " updated_at, expires_at, conflicts_with, supersedes, audit, owner)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(uid) DO UPDATE SET"
                " scope=excluded.scope, key=excluded.key,"
                " content_kind=excluded.content_kind, content_text=excluded.content_text,"
                " content_data=excluded.content_data, status=excluded.status,"
                " actor_kind=excluded.actor_kind, actor_id=excluded.actor_id,"
                " version=excluded.version, updated_at=excluded.updated_at,"
                " expires_at=excluded.expires_at, conflicts_with=excluded.conflicts_with,"
                " supersedes=excluded.supersedes, audit=excluded.audit,"
                " owner=excluded.owner",
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
                    item.owner,
                ),
            )
            self._conn.commit()

    def find(self, uid: str) -> MemoryItem | None:
        row = self._conn.execute(
            "SELECT * FROM memories WHERE uid = ?", (uid,)
        ).fetchone()
        return None if row is None else self._row_to_item(row)

    def find_ref(
        self, scope: MemoryScope, key: str, owner: str | None = None
    ) -> list[MemoryItem]:
        sql = "SELECT * FROM memories WHERE scope = ? AND key = ?"
        params: list[Any] = [scope.name, key]
        if owner is not None:
            sql += " AND owner = ?"
            params.append(owner)
        rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [self._row_to_item(r) for r in rows]

    def latest(
        self, scope: MemoryScope, key: str, owner: str | None = None
    ) -> MemoryItem | None:
        """与内存版语义一致：优先返回有效项（ACTIVE / PENDING），取 updated_at 最新。"""
        items = self.find_ref(scope, key, owner)
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
        owner: str | None = None,
    ) -> list[MemoryItem]:
        sql = "SELECT * FROM memories WHERE scope = ? AND status = ?"
        params: list[Any] = [scope.name, MemoryStatus.ACTIVE.name]
        if owner is not None:
            sql += " AND owner = ?"
            params.append(owner)
        sql += " ORDER BY updated_at DESC"
        rows = self._conn.execute(sql, tuple(params)).fetchall()
        items = [self._row_to_item(r) for r in rows]
        if text_like:
            needle = text_like.lower()
            items = [i for i in items if needle in i.content.text.lower()]
        return items[:limit]

    def close(self) -> None:
        self._conn.close()


class PostgresMemoryStore:
    """落 PG 的记忆库：实现 `MemoryRepository`，多副本共享同一份记忆。

    为什么需要它：`SqliteMemoryStore` 是单机文件——多副本部署时每个副本一份记忆，
    `USER` 作用域（"跨会话的用户级记忆"）会在副本 A 记得、副本 B 读不到，语义直接崩掉。
    企业级多副本交付必须让记忆也进共享库。

    行结构与 `SqliteMemoryStore` 完全对齐（同列、同序列化），SQL 占位符换成 `%s`。
    构造：`conn` 给一条 psycopg 连接（run_server 用 `store.new_connection()` 另开），
    或给 `connect_kwargs`。
    """

    def __init__(
        self,
        conn: Any = None,
        *,
        connect_kwargs: Mapping[str, Any] | None = None,
    ) -> None:
        if conn is not None:
            self._conn = conn
        elif connect_kwargs is not None:
            import psycopg

            self._conn = psycopg.connect(**connect_kwargs, autocommit=True)
        else:
            raise ValueError("PostgresMemoryStore 需要 conn 或 connect_kwargs")
        self._lock = threading.Lock()
        self._init()

    def _init(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    uid            TEXT PRIMARY KEY,
                    scope          TEXT NOT NULL,
                    key            TEXT NOT NULL,
                    content_kind   TEXT NOT NULL,
                    content_text   TEXT NOT NULL,
                    content_data   TEXT NOT NULL,
                    status         TEXT NOT NULL,
                    actor_kind     TEXT NOT NULL,
                    actor_id       TEXT NOT NULL,
                    version        INTEGER NOT NULL,
                    created_at     TEXT NOT NULL,
                    updated_at     TEXT NOT NULL,
                    expires_at     TEXT,
                    conflicts_with TEXT,
                    supersedes     TEXT,
                    audit          TEXT NOT NULL,
                    owner          TEXT NOT NULL DEFAULT ''
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_ref ON memories (scope, key)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_status ON memories (scope, status)"
            )
            # 老库补列（与 SQLite 版对齐）
            cur.execute(
                "ALTER TABLE memories ADD COLUMN IF NOT EXISTS owner TEXT NOT NULL DEFAULT ''"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_owner ON memories (owner, scope)"
            )
        self._conn.commit()

    def save(self, item: MemoryItem) -> None:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO memories (uid, scope, key, content_kind, content_text,"
                " content_data, status, actor_kind, actor_id, version, created_at,"
                " updated_at, expires_at, conflicts_with, supersedes, audit, owner)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (uid) DO UPDATE SET"
                " scope=EXCLUDED.scope, key=EXCLUDED.key,"
                " content_kind=EXCLUDED.content_kind, content_text=EXCLUDED.content_text,"
                " content_data=EXCLUDED.content_data, status=EXCLUDED.status,"
                " actor_kind=EXCLUDED.actor_kind, actor_id=EXCLUDED.actor_id,"
                " version=EXCLUDED.version, updated_at=EXCLUDED.updated_at,"
                " expires_at=EXCLUDED.expires_at, conflicts_with=EXCLUDED.conflicts_with,"
                " supersedes=EXCLUDED.supersedes, audit=EXCLUDED.audit,"
                " owner=EXCLUDED.owner",
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
                    SqliteMemoryStore._iso(item.expires_at),
                    item.conflicts_with,
                    item.supersedes,
                    json.dumps(_audit_to_json(item.audit), ensure_ascii=False),
                    item.owner,
                ),
            )
            self._conn.commit()

    def find(self, uid: str) -> MemoryItem | None:
        with self._conn.cursor() as cur:
            cur.execute("SELECT * FROM memories WHERE uid = %s", (uid,))
            row = cur.fetchone()
        return None if row is None else SqliteMemoryStore._row_to_item(row)

    def find_ref(
        self, scope: MemoryScope, key: str, owner: str | None = None
    ) -> list[MemoryItem]:
        sql = "SELECT * FROM memories WHERE scope = %s AND key = %s"
        params: list[Any] = [scope.name, key]
        if owner is not None:
            sql += " AND owner = %s"
            params.append(owner)
        with self._conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
        return [SqliteMemoryStore._row_to_item(r) for r in rows]

    def latest(
        self, scope: MemoryScope, key: str, owner: str | None = None
    ) -> MemoryItem | None:
        items = self.find_ref(scope, key, owner)
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
        owner: str | None = None,
    ) -> list[MemoryItem]:
        sql = "SELECT * FROM memories WHERE scope = %s AND status = %s"
        params: list[Any] = [scope.name, MemoryStatus.ACTIVE.name]
        if owner is not None:
            sql += " AND owner = %s"
            params.append(owner)
        sql += " ORDER BY updated_at DESC"
        with self._conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
        items = [SqliteMemoryStore._row_to_item(r) for r in rows]
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
