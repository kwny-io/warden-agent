"""Schema 迁移守门人（`scripts/check_migrations.py`）的 pytest 入口。

CI 里由专门的 `migration` job 跑脚本（SQLite + 真 PG）；这里再钉一条 SQLite 的，
让日常 `pytest` 也守着"schema 变了必须升版本、且结构指纹与签入快照一致"。
PG 侧不在这里跑（无库时会静默跳过，那等于没守）——它归 CI 的 migration job。
"""
from __future__ import annotations

import importlib.util
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
    assert {"uid", "owner", "scope", "audit"} <= set(tables["memories"])
    assert {"id", "prev_hash", "hash"} <= set(tables["audit_log"])


def test_快照版本等于两个后端的目标常量() -> None:
    from warden_agent.store.postgres import PostgresStore
    from warden_agent.store.sqlite import SqliteStore

    module = _load_check_module()
    snap_version = int(module.load_snapshot()["version"])
    assert snap_version == SqliteStore._SCHEMA_VERSION
    assert snap_version == PostgresStore._SCHEMA_VERSION


def _verify_with_broken_table(
    monkeypatch: pytest.MonkeyPatch, table: str, dropped: str
) -> list[str]:
    """用真实指纹，但故意从某表删一列，验证守门会失败。"""
    module = _load_check_module()
    version, tables = module.read_sqlite()
    broken = {name: list(cols) for name, cols in tables.items()}
    broken[table] = [col for col in broken[table] if col != dropped]
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
