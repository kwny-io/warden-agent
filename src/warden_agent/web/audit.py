"""审计日志（Audit）：把"谁在什么时间对哪个 Run 做了哪件事"沉淀成可查账的记录。

  - 每次 HTTP 请求都带一个 correlation_id（关联 ID），前端/网关/日志/审计共用，
    一条请求从入到出都能串起来。
  - 认证通过后，把 caller（谁）+ operation（做什么操作）+ run_id（对哪个会话）
    + 结果状态 记成一条 AuditRecord。
  - 审计写入是"尽力而为"的：审计后端挂了绝不能让业务请求失败（
    "审计绝不能成为主路径的单点"）。

提供了两种后端（都实现 AuditStore 协议，可互换，与 RunStore 的插拔风格一致）：
  - InMemoryAuditStore：进程内列表，适合单机/测试/演示。
  - SqliteAuditStore：落盘到 SQLite 的 audit 表，重启不丢，可查账。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from warden_agent.web.auth import LOCAL_CALLER, RunOperation, TrustedCaller

logger = logging.getLogger(__name__)


# ---- 审计记录 ----
@dataclass
class AuditRecord:
    correlation_id: str
    tenant_id: str
    principal_type: str
    principal_id: str
    product_id: str
    operation: str
    run_id: str | None
    method: str
    path: str
    status: int
    at: float = field(default_factory=time.time)  # epoch 秒

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AuditStore(Protocol):
    def append(self, record: AuditRecord) -> None: ...

    def query(
        self,
        *,
        tenant_id: str | None = None,
        run_id: str | None = None,
        operation: str | None = None,
        principal_id: str | None = None,
        limit: int = 200,
    ) -> list[AuditRecord]: ...


class InMemoryAuditStore:
    """进程内审计存储：内存列表 + 读写锁。适合单机与测试。"""

    def __init__(self) -> None:
        self._records: list[AuditRecord] = []
        self._lock = threading.Lock()

    def append(self, record: AuditRecord) -> None:
        with self._lock:
            self._records.append(record)

    def query(
        self,
        *,
        tenant_id: str | None = None,
        run_id: str | None = None,
        operation: str | None = None,
        principal_id: str | None = None,
        limit: int = 200,
    ) -> list[AuditRecord]:
        with self._lock:
            rows = list(self._records)
        if tenant_id is not None:
            rows = [r for r in rows if r.tenant_id == tenant_id]
        if run_id is not None:
            rows = [r for r in rows if r.run_id == run_id]
        if operation is not None:
            rows = [r for r in rows if r.operation == operation]
        if principal_id is not None:
            rows = [r for r in rows if r.principal_id == principal_id]
        return rows[-limit:] if limit and limit > 0 else rows


class SqliteAuditStore:
    """SQLite 落盘审计。表 audit_log 由 __init__ 自建（IF NOT EXISTS），
    与 warden-agent-local.db 同一文件，方便运维直接查账。
    """

    def __init__(
        self,
        db_path: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        if conn is not None:
            self._conn = conn
        elif db_path is not None:
            self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        else:
            raise ValueError("SqliteAuditStore 需要 db_path 或 conn")
        self._lock = threading.Lock()
        self._init()

    def _init(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                correlation_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                principal_type TEXT NOT NULL,
                principal_id TEXT NOT NULL,
                product_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                run_id TEXT,
                method TEXT NOT NULL,
                path TEXT NOT NULL,
                status INTEGER NOT NULL,
                at REAL NOT NULL
            )
            """
        )
        self._conn.commit()

    def append(self, record: AuditRecord) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit_log ("
                " correlation_id, tenant_id, principal_type, principal_id, product_id,"
                " operation, run_id, method, path, status, at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.correlation_id,
                    record.tenant_id,
                    record.principal_type,
                    record.principal_id,
                    record.product_id,
                    record.operation,
                    record.run_id,
                    record.method,
                    record.path,
                    record.status,
                    record.at,
                ),
            )
            self._conn.commit()

    def query(
        self,
        *,
        tenant_id: str | None = None,
        run_id: str | None = None,
        operation: str | None = None,
        principal_id: str | None = None,
        limit: int = 200,
    ) -> list[AuditRecord]:
        where: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            where.append("tenant_id = ?")
            params.append(tenant_id)
        if run_id is not None:
            where.append("run_id = ?")
            params.append(run_id)
        if operation is not None:
            where.append("operation = ?")
            params.append(operation)
        if principal_id is not None:
            where.append("principal_id = ?")
            params.append(principal_id)
        sql = "SELECT * FROM audit_log"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit) if limit and limit > 0 else 200)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_record(r) for r in rows][::-1]

    @staticmethod
    def _row_to_record(row: tuple[Any, ...]) -> AuditRecord:
        # 列序与建表一致：id, correlation_id, tenant_id, principal_type, principal_id,
        # product_id, operation, run_id, method, path, status, at
        return AuditRecord(
            correlation_id=row[1],
            tenant_id=row[2],
            principal_type=row[3],
            principal_id=row[4],
            product_id=row[5],
            operation=row[6],
            run_id=row[7],
            method=row[8],
            path=row[9],
            status=row[10],
            at=row[11],
        )

    def close(self) -> None:
        self._conn.close()


class AuditLogger:
    """App 层的审计写入门面：决定"是否记录 + 记到哪个后端"。"""

    def __init__(self, store: AuditStore, enabled: bool = True) -> None:
        self._store = store
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    def record(
        self,
        *,
        correlation_id: str,
        caller: TrustedCaller | None,
        operation: RunOperation,
        run_id: str | None,
        method: str,
        path: str,
        status: int,
    ) -> None:
        """写一条审计。审计后端异常绝不能波及业务请求——吞掉并记 warning。"""
        if not self._enabled:
            return
        caller = caller if caller is not None else LOCAL_CALLER
        record = AuditRecord(
            correlation_id=correlation_id,
            tenant_id=caller.tenant_id,
            principal_type=caller.principal_type,
            principal_id=caller.principal_id,
            product_id=caller.product_id,
            operation=operation.value,
            run_id=run_id,
            method=method,
            path=path.split("?")[0],
            status=status,
        )
        try:
            self._store.append(record)
        except Exception:  # noqa: BLE001 - 审计失败不能炸掉业务
            logger.warning("审计写入失败 correlation=%s", correlation_id)
