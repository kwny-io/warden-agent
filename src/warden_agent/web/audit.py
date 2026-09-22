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

## 防篡改：带密钥的哈希链

"审计"如果谁都能改，就只是普通日志。落盘那份因此做成**链式**的：
每条记录的哈希把**上一条的哈希**一起算进去（`prev_hash` → `hash`），于是——

  - 改任何一条的字段 → 它自己的哈希对不上；
  - 删中间一条 → 它后面那条的 `prev_hash` 对不上；
  - 重排 / 伪造插入 → 链条断掉。

**关键：哈希用 HMAC-SHA256，密钥来自 `WARDEN_AUDIT_KEY`，不是裸 SHA256。**
为什么必须带密钥：裸哈希链只能防"手改一行"——攻击者完全可以改完记录后**把整条链重算一遍**，
看起来依然自洽。带密钥就没有这个口子：**不知道密钥就算不出合法哈希**。
（"防篡改"的真实语义正是这样：不是不可改，而是**改了必然被发现**。）

诚实边界：
  - 未配置 `WARDEN_AUDIT_KEY` 时退化为**不带密钥的哈希链**并明确告警——仍能发现"手改/删行"，
    但挡不住会重算整条链的人。生产应配置该密钥。
  - 链是**单机串行**的：`append` 在同一把锁内读链头 + 写新链头，同一进程内顺序一致。
    **但没有跨进程的链锁**，所以审计的写入方建议只有一个（或共享库 + 单写入者）。
  - `verify_chain()` 是全表顺序扫描（账本本来就要能整条走一遍）；表很大时耗时线性增长。
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from warden_agent.core.settings import env_str
from warden_agent.web.auth import LOCAL_CALLER, RunOperation, TrustedCaller

logger = logging.getLogger(__name__)

# 链首的 prev_hash（第一条记录没有前驱）
GENESIS = ""

# 参与哈希的字段（顺序固定，改动会让历史链失效 → 必须当成格式变更对待）
_CHAIN_FIELDS = (
    "correlation_id", "tenant_id", "principal_type", "principal_id", "product_id",
    "operation", "run_id", "method", "path", "status", "at",
)


def audit_chain_key(env: Mapping[str, str] | None = None) -> bytes | None:
    """取审计链的 HMAC 密钥。

    只用环境变量。未配置时返回 None（调用方会退化为不带密钥的哈希链并告警）——
    不生成"进程内临时密钥"，因为审计链要跨重启校验，临时密钥会让昨天的链今天就验不过。
    """
    src = env if env is not None else os.environ
    material = env_str("WARDEN_AUDIT_KEY", "", src).strip()
    return material.encode("utf-8") if material else None


def chain_hash(key: bytes | None, record: AuditRecord, prev_hash: str, row_id: int) -> str:
    """算一条记录的链哈希：把记录内容 + 前一条的哈希 + 行号一起算进去。

    带 `row_id` 是为了让"重排/插到中间"也断链（否则两条内容相同的记录互换位置不会被发现）。
    密钥存在时用 HMAC-SHA256；不存在时退化为 SHA-256（能查出手改/删行，挡不住重算整链）。
    """
    payload = json.dumps(
        {name: getattr(record, name) for name in _CHAIN_FIELDS},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    message = f"{row_id}|{prev_hash}|{payload}".encode()
    if key is not None:
        return hmac.new(key, message, hashlib.sha256).hexdigest()
    return hashlib.sha256(message).hexdigest()


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
        chain_key: bytes | None | str = "env",
    ) -> None:
        if conn is not None:
            self._conn = conn
        elif db_path is not None:
            self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        else:
            raise ValueError("SqliteAuditStore 需要 db_path 或 conn")
        self._lock = threading.Lock()
        if chain_key == "env":
            self._chain_key = audit_chain_key()
        elif isinstance(chain_key, str):
            self._chain_key = chain_key.encode("utf-8")
        else:
            self._chain_key = chain_key
        if self._chain_key is None:
            logger.warning(
                "未配置 WARDEN_AUDIT_KEY：审计链退化为**不带密钥**的哈希链。"
                "仍能发现「手改/删行」，但挡不住会重算整条链的人——生产环境请配置该密钥。"
            )
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
                at REAL NOT NULL,
                -- 防篡改链：prev_hash 指向上一条的 hash；hash 覆盖本条内容 + 前驱 + 行号
                prev_hash TEXT NOT NULL DEFAULT '',
                hash TEXT NOT NULL DEFAULT ''
            )
            """
        )
        # 老库补列（列加在末尾，不影响 _row_to_record 的位置索引）
        for column in ("prev_hash", "hash"):
            with contextlib.suppress(sqlite3.OperationalError):
                self._conn.execute(
                    f"ALTER TABLE audit_log ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                )
        self._conn.commit()

    def _last_hash(self) -> str:
        """链头：已落库的最后一条的 hash（空表 → GENESIS）。

        注意：老库里可能既有"补列后为空串"的历史行，也有新写的有哈希行。
        空串视为"链从这里重新开始"，verify_chain 会如实报告断点，不假装完整。
        """
        row = self._conn.execute(
            "SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return str(row[0]) if row and row[0] else GENESIS

    def append(self, record: AuditRecord) -> None:
        with self._lock:
            prev_hash = self._last_hash()
            # 行号要等插入后才知道，而哈希又必须写入该行 → 先插占位再回填同一条记录。
            # 两步在同一把锁内完成，链头不会被并发插队。
            cur = self._conn.execute(
                "INSERT INTO audit_log ("
                " correlation_id, tenant_id, principal_type, principal_id, product_id,"
                " operation, run_id, method, path, status, at, prev_hash, hash)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '')",
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
                    prev_hash,
                ),
            )
            row_id = int(cur.lastrowid or 0)
            digest = chain_hash(self._chain_key, record, prev_hash, row_id)
            self._conn.execute(
                "UPDATE audit_log SET hash = ? WHERE id = ?", (digest, row_id)
            )
            self._conn.commit()

    def verify_chain(self) -> tuple[bool, str]:
        """整条链走一遍，校验每条记录的哈希与前后衔接。返回 (是否完整, 说明)。

        **这是"防篡改"的兑现方式**：审计表谁都能改，但改完必须对得上链——
        对不上就说明被动过，`warden audit-verify` / 运维巡检据此报警。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, correlation_id, tenant_id, principal_type, principal_id,"
                " product_id, operation, run_id, method, path, status, at,"
                " prev_hash, hash FROM audit_log ORDER BY id"
            ).fetchall()

        prev_hash = GENESIS
        for index, row in enumerate(rows):
            row_id = int(row[0])
            record = self._row_to_record(row)
            stored_prev, stored_hash = str(row[12] or ""), str(row[13] or "")
            if stored_hash == "":
                return False, (
                    f"第 {index + 1} 条（id={row_id}）没有链哈希——"
                    "可能是加链之前写下的历史记录；无法证明它未被改动"
                )
            if stored_prev != prev_hash:
                return False, (
                    f"链条在 id={row_id} 处断开：它的 prev_hash={stored_prev[:12]}… "
                    f"与上一条的 hash={prev_hash[:12]}… 不一致（该条之前有记录被删/被改）"
                )
            expect = chain_hash(self._chain_key, record, prev_hash, row_id)
            if expect != stored_hash:
                return False, (
                    f"id={row_id} 的内容与它的哈希不符——这条记录被改动过"
                    "（字段、时间或行号任一被改都会导致不匹配）"
                )
            prev_hash = stored_hash
        return True, f"链完整：{len(rows)} 条记录，链头 {prev_hash[:12]}…"

    def export_records(
        self, *, after_id: int = 0, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """按 id 升序导出审计记录（含**链字段** id/prev_hash/hash），供归档/取证。

        为什么带上链字段：审计导出常被当作"交给审计方/存档"的证据。只导出内容而不带
        `prev_hash`/`hash`，接收方就无法独立复核"这份导出有没有被动过"；
        带上之后，接收方可以拿同样的键重算整条链。

        `after_id` 支持增量导出（接着上次的 id 往后拉）。
        两条 SQL 都**直接以字面量写在 execute 处**、只用占位符传值——不把 SQL 放进变量、
        也不做任何字符串拼接（本仓库的硬约定：扫描器与守卫都按这条看着）。
        """
        if limit is not None and limit > 0:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT id, correlation_id, tenant_id, principal_type, principal_id,"
                    " product_id, operation, run_id, method, path, status, at,"
                    " prev_hash, hash FROM audit_log WHERE id > ? ORDER BY id LIMIT ?",
                    (int(after_id), int(limit)),
                ).fetchall()
        else:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT id, correlation_id, tenant_id, principal_type, principal_id,"
                    " product_id, operation, run_id, method, path, status, at,"
                    " prev_hash, hash FROM audit_log WHERE id > ? ORDER BY id",
                    (int(after_id),),
                ).fetchall()
        return [
            {
                "id": int(r[0]),
                "correlation_id": r[1],
                "tenant_id": r[2],
                "principal_type": r[3],
                "principal_id": r[4],
                "product_id": r[5],
                "operation": r[6],
                "run_id": r[7],
                "method": r[8],
                "path": r[9],
                "status": r[10],
                "at": r[11],
                "prev_hash": r[12],
                "hash": r[13],
            }
            for r in rows
        ]

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
