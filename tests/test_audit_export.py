"""审计导出（`warden audit-export`）：归档/取证用的导出，且**自带可复核性**。

为什么这份测试要盯着"链字段"和"篡改退出码"：审计导出常被当作交给审计方/存档的证据。
只导出内容、不带 `prev_hash`/`hash` 的话，接收方无法独立复核"这份导出被动过没有"；
所以导出必须带链字段，且在**链已断**时明确报出来（退出码 4），而不是安静地写个文件。
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import pytest

from warden_agent.cli import _safe_export_name, main
from warden_agent.web.audit import AuditRecord, SqliteAuditStore


def _db(tmp_path: Path) -> Path:
    return tmp_path / "audit.db"


def _store(tmp_path: Path, key: bytes | None = None) -> SqliteAuditStore:
    # 默认 chain_key=None：CLI 也是从环境取 `WARDEN_AUDIT_KEY`（测试里没配），
    # 两边口径一致才能校验通过——这正是"校验必须用同一把键"的体现。
    return SqliteAuditStore(db_path=_db(tmp_path), chain_key=key)


def _append_n(store: SqliteAuditStore, n: int) -> None:
    for i in range(n):
        store.append(AuditRecord(
            correlation_id=f"cid-{i}", tenant_id="acme", principal_type="user",
            principal_id="alice", product_id="local", operation="QUERY",
            run_id=f"run-{i}", method="GET", path=f"/status/run-{i}",
            status=200, at=1_700_000_000.0 + i,
        ))


# ---- 存储层 ----


def test_导出按id升序且带链字段(tmp_path: Path) -> None:
    _append_n(_store(tmp_path), 3)
    rows = _store(tmp_path).export_records()
    assert [r["id"] for r in rows] == [1, 2, 3]
    for r in rows:
        assert r["hash"], "必须带链哈希，否则接收方无法复核"
        assert r["principal_id"] == "alice" and r["path"].startswith("/status/")
    # 链是"上一条的 hash 指向下一条的 prev_hash"——导出里必须保留这个指向关系
    assert rows[0]["prev_hash"] == ""          # 第一条的前驱是 GENESIS（空串）
    assert rows[1]["prev_hash"] == rows[0]["hash"]
    assert rows[2]["prev_hash"] == rows[1]["hash"]


def test_增量与限量导出(tmp_path: Path) -> None:
    _append_n(_store(tmp_path), 5)
    store = _store(tmp_path)
    assert [r["id"] for r in store.export_records(after_id=3)] == [4, 5]
    assert [r["id"] for r in store.export_records(limit=2)] == [1, 2]
    assert [r["id"] for r in store.export_records(after_id=1, limit=2)] == [2, 3]


# ---- CLI ----


def test_cli导出jsonl_且摘要报链完整(tmp_path: Path, capsys) -> None:
    _append_n(_store(tmp_path), 3)
    out = tmp_path / "out"
    main(["audit-export", "--db", str(_db(tmp_path)), "--out-dir", str(out)])

    files = list(out.glob("*.jsonl"))
    assert len(files) == 1
    lines = [json.loads(x) for x in files[0].read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 3 and lines[0]["id"] == 1
    captured = capsys.readouterr()
    assert "导出 3 条" in captured.out
    assert "链完整" in captured.out


def test_cli导出csv带表头(tmp_path: Path) -> None:
    _append_n(_store(tmp_path), 2)
    out = tmp_path / "out"
    main(["audit-export", "--db", str(_db(tmp_path)), "--out-dir", str(out), "--format", "csv"])
    files = list(out.glob("*.csv"))
    assert len(files) == 1
    with files[0].open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 2
    assert rows[0]["id"] == "1" and rows[0]["hash"]


def test_cli链断了_退出码4且仍然导出(tmp_path: Path) -> None:
    """链被改过 → 必须明确报出来（别让一份"被动过的审计"被当成正常导出）。"""
    _append_n(_store(tmp_path), 3)
    # 直接改库里的内容：链哈希对不上，verify_chain 会断
    conn = sqlite3.connect(str(_db(tmp_path)))
    conn.execute("UPDATE audit_log SET path = '/tampered' WHERE id = 2")
    conn.commit()
    conn.close()

    out = tmp_path / "out"
    with pytest.raises(SystemExit) as e:
        main(["audit-export", "--db", str(_db(tmp_path)), "--out-dir", str(out)])
    assert e.value.code == 4
    # 取证场景：即使链断了也要把数据导出来（便于人工比对），所以文件应当已写出
    assert list(out.glob("*.jsonl")), "链断时仍应写出导出文件（供取证）"


def test_导出文件名必须是纯文件名() -> None:
    assert _safe_export_name("audit-2026.jsonl") == "audit-2026.jsonl"
    for bad in ("../evil.jsonl", "a/b.jsonl", "a\\b.jsonl", "", ".hidden", "C:evil"):
        with pytest.raises(ValueError):
            _safe_export_name(bad)


def test_cli拒绝越界文件名(tmp_path: Path) -> None:
    _append_n(_store(tmp_path), 1)
    with pytest.raises(SystemExit) as e:
        main([
            "audit-export", "--db", str(_db(tmp_path)),
            "--out-dir", str(tmp_path / "out"), "--name", "../evil.jsonl",
        ])
    assert e.value.code == 1
    assert not (tmp_path / "evil.jsonl").exists()
