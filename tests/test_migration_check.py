"""Schema 迁移守门人（`scripts/check_migrations.py`）的 pytest 入口。

CI 里由专门的 `migration` job 跑脚本（SQLite + 真 PG）；这里再钉一条 SQLite 的，
让日常 `pytest` 也守着"schema 变了必须升版本、且结构指纹与签入快照一致"。
PG 侧不在这里跑（无库时会静默跳过，那等于没守）——它归 CI 的 migration job。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

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
