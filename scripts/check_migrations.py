#!/usr/bin/env python3
"""Schema 迁移检查：把"加了表/列却忘了升版本"从"上线后才发现"提前到 CI。

背景（交付面缺口 item 5）：
  item 4 把 `store/migrations.py` 删掉后，迁移改成**显式 schema 版本**——
  `SqliteStore._SCHEMA_VERSION` / `PostgresStore._SCHEMA_VERSION` + 启动时补列，
  并把目标版本写进 `__schema_version__` 单行表。这套机制**没有守门人**：
  谁往 `_init_schema` 加了一张表却忘了 `_SCHEMA_VERSION += 1`，启动照旧成功、
  老库却停在上一个版本，于是"记录版本"和"真实结构"悄悄分叉。

这个脚本就是那个守门人，做三件事：
  1. 真起一个新库（SQLite；PG 走 CI 的 service）→ 读出**真实结构指纹**。
  2. 断言新库记录的版本 == 目标常量，且 SQLite 与 PG 两个常量一致（两后端必须对齐）。
  3. 把真实结构指纹和签入的快照 `scripts/schema_snapshot.json` 对比。

结构指纹（升级过一版，见下"指纹结构"）不再是"表 -> 列名"，而是每张表
  列集合（列名 / 归一化类型 / 可空 / 默认值 / 是否主键）
+ 索引集合（规范名 / 唯一性 / 有序列）。

为什么要这么细：旧指纹只记列名，于是**加索引、改类型、改可空、加唯一约束
都能静默通过迁移检查**——迁移写错不会红，这是企业级交付最不该有的盲区。

用法：
    uv run --frozen python scripts/check_migrations.py --backend sqlite
    WARDEN_TEST_PG_HOST=localhost uv run --frozen \
        python scripts/check_migrations.py --backend postgres
    uv run --frozen python scripts/check_migrations.py --write  # 有意的 schema 变更后重写快照

退出码：0 一致（无漂移）；1 有分叉（并打印可执行的修复提示）。

指纹结构（确定性：表名、列名、索引名、字段名都排序后再落 JSON，保证 diff 稳定）：
    {
      "version": 5,
      "tables": {
        "<表名>": {
          "columns": {
            "<列名>": {"type": "text", "nullable": false, "default": "''", "pk": false}
          },
          "indexes": {
            "<规范索引名>": {"unique": false, "columns": ["a", "b"]}
          }
        }
      }
    }
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "scripts" / "schema_snapshot.json"
# SQLite 自带 sqlite_sequence/sqlite_master 等内部表，不属于业务 schema，指纹里排除。
_IGNORED_PREFIXES = ("sqlite_", "pg_")

# 类型别名表：把各后端对**同一语义类型**的不同写法收敛到一个规范名。
#
# 为什么必须归一：这份指纹要同时对着 SQLite 与 PG 校验，而 CI 的两个 job 用**同一份快照**
# （快照由 SQLite 侧 --write 生成）——即要求两个后端归一后产出**逐字节相同**的指纹。
# 可两边对同一列的 DDL 用词并不一致：
#   * SQLite 的 `INTEGER` 是 64 位动态整数；PG 侧对应列写的是 `BIGINT`（run_events.id
#     甚至是 `BIGSERIAL`），而 PG 自己的 4 字节 `INTEGER` 反而更窄。
#   * 浮点同理：SQLite 只有 `REAL`，PG 侧时间戳/计数列写的是 `DOUBLE PRECISION`。
# 不归一，这些"同义异写"会在每次跨后端对比时报假差异，把真漂移淹没在噪声里。
#
# 取舍（写死，别指望它抓精度）：归一以**语义族**为准（整数族 / 浮点族 / 文本族）。
# 因此"BIGINT 改成 INTEGER"这类**同族宽度**变化本指纹不报——这是刻意的：我们守的是
# "结构语义漂移"，不是逐字节的类型精度；真要抓宽度应在 DDL review 里抓。
_TYPE_FAMILIES: dict[str, str] = {
    # 文本族
    "text": "text",
    "string": "text",
    "clob": "text",
    "character varying": "varchar",
    "varchar": "varchar",
    "nvarchar": "varchar",
    "character": "char",
    "char": "char",
    "nchar": "char",
    # 整数族
    "integer": "integer",
    "int": "integer",
    "int4": "integer",
    "serial": "integer",
    "bigint": "integer",
    "int8": "integer",
    "bigserial": "integer",
    "smallint": "integer",
    "int2": "integer",
    # 浮点族
    "real": "real",
    "float": "real",
    "float4": "real",
    "float8": "real",
    "double": "real",
    "double precision": "real",
    # 其他
    "boolean": "boolean",
    "bool": "boolean",
    "blob": "blob",
    "bytea": "blob",
    "numeric": "numeric",
    "decimal": "numeric",
    "timestamp": "timestamp",
    "timestamp with time zone": "timestamp",
    "timestamp without time zone": "timestamp",
}

# 类型形如 `<base>` 或 `<base>(<长度>[, <精度>])`。base 允许含空格（`double precision`）。
_TYPE_RE = re.compile(r"^\s*([a-zA-Z_][a-zA-Z_ ]*?)\s*(?:\((\d+)(?:\s*,\s*(\d+))?\))?\s*$")
# PG 的 column_default 结尾会带类型 cast（`''::text`、`''::character varying(64)`），
# 而 SQLite 的 dflt_value 不带——抹掉才可比。
_PG_CAST_RE = re.compile(r"::[a-zA-Z_][a-zA-Z0-9_ ]*(?:\(\d+(?:\s*,\s*\d+)?\))?\s*$")


def _normalize_type(raw: Any) -> str:
    """把后端各自的类型写法收敛成规范名（见 `_TYPE_FAMILIES` 的理由）。

    保留括号里的长度（`varchar(64)`），这样 `VARCHAR(64)` 与 PG 的
    `character varying(64)` 归一成同一个 `varchar(64)`，而 `varchar(64)` 改成
    `varchar(128)` 仍会被发现——长度是我们想抓的真实漂移，不能丢。
    认不出的类型原样小写返回：**宁可报差异，也不隐藏**（假阴性比假阳性更危险）。
    """
    text = " ".join(str(raw or "").split()).lower()
    match = _TYPE_RE.match(text)
    if not match:
        return text
    base = match.group(1).strip()
    length = match.group(2)
    canonical = _TYPE_FAMILIES.get(base, base)
    return f"{canonical}({length})" if length is not None else canonical


def _normalize_sqlite_default(value: Any) -> str | None:
    """SQLite 的 dflt_value 就是 DDL 里的字面量（如 `''`）；没有默认值时是 None。"""
    return None if value is None else str(value)


def _normalize_pg_default(value: Any) -> str | None:
    """PG 的 column_default 比 SQLite 多两样"实现细节"，必须抹掉才可比：

    1. 结尾的类型 cast（`''::text`）——SQLite 不写；
    2. SERIAL/BIGSERIAL 的 `nextval('...'::regclass)`——它在 SQLite 侧对应
       `AUTOINCREMENT`，而 `pragma_table_info` 对 AUTOINCREMENT **根本不报默认值**；
       两边只有都归成 None 才一致。
    """
    if value is None:
        return None
    text = str(value).strip()
    if text.startswith("nextval("):
        return None
    return _PG_CAST_RE.sub("", text).strip()


def _canonical_index_key(name: str, unique: bool, columns: list[str]) -> str:
    """索引的规范键：唯一（约束）索引与普通索引分开处理。

    唯一约束在 SQLite 里常体现为自动索引（`sqlite_autoindex_*`）、在 PG 里是自动命名的
    `<表>_<列>_key`，名字两边不可比；而"哪个唯一约束存在"本身是有意义的 schema 事实。
    故把**非主键的唯一索引**统一命名成 `UNIQUE(列...)`（两边一致）；普通索引保留真实
    名字——显式 `CREATE INDEX` 的名字在两个后端本来就相同。

    取舍：唯一索引**改名**不会被本指纹发现；唯一性与列序才是我们要守的语义。
    """
    if unique:
        return "UNIQUE(" + ",".join(columns) + ")"
    return name


def _sqlite_table_fingerprint(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    """单表指纹：列（类型/可空/默认值/主键）+ 非主键索引（唯一性/有序列）。"""
    columns: dict[str, Any] = {}
    for _cid, col, ctype, notnull, dflt, pk in conn.execute(
        "SELECT * FROM pragma_table_info(?)", (name,)
    ):
        columns[str(col)] = {
            "type": _normalize_type(ctype),
            # SQLite 的坑：非 INTEGER 主键列的 `notnull` 可能是 0（"PK 不隐含 NOT NULL" 是老行为），
            # 而 PG 里主键列一律 is_nullable='NO'。以 `pk` 为准补正，否则跨后端必假差异。
            "nullable": not notnull and not pk,
            "default": _normalize_sqlite_default(dflt),
            "pk": bool(pk),
        }

    indexes: dict[str, Any] = {}
    for _seq, idx_name, unique, origin, _partial in conn.execute(
        "SELECT * FROM pragma_index_list(?)", (name,)
    ):
        idx_name = str(idx_name)
        # 主键的"自动索引"两边名字不同（SQLite `sqlite_autoindex_*`，PG `<表>_pkey`），
        # 且 rowid 别名主键（`INTEGER PRIMARY KEY`）在 SQLite 侧根本没有索引条目、
        # PG 侧却有 `_pkey`——名字不可比。主键信息已由上面的 `pk` 字段承载，
        # 这里**整体排除主键索引**，避免纯噪声 + 跨后端假差异。
        if origin == "pk":
            continue
        cols = [
            str(row[2])
            for row in conn.execute("SELECT * FROM pragma_index_info(?)", (idx_name,))
        ]
        key = _canonical_index_key(idx_name, bool(unique), cols)
        indexes[key] = {"unique": bool(unique), "columns": cols}
    return {"columns": columns, "indexes": indexes}


def _sqlite_tables(conn: sqlite3.Connection) -> dict[str, Any]:
    """整库指纹：表名 → 单表指纹。排除 SQLite 内部表。"""
    names = [
        str(r[0])
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    ]
    out: dict[str, Any] = {}
    for name in sorted(names):
        if name.startswith(_IGNORED_PREFIXES):
            continue
        out[name] = _sqlite_table_fingerprint(conn, name)
    return out


def read_sqlite() -> tuple[int, dict[str, Any]]:
    """起一个全新的 SQLite 库，读回 (记录的 schema 版本, 结构指纹)。

    除 RunStore 的库外，还**顺手建出 memories / audit_log**：它们与 Run 表同属一个
    "存储 schema"，却各自在自己的 store 里建表；不在这里补上，它们的结构变更就落在
    版本 + 指纹守门的盲区里。
    """
    from warden_agent.memory.store import SqliteMemoryStore
    from warden_agent.store.sqlite import SqliteStore
    from warden_agent.web.audit import SqliteAuditStore

    db_path = Path(tempfile.mkdtemp()) / "fresh.db"
    store = SqliteStore(db_path)
    try:
        # 复用同一条连接建表：指纹只关心结构，不关心连接归属。
        SqliteMemoryStore(conn=store.conn)
        # 传一个哑键，避免无 WARDEN_AUDIT_KEY 时打出"审计链退化为无键"告警。
        SqliteAuditStore(conn=store.conn, chain_key=b"schema-fingerprint")
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


def _pg_table_fingerprint(cur: Any, name: str) -> dict[str, Any]:
    """单表指纹（PG 侧）：information_schema 取列，pg_index/pg_attribute 取主键与索引。

    列属性来自 `information_schema.columns`（跨版本稳定）；主键与索引顺序来自
    `pg_catalog`（information_schema 不暴露索引列序，只能下沉到系统表）。
    """
    cur.execute(
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s",
        (name,),
    )
    col_rows = cur.fetchall()

    # 主键列集合：`information_schema` 不暴露主键，改查 pg_index（indisprimary）。
    # `a.attnum = ANY(i.indkey)` 是 PG 里取"索引覆盖列"的惯用写法。
    cur.execute(
        "SELECT a.attname FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
        "JOIN pg_class t ON t.oid = i.indrelid "
        "JOIN pg_namespace n ON n.oid = t.relnamespace "
        "WHERE n.nspname = 'public' AND t.relname = %s AND i.indisprimary",
        (name,),
    )
    pk_cols = {str(r[0]) for r in cur.fetchall()}

    columns: dict[str, Any] = {}
    for col_name, data_type, is_nullable, column_default in col_rows:
        columns[str(col_name)] = {
            "type": _normalize_type(data_type),
            "nullable": str(is_nullable).upper() == "YES",
            "default": _normalize_pg_default(column_default),
            "pk": str(col_name) in pk_cols,
        }

    # 非主键索引：`indkey` 的下标序就是索引列序（int2vector → int2[] 后 unnest 还原）。
    cur.execute(
        "SELECT c.relname, i.indisunique, "
        "ARRAY("
        "  SELECT a.attname FROM unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord) "
        "  JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum "
        "  WHERE k.attnum <> 0 ORDER BY k.ord"
        ") "
        "FROM pg_index i "
        "JOIN pg_class c ON c.oid = i.indexrelid "
        "JOIN pg_class t ON t.oid = i.indrelid "
        "JOIN pg_namespace n ON n.oid = t.relnamespace "
        "WHERE n.nspname = 'public' AND t.relname = %s AND NOT i.indisprimary",
        (name,),
    )
    indexes: dict[str, Any] = {}
    for idx_name, unique, cols in cur.fetchall():
        col_list = [str(c) for c in (cols or [])]
        key = _canonical_index_key(str(idx_name), bool(unique), col_list)
        indexes[key] = {"unique": bool(unique), "columns": col_list}
    return {"columns": columns, "indexes": indexes}


def read_postgres() -> tuple[int, dict[str, Any]]:
    """连上 CI 的 PG service，起 store 建表，读回 (记录版本, 结构指纹)。

    与 SQLite 侧对齐：memories / audit_log 也必须建出来，否则 PG 指纹里少了这两张表，
    会与快照（由 SQLite 侧写入）不一致而误报。
    """
    from warden_agent.memory.store import PostgresMemoryStore
    from warden_agent.store.postgres import PostgresStore
    from warden_agent.web.audit import PostgresAuditStore

    store = PostgresStore(**_pg_params())
    try:
        PostgresMemoryStore(conn=store.conn)
        PostgresAuditStore(conn=store.conn, chain_key=b"schema-fingerprint")
        version = store.schema_version()
        with store.conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
                "ORDER BY table_name"
            )
            names = [str(r[0]) for r in cur.fetchall()]
            tables: dict[str, Any] = {}
            for name in names:
                if name.startswith(_IGNORED_PREFIXES):
                    continue
                tables[name] = _pg_table_fingerprint(cur, name)
    finally:
        store.close()
    return version, tables


def load_snapshot() -> dict[str, Any]:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def write_snapshot(version: int, tables: dict[str, Any]) -> None:
    payload = {"version": version, "tables": tables}
    # 必须 newline="\n"：这是**签入仓库**的文件（scripts/schema_snapshot.json），
    # 而 Windows 上文本模式写入会把 \n 翻成 \r\n；`.gitattributes` 是 `* text=auto eol=lf`，
    # 所以工作区会变成 CRLF，而 `git diff`（git 归一化）与 `load_snapshot()`
    # （read_text 归一化）**都看不出来**。字节级守卫见 tests/test_migration_check.py。
    SNAPSHOT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"已重写快照：{SNAPSHOT}")


def _diff(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """打印人话版结构差异（表 / 列 / 列属性 / 索引），点明**具体**对象便于定位。"""
    lines: list[str] = []
    for name in sorted(set(actual) - set(expected)):
        lines.append(f"  + 新增表 {name}")
    for name in sorted(set(expected) - set(actual)):
        lines.append(f"  - 删除表 {name}")
    for name in sorted(set(expected) & set(actual)):
        exp, act = expected[name], actual[name]
        if not isinstance(exp, dict) or not isinstance(act, dict):
            continue
        exp_cols = exp.get("columns", {})
        act_cols = act.get("columns", {})
        for col in sorted(set(act_cols) - set(exp_cols)):
            lines.append(f"  + 表 {name} 新增列 {col}: {act_cols[col]}")
        for col in sorted(set(exp_cols) - set(act_cols)):
            lines.append(f"  - 表 {name} 删除列 {col}: {exp_cols[col]}")
        for col in sorted(set(exp_cols) & set(act_cols)):
            if exp_cols[col] != act_cols[col]:
                lines.append(
                    f"  ~ 表 {name} 列 {col} 属性变化：{exp_cols[col]} -> {act_cols[col]}"
                )
        exp_idx = exp.get("indexes", {})
        act_idx = act.get("indexes", {})
        for idx in sorted(set(act_idx) - set(exp_idx)):
            lines.append(f"  + 表 {name} 新增索引 {idx}: {act_idx[idx]}")
        for idx in sorted(set(exp_idx) - set(act_idx)):
            lines.append(f"  - 表 {name} 删除索引 {idx}: {exp_idx[idx]}")
        for idx in sorted(set(exp_idx) & set(act_idx)):
            if exp_idx[idx] != act_idx[idx]:
                lines.append(
                    f"  ~ 表 {name} 索引 {idx} 变化：{exp_idx[idx]} -> {act_idx[idx]}"
                )
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
        problems.append("真实 schema 结构与快照不一致（检测到漂移）：")
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
    print(f"[OK] {args.backend} 迁移检查通过：无漂移，版本与结构指纹都与快照一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
