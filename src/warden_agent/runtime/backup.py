"""备份与恢复：把"能不能把数据拿回来"从口头承诺变成一条可演练的命令。

为什么归到"企业级"缺口里：
  项目此前**没有任何备份/恢复能力**——数据库文件就在那儿，坏了/误删了就只能认。
  而"能恢复"这件事不能靠"我们用的是 SQLite"来搪塞：得有一条真跑得通、且**演练过**的路径。

实现选择（零新依赖）：
  - SQLite 用 Python 标准库 `sqlite3` 的**在线备份 API**（`Connection.backup`）：
    它做的是**一致性快照**——即使数据库正被写入，也不会拷到一个"半个事务"的中间态
    （直接 `copy` 文件就可能拷到不一致状态；这也正是本模块不推荐 cp 的原因）。
  - 恢复同样走备份 API，把备份内容写回目标库（**要求目标库当前没有别的进程在用**）。
  - 恢复前先校验备份**确实是一个 SQLite 库**、且通过 `PRAGMA integrity_check`——
    "备份是坏的"这件事必须在恢复时立刻发现，而不是等应用起来才炸。

PostgreSQL 不在本模块里实现：它的标准做法是 `pg_dump` / `pg_basebackup`（运维工具，
和本机是否装了那个客户端有关）。与其塞一段没验证过的 pg_dump 包装，不如在
`docs/operations.md` 里把流程和演练步骤写清楚——**没验证过的东西不进代码**。
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from pathlib import Path


class BackupError(RuntimeError):
    """备份/恢复失败（路径不对、备份损坏、目标被占用等）。"""


def _connect(path: str | Path) -> sqlite3.Connection:
    return sqlite3.connect(str(path))


def verify_sqlite(path: str | Path) -> tuple[bool, str]:
    """校验一个文件是不是健康的 SQLite 库。返回 (是否通过, 说明)。

    用途：① 恢复前确认备份可用；② 运维随手检查线上库有没有坏页。
    """
    target = Path(path)
    if not target.exists():
        return False, f"文件不存在：{target}"
    try:
        conn = _connect(target)
    except sqlite3.Error as e:  # 不是 SQLite 文件 / 权限问题
        return False, f"打不开：{e}"
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        result = str(row[0]) if row else "no result"
        if result != "ok":
            return False, f"integrity_check 未通过：{result}"
        # 再确认能列出表（有些"空文件"会通过 integrity_check 但其实是空的）
        tables = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()
        return True, f"ok（表数量 {int(tables[0]) if tables else 0}）"
    except sqlite3.DatabaseError as e:
        return False, f"不是有效的 SQLite 数据库：{e}"
    finally:
        conn.close()


def default_backup_path(db_path: str | Path, *, now: _dt.datetime | None = None) -> Path:
    """没指定目标时生成的默认文件名：`<库名>.backup-<UTC 时间戳>`，便于按时间找。"""
    src = Path(db_path)
    stamp = (now or _dt.datetime.now(_dt.UTC)).strftime("%Y%m%dT%H%M%SZ")
    return src.with_name(f"{src.name}.backup-{stamp}")


def backup_sqlite(
    db_path: str | Path, dest_path: str | Path | None = None
) -> dict[str, object]:
    """做一份一致性备份。返回一份可打印/可审计的信息。

    `dest_path` 不传则用 `default_backup_path()`。目标已存在时**拒绝覆盖**——
    备份文件被悄悄覆盖是最气人的事故之一（你以为在留副本，其实在毁副本）。
    """
    src_path = Path(db_path)
    if not src_path.exists():
        raise BackupError(f"源数据库不存在：{src_path}")
    ok, detail = verify_sqlite(src_path)
    if not ok:
        raise BackupError(f"源数据库不可用，拒绝备份：{detail}")

    dest = Path(dest_path) if dest_path is not None else default_backup_path(src_path)
    if dest.exists():
        raise BackupError(f"目标已存在，拒绝覆盖：{dest}（换一个名字或先移走它）")
    dest.parent.mkdir(parents=True, exist_ok=True)

    src = _connect(src_path)
    try:
        target = _connect(dest)
        try:
            # 在线备份 API：一致性快照，不需要停服务
            src.backup(target)
        finally:
            target.close()
    except sqlite3.Error as e:
        raise BackupError(f"备份失败：{e}") from e
    finally:
        src.close()

    ok2, detail2 = verify_sqlite(dest)
    if not ok2:
        raise BackupError(f"备份产物校验失败（已生成但不可用）：{dest} —— {detail2}")
    return {
        "source": str(src_path),
        "backup": str(dest),
        "bytes": dest.stat().st_size,
        "verified": detail2,
    }


def restore_sqlite(
    backup_path: str | Path, db_path: str | Path, *, overwrite: bool = False
) -> dict[str, object]:
    """从备份恢复。**目标库必须没有别的进程在用**（否则写进去的内容会被覆盖/冲突）。

    安全设计：目标已存在时默认**拒绝**，必须显式 `overwrite=True`。
    恢复是破坏性操作，"少打一个参数就覆盖掉线上库"太容易发生了。
    """
    src = Path(backup_path)
    if not src.exists():
        raise BackupError(f"备份不存在：{src}")
    ok, detail = verify_sqlite(src)
    if not ok:
        raise BackupError(f"备份不可用，拒绝恢复：{detail}")

    target_path = Path(db_path)
    if target_path.exists() and not overwrite:
        raise BackupError(
            f"目标库已存在：{target_path}。恢复会**覆盖**它——"
            "确认要这么做请加 overwrite=True（并先确认服务已停）"
        )
    target_path.parent.mkdir(parents=True, exist_ok=True)

    backup_conn = _connect(src)
    try:
        target = _connect(target_path)
        try:
            backup_conn.backup(target)   # 把备份内容写回目标库
            target.commit()
        finally:
            target.close()
    except sqlite3.Error as e:
        raise BackupError(f"恢复失败：{e}") from e
    finally:
        backup_conn.close()

    ok2, detail2 = verify_sqlite(target_path)
    if not ok2:
        raise BackupError(f"恢复后校验失败：{target_path} —— {detail2}")
    return {
        "backup": str(src),
        "restored_to": str(target_path),
        "bytes": target_path.stat().st_size,
        "verified": detail2,
    }
