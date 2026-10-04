"""Schema 迁移守门人（`scripts/check_migrations.py`）的 pytest 入口。

CI 里由专门的 `migration` job 跑脚本（SQLite + 真 PG）；这里再钉一条 SQLite 的，
让日常 `pytest` 也守着"schema 变了必须升版本、且结构指纹与签入快照一致"。
PG 侧不在这里跑（无库时会静默跳过，那等于没守）——它归 CI 的 migration job。

指纹升级后（列名 → 列属性 + 索引），这里的重点从"表里有这列"扩展到
"改类型 / 加索引 / 改可空 / 改默认值 / 加唯一约束 / 换主键 都能被报出来"：
旧指纹只记列名，这些漂移全会静默通过。
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def _load_check_module():
    spec = importlib.util.spec_from_file_location(
        "check_migrations", _ROOT / "scripts" / "check_migrations.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fingerprint(module, script: str) -> dict:
    """在内存库里建出给定 schema，返回脚本算出的整库指纹。"""
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(script)
        return module._sqlite_tables(conn)
    finally:
        conn.close()


def _drift(module, before: str, after: str) -> list[str]:
    """把两个 schema 各算一遍指纹并 diff，返回漂移描述行。"""
    return module._diff(_fingerprint(module, before), _fingerprint(module, after))


# ---------------------------------------------------------------------------
# 端到端：真实库 vs 签入快照
# ---------------------------------------------------------------------------
def test_sqlite迁移检查通过_版本与结构指纹一致() -> None:
    module = _load_check_module()
    assert module.verify("sqlite") == []


def test_快照覆盖记忆与审计表() -> None:
    """memories / audit_log 此前不在指纹里——改了它们的结构 CI 拦不住，现在必须覆盖。"""
    module = _load_check_module()
    snapshot = module.load_snapshot()
    tables = snapshot["tables"]
    assert "memories" in tables, "记忆表不在 schema 指纹里，结构变更会绕过守门"
    assert "audit_log" in tables, "审计表不在 schema 指纹里，结构变更会绕过守门"
    # 关键列确实在指纹里（防止只登记了一个空表名糊弄过去）
    memory_cols = set(tables["memories"]["columns"])
    audit_cols = set(tables["audit_log"]["columns"])
    assert {"uid", "owner", "scope", "audit"} <= memory_cols
    assert {"id", "prev_hash", "hash"} <= audit_cols


def test_快照把主键与索引也记进去了() -> None:
    """列名之外的属性/索引必须在快照里，否则加索引/换主键又会静默通过。"""
    module = _load_check_module()
    tables = module.load_snapshot()["tables"]
    # runs.run_id TEXT PRIMARY KEY：主键标记为 True，且非空
    run_id = tables["runs"]["columns"]["run_id"]
    assert run_id["pk"] is True
    assert run_id["nullable"] is False
    assert run_id["type"] == "text"
    # 显式索引被记录（名字 + 有序列）
    assert tables["memories"]["indexes"]["idx_memories_ref"] == {
        "unique": False,
        "columns": ["scope", "key"],
    }
    # 默认值也被记录
    assert tables["runs"]["columns"]["user_id"]["default"] == "''"


def test_快照版本等于两个后端的目标常量() -> None:
    from warden_agent.store.postgres import PostgresStore
    from warden_agent.store.sqlite import SqliteStore

    module = _load_check_module()
    snap_version = int(module.load_snapshot()["version"])
    assert snap_version == SqliteStore._SCHEMA_VERSION
    assert snap_version == PostgresStore._SCHEMA_VERSION


# ---------------------------------------------------------------------------
# 漂移类别：每种真实漂移都必须能被报出（旧指纹在这些场景全是哑的）
# ---------------------------------------------------------------------------
def test_重写快照在字节级别可还原且不引入CRLF(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`write_snapshot` 改写的是**签入仓库**的 `scripts/schema_snapshot.json`。

    两条性质必须在**字节**级别成立：
      * 用同一份内容重写两次，字节完全一致（幂等）；
      * 不引入 `\r\n`——Windows 上文本模式写入会把 `\n` 翻成 `\r\n`，而本仓的
        `.gitattributes` 是 `* text=auto eol=lf`：工作区会变成 CRLF，而
        `git diff`（git 归一化）与 `load_snapshot()`（`read_text` 归一化）
        **都看不出来**——只能用字节守。
    """
    module = _load_check_module()
    payload = module.load_snapshot()  # 先读真快照（此时 SNAPSHOT 还是真路径）
    copy = tmp_path / "schema_snapshot.json"
    monkeypatch.setattr(module, "SNAPSHOT", copy)
    module.write_snapshot(payload["version"], payload["tables"])
    first = copy.read_bytes()
    module.write_snapshot(payload["version"], payload["tables"])
    assert copy.read_bytes() == first, "同一内容重写两次，字节却变了"
    assert b"\r\n" not in first, "重写引入了 CRLF（newline='\\n' 掉了？）"
    assert first.endswith(b"}\n"), "结尾不是单个换行"


def test_改列类型会被指纹报出() -> None:
    module = _load_check_module()
    diff = _drift(
        module,
        "CREATE TABLE t (a TEXT);",
        "CREATE TABLE t (a INTEGER);",
    )
    assert diff, "TEXT 改成 INTEGER 却没报差异——类型漂移是盲区"
    assert any("列 a" in line and "type" in line for line in diff)


def test_加索引会被指纹报出() -> None:
    module = _load_check_module()
    diff = _drift(
        module,
        "CREATE TABLE t (a TEXT, b TEXT);",
        "CREATE TABLE t (a TEXT, b TEXT);\nCREATE INDEX idx_t_a ON t (a);",
    )
    assert diff, "加了索引却没报差异——索引改动是盲区"
    assert any("idx_t_a" in line for line in diff)


def test_改可空会被指纹报出() -> None:
    module = _load_check_module()
    diff = _drift(
        module,
        "CREATE TABLE t (a TEXT);",
        "CREATE TABLE t (a TEXT NOT NULL);",
    )
    assert diff, "可空性变了却没报差异——NOT NULL 漂移是盲区"
    assert any("列 a" in line and "nullable" in line for line in diff)


def test_改默认值会被指纹报出() -> None:
    module = _load_check_module()
    diff = _drift(
        module,
        "CREATE TABLE t (a TEXT);",
        "CREATE TABLE t (a TEXT DEFAULT 'x');",
    )
    assert diff, "默认值变了却没报差异——DEFAULT 漂移是盲区"
    assert any("列 a" in line and "default" in line for line in diff)


def test_加唯一约束会被指纹报出() -> None:
    module = _load_check_module()
    # SQLite 会把 UNIQUE 落成自动索引 sqlite_autoindex_*；规范化成 UNIQUE(a) 后必须可见。
    diff = _drift(
        module,
        "CREATE TABLE t (a TEXT);",
        "CREATE TABLE t (a TEXT UNIQUE);",
    )
    assert diff, "加了唯一约束却没报差异——唯一性漂移是盲区"
    assert any("UNIQUE(a)" in line for line in diff)


def test_主键变化会被指纹报出() -> None:
    module = _load_check_module()
    diff = _drift(
        module,
        "CREATE TABLE t (a TEXT, b TEXT);",
        "CREATE TABLE t (a TEXT, b TEXT, PRIMARY KEY (a));",
    )
    assert diff, "主键从无到有却没报差异——主键漂移是盲区"
    assert any("列 a" in line and "pk" in line for line in diff)


def test_类型归一化不把VARCHAR与character_varying当差异() -> None:
    """归一化规则本身：跨后端同义写法不能产生假差异。"""
    module = _load_check_module()
    assert module._normalize_type("VARCHAR(64)") == module._normalize_type(
        "character varying(64)"
    )
    # 但长度是真漂移，必须保留
    assert module._normalize_type("varchar(64)") != module._normalize_type("varchar(128)")


# ---------------------------------------------------------------------------
# 退出码与错误信息可用性
# ---------------------------------------------------------------------------
def test_无漂移时退出码为零且报无漂移(capsys: pytest.CaptureFixture[str]) -> None:
    module = _load_check_module()
    assert module.main(["--backend", "sqlite"]) == 0
    out = capsys.readouterr().out
    assert "无漂移" in out


def _verify_with_broken_table(
    monkeypatch: pytest.MonkeyPatch, table: str, dropped: str
) -> list[str]:
    """用真实指纹，但故意从某表删一列，验证守门会失败。"""
    module = _load_check_module()
    version, tables = module.read_sqlite()
    broken = {
        name: {
            "columns": dict(spec["columns"]),
            "indexes": dict(spec["indexes"]),
        }
        for name, spec in tables.items()
    }
    del broken[table]["columns"][dropped]
    monkeypatch.setattr(module, "read_sqlite", lambda: (version, broken))
    return module.verify("sqlite")


def test_故意删掉记忆表一列会被判失败(monkeypatch: pytest.MonkeyPatch) -> None:
    problems = _verify_with_broken_table(monkeypatch, "memories", "owner")
    assert problems, "memories 少了列却没被判失败——记忆表仍在指纹盲区"
    assert any("memories" in line for line in problems)


def test_故意删掉审计表一列会被判失败(monkeypatch: pytest.MonkeyPatch) -> None:
    problems = _verify_with_broken_table(monkeypatch, "audit_log", "prev_hash")
    assert problems, "audit_log 少了列却没被判失败——审计表仍在指纹盲区"
    assert any("audit_log" in line for line in problems)


def test_有漂移时退出码为一且信息定位到表列(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """差异时：退出码 1，且能指出**具体**表/列（不是笼统的"结构不一致"）。"""
    module = _load_check_module()
    version, tables = module.read_sqlite()
    broken = {
        name: {
            "columns": dict(spec["columns"]),
            "indexes": dict(spec["indexes"]),
        }
        for name, spec in tables.items()
    }
    broken["runs"]["columns"]["status"] = dict(broken["runs"]["columns"]["status"])
    broken["runs"]["columns"]["status"]["type"] = "integer"  # 类型被改
    monkeypatch.setattr(module, "read_sqlite", lambda: (version, broken))

    assert module.main(["--backend", "sqlite"]) == 1
    out = capsys.readouterr().out
    assert "runs" in out and "status" in out, "错误信息没定位到具体表/列"
    assert "属性变化" in out
