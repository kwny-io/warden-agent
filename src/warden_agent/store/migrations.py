"""数据库迁移（Migration）——让 schema 像软件一样可版本化演进。

  - 每张"版本化结构变更"都是一个 Migration（向前应用，不可修改历史）。
  - 数据库里存一个 __migrations__ 表记录"已应用到第几版"。
  - 启动时从当前版本一路往上跑到最新版，之后新增迁移只加新编号，不碰旧代码。

为什么要这个：
  之前 SqliteStore 用 `CREATE TABLE IF NOT EXISTS ...` 裸建表。
  这有个致命问题——"加一列"没法表达：表已经存在时 IF NOT EXISTS 什么都不做，
  老库永远不会长出新列，而你也不能删了重建（会丢数据）。
  迁移体系让"改表结构但不丢数据"成为可能，这正是数据库版本管理的意义。

设计：
  - Migration 是一个可调用对象：(conn) -> None，负责把 schema 从 N-1 带到 N。
  - migrate(conn, current_version) 返回应用后的新版本；幂等、逐版本提交。
  - 我们用 userspace PRAGMA user_version 之外再加一张表，纯 SQL、与具体 Store 解耦。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

Migration = Callable[[sqlite3.Connection], None]

_MIGRATIONS_TABLE = "__migrations__"


@dataclass(frozen=True)
class MigrationRecord:
    version: int
    name: str
    apply: Migration


def version_of(conn: sqlite3.Connection) -> int:
    """读当前已应用到第几版。没有迁移表视为 0（全新库 / 老库）。"""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (_MIGRATIONS_TABLE,),
    ).fetchone()
    if row is None:
        return 0
    r = conn.execute(
        f"SELECT MAX(version) FROM {_MIGRATIONS_TABLE}"
    ).fetchone()
    return int(r[0]) if r and r[0] is not None else 0


def _ensure_record_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_MIGRATIONS_TABLE} (
            version INTEGER PRIMARY KEY,
            name     TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )


def record_migration(
    conn: sqlite3.Connection, version: int, name: str, now: str
) -> None:
    _ensure_record_table(conn)
    conn.execute(
        f"INSERT INTO {_MIGRATIONS_TABLE} (version, name, applied_at) VALUES (?, ?, ?)",
        (version, name, now),
    )


def migrate(
    conn: sqlite3.Connection,
    migrations: list[MigrationRecord],
) -> int:
    """把连接从当前版本按序迁移到最新版，返回新版本号。幂等、逐版本提交。

    - 每个迁移在独立事务里提交：某一步失败，之前的已生效，不会出现半成品。
    - 历史版本只准新增、不准修改（改了会破坏已就位的老库）。
    """
    current = version_of(conn)
    target = _migrations_by_version(migrations)
    for version, (name, fn) in sorted(target.items()):
        if version <= current:
            continue
        conn.execute("BEGIN")
        try:
            fn(conn)
            _ensure_record_table(conn)
            record_migration(conn, version, name, "now")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return version_of(conn)


def _migrations_by_version(
    records: list[MigrationRecord],
) -> dict[int, tuple[str, Migration]]:
    result: dict[int, tuple[str, Migration]] = {}
    for record in records:
        result[record.version] = (record.name, record.apply)
    return result
