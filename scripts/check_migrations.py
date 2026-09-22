#!/usr/bin/env python3
"""Schema 迁移检查：把"加了表/列却忘了升版本"从"上线后才发现"提前到 CI。

背景（交付面缺口 item 5）：
  item 4 把 `store/migrations.py` 删掉后，迁移改成**显式 schema 版本**——
  `SqliteStore._SCHEMA_VERSION` / `PostgresStore._SCHEMA_VERSION` + 启动时补列，
  并把目标版本写进 `__schema_version__` 单行表。这套机制**没有守门人**：
  谁往 `_init_schema` 加了一张表却忘了 `_SCHEMA_VERSION += 1`，启动照旧成功、
  老库却停在上一个版本，于是"记录版本"和"真实结构"悄悄分叉。

这个脚本就是那个守门人，做三件事：
  1. 真起一个新库（SQLite；PG 走 CI 的 service）→ 读出**真实结构指纹**（表 → 列集合）。
  2. 断言新库记录的版本 == 目标常量，且 SQLite 与 PG 两个常量一致（两后端必须对齐）。
  3. 把真实结构指纹和签入的快照 `scripts/schema_snapshot.json` 对比：
     - 结构变了、版本却没超过快照版本 → **失败**（"schema 变更未伴随版本 +1"）；
     - 版本升了但快照没更新 → 也失败（拿新指纹重写快照即可，见 --write）。

用法：
    uv run --frozen python scripts/check_migrations.py --backend sqlite
    WARDEN_TEST_PG_HOST=localhost uv run --frozen python scripts/check_migrations.py --backend postgres
    uv run --frozen python scripts/check_migrations.py --write   # 维护者：有意的 schema 变更后重写快照

退出码：0 一致；1 有分叉（并打印可执行的修复提示）。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "scripts" / "schema_snapshot.json"
# SQLite 自带 sqlite_sequence/sqlite_master 等内部表，不属于业务 schema，指纹里排除。
_IGNORED_PREFIXES = ("sqlite_", "pg_")


def _sqlite_tables(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """布尔指纹：表名 → 排好序的列名。排除 SQLite 内部表。"""
    names = [
        str(r[0])
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    ]
    out: dict[str, list[str]] = {}
    for name in sorted(names):
        if name.startswith(_IGNORED_PREFIXES):
            continue
        cols = [
            str(r[1]) for r in conn.execute("SELECT * FROM pragma_table_info(?)", (name,))
        ]
        out[name] = sorted(cols)
    return out


def read_sqlite() -> tuple[int, dict[str, list[str]]]:
    """起一个全新的 SQLite 库，读回 (记录的 schema 版本, 结构指纹)。"""
    from warden_agent.store.sqlite import SqliteStore

    db_path = Path(tempfile.mkdtemp()) / "fresh.db"
    store = SqliteStore(db_path)
    try:
        version = store.schema_version()
        tables = _sqlite_tables(store.conn)
    finally:
        store.close()
    return version, tables


def _pg_params() -> dict[str, Any]:
    """连接参数沿用测试约定（test_postgres_integration.py），可用环境变量覆盖（给 CI）。"""
    return {
        "host": os.environ.get("WARDEN_TEST_PG_HOST", "localhost"),
        "port": int(os.environ.get("WARDEN_TEST_PG_PORT", "5432")),
        "dbname": os.environ.get("WARDEN_TEST_PG_DB", "warden"),
        "user": os.environ.get("WARDEN_TEST_PG_USER", "postgres"),
        "password": os.environ.get("WARDEN_TEST_PG_PASSWORD", ""),
    }


def read_postgres() -> tuple[int, dict[str, list[str]]]:
    """连上 CI 的 PG service，起 store 建表，读回 (记录版本, 结构指纹)。

    用 information_schema 而不是 pg_catalog：它跨版本稳定，且和 SQLite 的
    pragma_table_info 语义一致（都只给"表 → 列"），两个后端的指纹才能直接对比。
    """
    from warden_agent.store.postgres import PostgresStore

    store = PostgresStore(**_pg_params())
    try:
        version = store.schema_version()
        with store.conn.cursor() as cur:
            cur.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' ORDER BY table_name, column_name"
            )
            rows = cur.fetchall()
    finally:
        store.close()
    tables: dict[str, list[str]] = {}
    for table_name, column_name in rows:
        name = str(table_name)
        if name.startswith(_IGNORED_PREFIXES):
            continue
        tables.setdefault(name, []).append(str(column_name))
    return version, {t: sorted(cs) for t, cs in tables.items()}


def load_snapshot() -> dict[str, Any]:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def write_snapshot(version: int, tables: dict[str, list[str]]) -> None:
    payload = {"version": version, "tables": tables}
    SNAPSHOT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"已重写快照：{SNAPSHOT}")


def _diff(expected: dict[str, list[str]], actual: dict[str, list[str]]) -> list[str]:
    """打印人话版结构差异（新增/删除的表与列）。"""
    lines: list[str] = []
    for name in sorted(set(actual) - set(expected)):
        lines.append(f"  + 新增表 {name}: {actual[name]}")
    for name in sorted(set(expected) - set(actual)):
        lines.append(f"  - 删除表 {name}: {expected[name]}")
    for name in sorted(set(expected) & set(actual)):
        added = sorted(set(actual[name]) - set(expected[name]))
        removed = sorted(set(expected[name]) - set(actual[name]))
        if added:
            lines.append(f"  + 表 {name} 新增列 {added}")
        if removed:
            lines.append(f"  - 表 {name} 删除列 {removed}")
    return lines


def verify(backend: str) -> list[str]:
    """返回问题列表（空 = 通过）。不直接抛，方便测试与汇总打印。"""
    from warden_agent.store.postgres import PostgresStore
    from warden_agent.store.sqlite import SqliteStore

    problems: list[str] = []
    snapshot = load_snapshot()
    snap_version = int(snapshot["version"])
    snap_tables = snapshot["tables"]

    # 两个后端的目标版本必须一致——否则"换个存储"就换了 schema 版本，上层无从判断。
    if SqliteStore._SCHEMA_VERSION != PostgresStore._SCHEMA_VERSION:
        problems.append(
            "SQLite 与 PostgreSQL 的目标版本不一致："
            f"{SqliteStore._SCHEMA_VERSION} != {PostgresStore._SCHEMA_VERSION}"
        )

    # 起新库，拿真实结构；PG 版连 CI 的 service。
    if backend == "sqlite":
        version, tables = read_sqlite()
        target = SqliteStore._SCHEMA_VERSION
    else:
        version, tables = read_postgres()
        target = PostgresStore._SCHEMA_VERSION

    # 新库记录的版本必须等于代码里的目标常量（否则启动写入和声明分叉）。
    if version != target:
        problems.append(
            f"新 {backend} 库记录的 schema 版本 {version} != 目标常量 {target}"
        )
    # 版本常量必须与快照版本一致（改了版本号就要同步更新快照）。
    if target != snap_version:
        problems.append(
            f"{backend} 目标常量版本 {target} != 快照版本 {snap_version}"
        )
    # 真实结构必须与快照一致。不一致 = schema 变了，但快照/版本没跟上。
    if tables != snap_tables:
        problems.append("真实 schema 结构与快照不一致：")
        problems.extend(_diff(snap_tables, tables))
        if target <= snap_version:
            problems.append(
                f"  [!] 结构变了但版本没升（{target} <= 快照 {snap_version}）——"
                "有意变更就必须 _SCHEMA_VERSION += 1"
            )
        problems.append(
            "  修复：升版本号后运行 `uv run --frozen python scripts/check_migrations.py --write`"
        )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Schema 迁移检查（版本 + 结构指纹）")
    parser.add_argument(
        "--backend",
        choices=("sqlite", "postgres"),
        default="sqlite",
        help="检查哪个后端（postgres 需要可用的库，CI 里是 service）",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="维护者用：有意的 schema 变更后，按 SQLite 真实结构重写快照",
    )
    args = parser.parse_args(argv)

    if args.write:
        version, tables = read_sqlite()
        write_snapshot(version, tables)
        return 0

    problems = verify(args.backend)
    if problems:
        print(f"[FAIL] {args.backend} 迁移检查未通过：")
        for line in problems:
            print(line)
        return 1
    print(f"[OK] {args.backend} 迁移检查通过：版本与结构指纹都与快照一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
