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

PostgreSQL：走标准运维工具 `pg_dump -Fc`（自定义格式）+ `pg_restore`。
  为什么还是进了代码（原先刻意不进）：因为**这次能在本地真跑通并演练**——
  容器里带 `pg_dump`/`pg_restore`，也就是说"没验证过的东西不进代码"这条原则满足了。
  代码只做三件事：① 明确检查 `pg_dump` 在不在（不在就报清楚，而不是假装成功）；
  ② 产物做完整性校验（`pg_restore --list` 能列出内容，才算一份可用的 dump）；
  ③ 默认不覆盖已有备份。密码走 `PGPASSWORD` 环境变量，**不放命令行**（命令行会进 ps/history）。

保留策略：备份会越堆越多，所以提供"只留最近 N 份"的清理（`plan_prune` / `prune_backups`），
  支持 `dry_run` 先看要删什么——**删除是不可逆的**，先干跑再真删。
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import shutil
import sqlite3
import subprocess
from collections.abc import Iterable, Mapping
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


# ---------------------------------------------------------------------------
# PostgreSQL 备份（pg_dump -Fc + pg_restore --list 校验）
# ---------------------------------------------------------------------------


def verify_pg_dump(path: str | Path, *, pg_restore_bin: str = "pg_restore") -> tuple[bool, str]:
    """校验一个文件是不是**可用的** PostgreSQL 自定义格式 dump。

    用 `pg_restore --list` 列出内容：能列出来才说明这份 dump 是完整可读的
    （相当于 SQLite 那边的 `PRAGMA integrity_check`）。
    """
    target = Path(path)
    if not target.exists():
        return False, f"文件不存在：{target}"
    if target.stat().st_size == 0:
        return False, f"文件为空（0 字节）：{target}"
    binary = shutil.which(pg_restore_bin)
    if binary is None:
        return False, f"找不到 {pg_restore_bin}（校验 dump 需要它；请装 postgresql-client）"
    try:
        proc = subprocess.run(
            [binary, "--list", str(target)],
            capture_output=True, timeout=120, check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"运行 {pg_restore_bin} 失败：{e}"
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()[:200]
        return False, f"dump 不可用（pg_restore --list 返回 {proc.returncode}）：{err}"
    entries = [
        line for line in proc.stdout.decode("utf-8", errors="replace").splitlines()
        if line.strip() and not line.startswith(";")
    ]
    return True, f"ok（可列出 {len(entries)} 个对象）"


def backup_postgres(
    params: Mapping[str, object],
    dest_path: str | Path | None = None,
    *,
    pg_dump_bin: str = "pg_dump",
    pg_restore_bin: str = "pg_restore",
    password_env: str = "PGPASSWORD",
    timeout_s: float = 600.0,
) -> dict[str, object]:
    """用 `pg_dump -Fc` 做一份 PostgreSQL 备份（**不覆盖已有文件**）。

    `params`：`host` / `port` / `dbname` / `user` / `password`（可省）。
    密码通过 `PGPASSWORD` 环境变量传给子进程，**不放进命令行**——命令行参数会出现在
    `ps`/`/proc` 与 shell history 里，等于把口令写到了日志里。
    """
    host = str(params.get("host") or "localhost")
    port = str(params.get("port") or 5432)
    dbname = str(params.get("dbname") or "")
    user = str(params.get("user") or "")
    password = params.get("password")
    if not dbname:
        raise BackupError("PostgreSQL 备份需要 dbname")

    binary = shutil.which(pg_dump_bin)
    if binary is None:
        raise BackupError(
            f"找不到 {pg_dump_bin}：PostgreSQL 备份需要 postgresql-client。"
            "装好后再试（或把 --pg-dump 指到具体路径），不要用 cp 备份 PG 数据目录。"
        )

    dest = Path(dest_path) if dest_path is not None else default_backup_path(f"{dbname}.pg")
    if dest.exists():
        raise BackupError(f"目标已存在，拒绝覆盖：{dest}（换一个名字或先移走它）")
    dest.parent.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    if password is not None and str(password) != "":
        env[password_env] = str(password)
    cmd = [binary, "-Fc"]
    if user:
        cmd += ["-U", user]
    cmd += ["-h", host, "-p", port, "-d", dbname, "-f", str(dest)]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, env=env, timeout=timeout_s, check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise BackupError(f"pg_dump 超时（{timeout_s}s）：数据库可能过大或不可达") from e
    except OSError as e:
        raise BackupError(f"无法执行 {pg_dump_bin}：{e}") from e

    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()[:300]
        raise BackupError(f"pg_dump 失败（退出码 {proc.returncode}）：{err}")

    ok, detail = verify_pg_dump(dest, pg_restore_bin=pg_restore_bin)
    if not ok:
        raise BackupError(f"备份产物校验失败（已生成但不可用）：{dest} —— {detail}")
    return {
        "source": f"postgresql://{host}:{port}/{dbname}",
        "backup": str(dest),
        "bytes": dest.stat().st_size,
        "verified": detail,
        "format": "pg_dump -Fc",
    }


# ---------------------------------------------------------------------------
# 保留策略：备份越堆越多，"只留最近 N 份"要能自动化
# ---------------------------------------------------------------------------

# 备份文件名里的时间戳（见 default_backup_path）：`.backup-YYYYmmddTHHMMSSZ`
_STAMP_RE = re.compile(r"\.backup-(\d{8}T\d{6}Z)$")


def plan_prune(paths: Iterable[str | Path], keep: int) -> list[Path]:
    """算出**该删掉**哪些备份：按时间倒序保留最新 `keep` 份，返回其余（要删的）。

    排序依据：优先用文件名里的时间戳（`default_backup_path` 生成的格式），
    拿不到就退回文件的修改时间。`keep <= 0` 视为参数错误（拒绝执行，而不是"全删"）。
    """
    if keep <= 0:
        raise BackupError("keep 必须 >= 1（不提供\"全删\"；要清空请自己动手删）")

    def sort_key(p: Path) -> tuple[str, float]:
        m = _STAMP_RE.search(p.name)
        return (m.group(1) if m else "", p.stat().st_mtime)

    files = [Path(p) for p in paths]
    files.sort(key=sort_key, reverse=True)     # 新的在前
    return files[keep:]


def prune_backups(
    directory: str | Path,
    keep: int,
    *,
    pattern: str = "*.backup-*",
    dry_run: bool = False,
) -> dict[str, object]:
    """清理备份目录，只留最近 `keep` 份。

    `dry_run=True` 只报告要删什么、**不真删**——删除不可逆，先干跑再动手。
    """
    base = Path(directory)
    if not base.is_dir():
        raise BackupError(f"不是目录：{base}")
    candidates = sorted(p for p in base.glob(pattern) if p.is_file())
    doomed = plan_prune(candidates, keep)
    if not dry_run:
        for path in doomed:
            try:
                path.unlink()
            except OSError as e:
                raise BackupError(f"删除失败 {path}: {e}") from e
    return {
        "directory": str(base),
        "pattern": pattern,
        "found": len(candidates),
        "kept": len(candidates) - len(doomed),
        "removed": [str(p) for p in doomed],
        "dry_run": dry_run,
    }
