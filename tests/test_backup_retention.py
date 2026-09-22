"""备份保留策略与 PostgreSQL 备份。

两块要盯的东西：
  1. **保留策略**：备份会越堆越多，"只留最近 N 份"必须是可预测、可演练的
     （`dry_run` 先看要删什么——**删除不可逆**，不能一上来就删）。
  2. **PG 备份**：`pg_dump` 不在时要**明确报错**（不是假装成功）；产物要能通过
     `pg_restore --list` 校验；密码走环境变量、不进命令行。
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from warden_agent.cli import main
from warden_agent.runtime.backup import (
    BackupError,
    backup_postgres,
    backup_sqlite,
    plan_prune,
    prune_backups,
    verify_pg_dump,
)


def _touch_backup(directory: Path, stamp: str) -> Path:
    """造一个"备份文件"（内容不重要，只是要文件名符合约定）。"""
    path = directory / f"warden.db.backup-{stamp}"
    path.write_bytes(b"x")
    return path


# ---------- 保留策略 ----------


def test_保留最近N份_其余列出待删(tmp_path: Path) -> None:
    stamps = ["20260101T000000Z", "20260102T000000Z", "20260103T000000Z"]
    files = [_touch_backup(tmp_path, s) for s in stamps]
    doomed = plan_prune(files, keep=2)
    assert [p.name for p in doomed] == ["warden.db.backup-20260101T000000Z"]  # 最旧的


def test_保留数不少于总数时_一个都不删(tmp_path: Path) -> None:
    files = [_touch_backup(tmp_path, "20260101T000000Z")]
    assert plan_prune(files, keep=5) == []


def test_keep非正数_拒绝执行而不是全删(tmp_path: Path) -> None:
    """`keep=0` 不能理解成"全删"——那是最危险的默认解读。"""
    files = [_touch_backup(tmp_path, "20260101T000000Z")]
    with pytest.raises(BackupError):
        plan_prune(files, keep=0)


def test_演练不真删_真删只删该删的(tmp_path: Path) -> None:
    for s in ("20260101T000000Z", "20260102T000000Z", "20260103T000000Z"):
        _touch_backup(tmp_path, s)

    dry = prune_backups(tmp_path, keep=1, dry_run=True)
    assert dry["found"] == 3 and dry["kept"] == 1
    assert len(dry["removed"]) == 2  # type: ignore[arg-type]
    assert len(list(tmp_path.glob("*.backup-*"))) == 3, "演练不该删任何东西"

    real = prune_backups(tmp_path, keep=1, dry_run=False)
    assert len(real["removed"]) == 2  # type: ignore[arg-type]
    remaining = [p.name for p in tmp_path.glob("*.backup-*")]
    assert remaining == ["warden.db.backup-20260103T000000Z"], remaining


def test_cli_prune_默认只演练_加yes才删(tmp_path: Path) -> None:
    for s in ("20260101T000000Z", "20260102T000000Z"):
        _touch_backup(tmp_path, s)

    main(["backup-prune", "--dir", str(tmp_path), "--keep", "1"])
    assert len(list(tmp_path.glob("*.backup-*"))) == 2, "默认应只演练"

    main(["backup-prune", "--dir", str(tmp_path), "--keep", "1", "--yes"])
    assert [p.name for p in tmp_path.glob("*.backup-*")] == [
        "warden.db.backup-20260102T000000Z"
    ]


def test_备份后按keep清理(tmp_path: Path) -> None:
    """`warden backup --keep N`：备份完顺手清旧的；`--dry-run` 时只报告不删。"""
    db = tmp_path / "app.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()

    backup_sqlite(db, tmp_path / "w.db.backup-20260101T000000Z")
    backup_sqlite(db, tmp_path / "w.db.backup-20260102T000000Z")

    # 演练：新备份照样做（用显式名字避免"同一秒重名被拒"），但旧的一份都不删
    main([
        "backup", str(tmp_path / "w.db.backup-20260103T000000Z"),
        "--db", str(db), "--keep", "1", "--dry-run",
    ])
    assert len(list(tmp_path.glob("*.backup-*"))) == 3

    # 真删：只留最新的那份
    main([
        "backup", str(tmp_path / "w.db.backup-20260104T000000Z"),
        "--db", str(db), "--keep", "1",
    ])
    assert [p.name for p in tmp_path.glob("*.backup-*")] == [
        "w.db.backup-20260104T000000Z"
    ]


# ---------- PostgreSQL 备份 ----------


def test_没有pg_dump时明确报错(monkeypatch: pytest.MonkeyPatch) -> None:
    """不在就报清楚——**不要**悄悄退化成"用别的方式备份"（那才是真危险）。"""
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(BackupError, match="pg_dump"):
        backup_postgres({"dbname": "warden"})


def test_缺库名时报错() -> None:
    with pytest.raises(BackupError, match="dbname"):
        backup_postgres({"host": "localhost"})


def test_不是dump的文件校验失败(tmp_path: Path) -> None:
    bogus = tmp_path / "not-a-dump.dump"
    bogus.write_bytes(b"definitely not a pg dump")
    ok, detail = verify_pg_dump(bogus)
    # 没有 pg_restore 时说"找不到工具"，有则说"不可用"——两种都不是通过
    assert ok is False
    assert detail


def test_空文件校验失败(tmp_path: Path) -> None:
    empty = tmp_path / "empty.dump"
    empty.write_bytes(b"")
    ok, detail = verify_pg_dump(empty)
    assert ok is False and "空" in detail


def test_目标已存在时拒绝覆盖(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
    dest = tmp_path / "exists.dump"
    dest.write_bytes(b"already here")
    with pytest.raises(BackupError, match="拒绝覆盖"):
        backup_postgres({"dbname": "warden"}, dest)


# ---------- 真实 pg_dump 端到端（有工具才跑）----------

_PG_DUMP = shutil.which("pg_dump")
_HAS_PG = _PG_DUMP is not None and shutil.which("pg_restore") is not None


@pytest.mark.skipif(not _HAS_PG, reason="需要 pg_dump/pg_restore（postgresql-client）")
def test_pg备份端到端_产物可校验(tmp_path: Path) -> None:
    """真跑一遍 pg_dump（需要可用的 PG 与客户端工具）。

    本地验证方式：把 `pg_dump`/`pg_restore` 指向"转发到容器内"的包装脚本即可
    （容器 `warden-pg` 里带这两个工具）。
    """
    import os

    dest = tmp_path / "warden.dump"
    info = backup_postgres(
        {
            "host": os.environ.get("WARDEN_TEST_PG_HOST", "localhost"),
            "port": os.environ.get("WARDEN_TEST_PG_PORT", "5432"),
            "dbname": os.environ.get("WARDEN_TEST_PG_DB", "warden"),
            "user": os.environ.get("WARDEN_TEST_PG_USER", "postgres"),
            "password": os.environ.get("WARDEN_TEST_PG_PASSWORD", ""),
        },
        dest,
    )
    assert Path(str(info["backup"])).exists()
    assert int(info["bytes"]) > 0  # type: ignore[call-overload]
    assert "对象" in str(info["verified"])
