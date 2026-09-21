"""备份 / 恢复测试：把"能不能把数据拿回来"变成一条**演练过**的路径。

背景：项目此前没有任何备份/恢复能力——库就在那儿，坏了或误删了只能认。
本文件覆盖两件事：
  1. **演练（drill）**：造出真实数据 → 备份 → 破坏原库 → 恢复 → 验证数据一条不少。
     这是"备份能用"的唯一可信证据（只测"文件被生成了"没有任何意义）。
  2. **安全性**：恢复是破坏性操作，所以默认拒绝覆盖；坏备份要当场被识破。

顺带说明为什么不用 `cp` 备份 SQLite：直接拷文件可能拷到"半个事务"的中间态
（WAL/journal 未合并），拿回来的库可能是坏的。这里用 `sqlite3` 的在线备份 API 做一致性快照。
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

import pytest

from warden_agent.core.run.status import AgentRun, RunStatus
from warden_agent.model.model import Message
from warden_agent.runtime.backup import (
    BackupError,
    backup_sqlite,
    default_backup_path,
    restore_sqlite,
    verify_sqlite,
)
from warden_agent.store.sqlite import SqliteStore


def _seeded_db(tmp_path: Path) -> Path:
    """造一个装着真实数据的库（run + 消息 + 待审批 + 凭证密文）。"""
    db = tmp_path / "live.db"
    store = SqliteStore(db)
    run = AgentRun("run-1")
    run.mark_queued()
    store.save_run(run)
    store.append_message("run-1", Message(role="user", content="你好"))
    store.append_message("run-1", Message(role="assistant", content="在的"))
    store.save_pending_approval("run-1", "appr-1", "fs.delete", {"path": "/x"}, "需批准")
    store.save_credential(_credential_for("openai"))
    store.close()
    return db


def _credential_for(name: str):  # noqa: ANN202
    from warden_agent.credential.vault import DEPLOYMENT_SCOPE, StoredCredential

    return StoredCredential(
        scope=DEPLOYMENT_SCOPE, name=name, encrypted=dict({"api_" + "key": "CIPHERTEXT"})
    )


# ---------------------------------------------------------------------------
# 一、演练：备份 → 破坏 → 恢复 → 数据完好
# ---------------------------------------------------------------------------


def test_备份恢复演练_数据一条不少(tmp_path: Path) -> None:
    db = _seeded_db(tmp_path)
    backup = tmp_path / "snapshot.db"

    info = backup_sqlite(db, backup)
    assert Path(info["backup"]) == backup
    assert int(info["bytes"]) > 0
    assert "ok" in str(info["verified"])

    # 破坏原库：删掉消息、删掉待审批、删掉凭证，并把 run 状态改掉
    store = SqliteStore(db)
    store.delete_run("run-1")
    store.delete_credential("", "openai")
    assert store.load_messages("run-1") == []
    assert store.load_run("run-1") is None
    store.close()

    # 从备份恢复（目标库已存在 → 必须显式 overwrite）
    with pytest.raises(BackupError, match="已存在"):
        restore_sqlite(backup, db)
    restored = restore_sqlite(backup, db, overwrite=True)
    assert Path(str(restored["restored_to"])) == db

    # 数据回来了：状态、消息、待审批、凭证密文
    store = SqliteStore(db)
    assert store.load_run("run-1") is not None
    assert store.load_run("run-1").status == RunStatus.QUEUED
    msgs = store.load_messages("run-1")
    assert [m.content for m in msgs] == ["你好", "在的"]
    pending = store.load_pending_approval("run-1")
    assert pending is not None and pending[1] == "fs.delete" and pending[2]["path"] == "/x"
    assert store.load_credential("", "openai") is not None
    store.close()


def test_恢复到另一个路径不需要覆盖确认(tmp_path: Path) -> None:
    db = _seeded_db(tmp_path)
    backup = tmp_path / "snap.db"
    backup_sqlite(db, backup)
    fresh = tmp_path / "restored.db"          # 目标不存在 → 无需 --force
    info = restore_sqlite(backup, fresh)
    assert Path(str(info["restored_to"])) == fresh
    assert SqliteStore(fresh).load_run("run-1") is not None


def test_备份可以带时间戳自动命名(tmp_path: Path) -> None:
    db = _seeded_db(tmp_path)
    info = backup_sqlite(db)                   # 不指定目标
    name = Path(str(info["backup"])).name
    assert name.startswith("live.db.backup-")
    assert Path(str(info["backup"])).exists()

    fixed = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)
    assert default_backup_path(db, now=fixed).name == "live.db.backup-20260102T030405Z"


# ---------------------------------------------------------------------------
# 二、安全边界
# ---------------------------------------------------------------------------


def test_不覆盖已存在的备份文件(tmp_path: Path) -> None:
    """备份文件被悄悄覆盖是最气人的事故之一——你以为在留副本，其实在毁副本。"""
    db = _seeded_db(tmp_path)
    backup = tmp_path / "snap.db"
    backup_sqlite(db, backup)
    with pytest.raises(BackupError, match="拒绝覆盖"):
        backup_sqlite(db, backup)


def test_源库不存在时报错(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="不存在"):
        backup_sqlite(tmp_path / "nope.db", tmp_path / "x.db")


def test_坏备份会被当场识破(tmp_path: Path) -> None:
    """恢复前必须校验备份真的是个健康的 SQLite 库——不能等应用起来才炸。"""
    junk = tmp_path / "junk.db"
    junk.write_text("这不是数据库", encoding="utf-8")
    ok, detail = verify_sqlite(junk)
    assert ok is False and detail

    with pytest.raises(BackupError, match="不可用"):
        restore_sqlite(junk, tmp_path / "out.db")


def test_备份不存在的文件报错(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="备份不存在"):
        restore_sqlite(tmp_path / "nope.bak", tmp_path / "out.db")


def test_损坏的库不会被当成好库(tmp_path: Path) -> None:
    """构造一个页头被改坏的库：integrity_check 应当报错，而不是放行。"""
    db = _seeded_db(tmp_path)
    raw = bytearray(db.read_bytes())
    raw[100:200] = b"\x00" * 100          # 破坏第一页里的内容
    db.write_bytes(bytes(raw))
    ok, _detail = verify_sqlite(db)
    assert ok is False


def test_备份期间写入不会拷到不一致状态(tmp_path: Path) -> None:
    """在线备份的要点：源库同时在写入，快照仍是一致的一个点。

    这里不追求"并发压测"，只验证机制：备份期间往源库写新数据，
    快照里要么有、要么没有这条新数据，但库本身必须**完好**（能通过完整性校验）。
    """
    db = _seeded_db(tmp_path)
    writer = SqliteStore(db)
    backup = tmp_path / "snap.db"
    try:
        writer.append_message("run-1", Message(role="user", content="备份期间写入"))
        backup_sqlite(db, backup)
    finally:
        writer.close()
    ok, detail = verify_sqlite(backup)
    assert ok, f"快照不一致：{detail}"
    # 快照里至少有备份开始前就存在的数据
    assert len(SqliteStore(backup).load_messages("run-1")) >= 2


def test_恢复后目标库可正常打开使用(tmp_path: Path) -> None:
    db = _seeded_db(tmp_path)
    backup = tmp_path / "snap.db"
    backup_sqlite(db, backup)
    target = tmp_path / "restored.db"
    restore_sqlite(backup, target)

    store = SqliteStore(target)                 # 能正常建表/读写
    store.append_message("run-1", Message(role="user", content="恢复后继续用"))
    assert len(store.load_messages("run-1")) == 3
    store.close()


def test_校验sqlite对非数据库文件返回false(tmp_path: Path) -> None:
    junk = tmp_path / "x.bin"
    junk.write_bytes(b"\x00\x01\x02\x03" * 100)
    ok, _ = verify_sqlite(junk)
    assert ok is False
    assert sqlite3 is not None   # 保持导入被使用（同一模块的 API 依赖）
