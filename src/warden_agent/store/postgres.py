"""PostgreSQL 存储实现：和 SqliteStore 实现同一个 RunStore 接口，可互换替换。

  - 本地开发/单机：用 SqliteStore（零配置，一个文件）。
  - 上云 / 多机 / 生产：换 PostgresStore（连数据库服务器），上层一行不改。

用法（需要先有 PostgreSQL 服务器，或用 docker 起一个）：
    store = PostgresStore(host="localhost", port=5432,
                          dbname="warden", user="postgres", password="xxx")
    app = build_app(model=..., catalog=..., policy=..., store=store)

依赖：psycopg（PostgreSQL 驱动）。要启用 Postgres 才需要装：
    pip install "psycopg[binary]"
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from warden_agent.core.run.status import AgentRun, RunStatus
from warden_agent.credential.vault import (
    StoredCredential,
    StoredLease,
    decode_fields,
    encode_fields,
)
from warden_agent.model.model import Message, ToolCall


def _now_iso() -> str:
    """当前 UTC 时间的 ISO 字符串（秒级），与 SqliteStore 语义一致。"""
    return datetime.now(UTC).isoformat(timespec="seconds")


class PostgresStore:
    """PostgreSQL 持久化实现，接口与 SqliteStore 一致（见 store/base.py）。"""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 5432,
        dbname: str = "warden",
        user: str = "postgres",
        password: str = "",
        connect_timeout: int = 10,
    ) -> None:
        # 延迟导入：只有真要连 Postgres 时才需要 psycopg 已安装
        import psycopg

        self._psycopg = psycopg
        self.conn = psycopg.connect(
            host=host, port=port, dbname=dbname,
            user=user, password=password, connect_timeout=connect_timeout,
        )
        self._init_schema()

    def _init_schema(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id        SERIAL PRIMARY KEY,
                    run_id    TEXT NOT NULL,
                    role      TEXT NOT NULL,
                    content   TEXT NOT NULL,
                    tool_call TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pending_approvals (
                    run_id      TEXT PRIMARY KEY,
                    approval_id TEXT NOT NULL,
                    tool_name   TEXT NOT NULL,
                    arguments   TEXT NOT NULL,
                    reason      TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS approval_history (
                    id          SERIAL PRIMARY KEY,
                    run_id      TEXT NOT NULL,
                    approval_id TEXT NOT NULL,
                    tool_name   TEXT NOT NULL,
                    arguments   TEXT,
                    decision    TEXT NOT NULL,
                    created_at  TEXT NOT NULL
                )
            """)
            # 老库补列：会话最后活跃时间（对话列表展示用）
            cur.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS updated_at TEXT")
            # 老库补列：会话归属用户（多用户隔离）
            cur.execute(
                "ALTER TABLE runs ADD COLUMN IF NOT EXISTS user_id TEXT NOT NULL DEFAULT ''"
            )
            # 用户表（中控台账号）
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id    TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL
                )
            """)
            # 检查点（与 SqliteStore 对齐：单查 + 枚举，供跨 Run 恢复使用）
            cur.execute("""
                CREATE TABLE IF NOT EXISTS checkpoints (
                    run_id TEXT PRIMARY KEY,
                    data   TEXT NOT NULL
                )
            """)
            # 跨副本共享状态：幂等 / 事件流 / 限流计数
            cur.execute("""
                CREATE TABLE IF NOT EXISTS idempotency (
                    key        TEXT PRIMARY KEY,
                    payload    TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS run_events (
                    id         BIGSERIAL PRIMARY KEY,
                    run_id     TEXT NOT NULL,
                    data       TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_run_events_run ON run_events (run_id, id)"
            )
            cur.execute("""
                CREATE TABLE IF NOT EXISTS rate_limits (
                    bucket_key   TEXT PRIMARY KEY,
                    window_start DOUBLE PRECISION NOT NULL,
                    count        BIGINT NOT NULL
                )
            """)
            # 凭证密文与租约（与 SqliteStore 对齐，见 credential/vault.py）。
            # 存的是 AES-GCM 密文，明文不落这张表。
            cur.execute("""
                CREATE TABLE IF NOT EXISTS credentials (
                    scope      TEXT NOT NULL,
                    name       TEXT NOT NULL,
                    data       TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (scope, name)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS credential_leases (
                    scope      TEXT NOT NULL,
                    lease_id   TEXT NOT NULL,
                    name       TEXT NOT NULL,
                    issued_at  TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    PRIMARY KEY (scope, lease_id)
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_credential_leases_expiry "
                "ON credential_leases (scope, expires_at)"
            )
        self.conn.commit()

    # ---- Run 状态 ----
    def save_run(self, run: AgentRun) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO runs (run_id, status, user_id, updated_at) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (run_id) DO UPDATE SET status = EXCLUDED.status, "
                "updated_at = EXCLUDED.updated_at",
                (run.run_id, run.status.name, run.user_id, _now_iso()),
            )
        self.conn.commit()

    def load_run(self, run_id: str) -> AgentRun | None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT status, user_id FROM runs WHERE run_id = %s", (run_id,))
            row = cur.fetchone()
        if row is None:
            return None
        run = AgentRun(run_id, user_id=row[1] or "")
        run.status = RunStatus[row[0]]
        return run

    def delete_run(self, run_id: str) -> None:
        """删除整个会话：对话、待审批、checkpoint、事件、状态一并清掉（与 SqliteStore 一致）。"""
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM messages WHERE run_id = %s", (run_id,))
            cur.execute("DELETE FROM pending_approvals WHERE run_id = %s", (run_id,))
            cur.execute("DELETE FROM checkpoints WHERE run_id = %s", (run_id,))
            cur.execute("DELETE FROM run_events WHERE run_id = %s", (run_id,))
            cur.execute("DELETE FROM runs WHERE run_id = %s", (run_id,))
        self.conn.commit()

    def record_approval_decision(
        self,
        run_id: str,
        approval_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        decision: str,
    ) -> None:
        """记录一条审批决策（approved / rejected），供历史追溯。"""
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO approval_history "
                "(run_id, approval_id, tool_name, arguments, decision, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (run_id, approval_id, tool_name,
                 json.dumps(arguments, ensure_ascii=False), decision, _now_iso()),
            )
        self.conn.commit()

    def list_approval_history(
        self, limit: int = 20, owner: str | None = None
    ) -> list[dict[str, Any]]:
        """审批决策历史（最新的在前）。owner 给定时只返回该用户名下 Run 的决策。"""
        with self.conn.cursor() as cur:
            if owner:
                cur.execute(
                    "SELECT h.run_id, h.approval_id, h.tool_name, h.decision, h.created_at "
                    "FROM approval_history h JOIN runs r ON r.run_id = h.run_id "
                    "WHERE r.user_id = %s ORDER BY h.id DESC LIMIT %s",
                    (owner, limit),
                )
            else:
                cur.execute(
                    "SELECT run_id, approval_id, tool_name, decision, created_at "
                    "FROM approval_history ORDER BY id DESC LIMIT %s",
                    (limit,),
                )
            rows = cur.fetchall()
        return [
            {"run_id": r[0], "approval_id": r[1], "tool_name": r[2],
             "decision": r[3], "created_at": r[4]}
            for r in rows
        ]

    def list_runs(self, limit: int = 50, owner: str | None = None) -> list[dict[str, Any]]:
        """列出会话概要（前端会话列表用）：按最近活跃排序，语义与 SqliteStore 一致。

        owner 给定时在应用层按归属过滤（与 SqliteStore 一致，避免动态拼 SQL）。
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.run_id,
                       r.status,
                       (SELECT COUNT(*) FROM messages m WHERE m.run_id = r.run_id) AS msg_count,
                       (SELECT m.content FROM messages m
                         WHERE m.run_id = r.run_id AND m.role = 'user'
                         ORDER BY m.id LIMIT 1) AS title,
                       (SELECT MAX(m.id) FROM messages m WHERE m.run_id = r.run_id) AS last_id,
                       r.updated_at,
                       r.user_id
                FROM runs r
                ORDER BY last_id DESC NULLS LAST
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
        out = [
            {
                "run_id": r[0],
                "status": r[1],
                "msg_count": r[2],
                "title": ((r[3] or r[0]) or "")[:60],
                "updated_at": r[5],
                "user_id": r[6],
            }
            for r in rows
        ]
        if owner:
            out = [r for r in out if r["user_id"] == owner]
        return out

    # ---- 用户（中控台账号）----
    def create_user(self, user_id: str) -> None:
        """登记一个中控台用户（幂等：已存在则不动）。"""
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (user_id, created_at) VALUES (%s, %s) "
                "ON CONFLICT (user_id) DO NOTHING",
                (user_id, _now_iso()),
            )
        self.conn.commit()

    def list_users(self) -> list[dict[str, Any]]:
        """已登记的用户列表（按创建时间）。"""
        with self.conn.cursor() as cur:
            cur.execute("SELECT user_id, created_at FROM users ORDER BY created_at")
            rows = cur.fetchall()
        return [{"user_id": r[0], "created_at": r[1]} for r in rows]

    # ---- 对话消息 ----
    def append_message(self, run_id: str, message: Message) -> None:
        """追加一条消息。入库前按会话 + 角色 + 内容 + 工具调用查重，完全相同的不重复落库。"""
        tool_json = (
            json.dumps(message.tool_call.to_dict())
            if message.tool_call else None
        )
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM messages "
                "WHERE run_id = %s AND role = %s AND content = %s "
                "AND COALESCE(tool_call, '') = COALESCE(%s, '') LIMIT 1",
                (run_id, message.role, message.content, tool_json),
            )
            if cur.fetchone() is not None:
                return  # 已有完全相同的一条，跳过
            cur.execute(
                "INSERT INTO messages (run_id, role, content, tool_call) "
                "VALUES (%s, %s, %s, %s)",
                (run_id, message.role, message.content, tool_json),
            )
        self.conn.commit()

    def load_messages(self, run_id: str) -> list[Message]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT role, content, tool_call FROM messages "
                "WHERE run_id = %s ORDER BY id",
                (run_id,),
            )
            rows = cur.fetchall()
        out: list[Message] = []
        for role, content, tool_json in rows:
            tool_call = None
            if tool_json:
                try:
                    tool_call = ToolCall.from_dict(json.loads(tool_json))
                except json.JSONDecodeError:
                    tool_call = None
            out.append(Message(role=role, content=content, tool_call=tool_call))
        return out

    # ---- 待审批 ----
    def save_pending_approval(
        self,
        run_id: str,
        approval_id: str,
        tool_name: str,
        arguments: dict[str, object],
        reason: str,
    ) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pending_approvals "
                "(run_id, approval_id, tool_name, arguments, reason) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (run_id) DO UPDATE SET "
                "approval_id = EXCLUDED.approval_id, tool_name = EXCLUDED.tool_name, "
                "arguments = EXCLUDED.arguments, reason = EXCLUDED.reason",
                (run_id, approval_id, tool_name, json.dumps(arguments), reason),
            )
        self.conn.commit()

    def load_pending_approval(
        self, run_id: str
    ) -> tuple[str, str, dict[str, object], str] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT approval_id, tool_name, arguments, reason "
                "FROM pending_approvals WHERE run_id = %s",
                (run_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        try:
            args = json.loads(row[2])
        except json.JSONDecodeError:
            args = {}
        return row[0], row[1], args if isinstance(args, dict) else {}, row[3]

    def clear_pending_approval(self, run_id: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM pending_approvals WHERE run_id = %s", (run_id,)
            )
        self.conn.commit()

    # ---- 检查点（与 SqliteStore 对齐）----
    def save_checkpoint(self, checkpoint: object) -> None:
        from warden_agent.runtime.checkpoint import Checkpoint

        assert isinstance(checkpoint, Checkpoint)
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO checkpoints (run_id, data) VALUES (%s, %s) "
                "ON CONFLICT (run_id) DO UPDATE SET data = EXCLUDED.data",
                (checkpoint.run_id, json.dumps(checkpoint.to_dict(), ensure_ascii=False)),
            )
        self.conn.commit()

    def load_checkpoint(self, run_id: str) -> object | None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT data FROM checkpoints WHERE run_id = %s", (run_id,))
            row = cur.fetchone()
        return None if row is None else self._decode_checkpoint(row[0])

    def list_checkpoints(self) -> list[object]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT data FROM checkpoints ORDER BY run_id")
            rows = cur.fetchall()
        out: list[object] = []
        for (raw,) in rows:
            cp = self._decode_checkpoint(raw)
            if cp is not None:
                out.append(cp)
        return out

    @staticmethod
    def _decode_checkpoint(raw: object) -> object | None:
        from warden_agent.runtime.checkpoint import Checkpoint

        try:
            obj = json.loads(str(raw))
        except json.JSONDecodeError:
            return None
        return Checkpoint.from_dict(obj) if isinstance(obj, dict) else None

    # ---- 跨副本共享状态（幂等 / 事件流 / 限流计数）----
    def get_idempotent(self, key: str) -> str | None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT payload FROM idempotency WHERE key = %s", (key,))
            row = cur.fetchone()
        return None if row is None else str(row[0])

    def save_idempotent(self, key: str, payload: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO idempotency (key, payload, created_at) VALUES (%s, %s, %s) "
                "ON CONFLICT (key) DO UPDATE SET payload = EXCLUDED.payload",
                (key, payload, _now_iso()),
            )
        self.conn.commit()

    def append_event(self, run_id: str, payload: str) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO run_events (run_id, data, created_at) VALUES (%s, %s, %s) "
                "RETURNING id",
                (run_id, payload, _now_iso()),
            )
            row = cur.fetchone()
        self.conn.commit()
        return int(row[0]) if row is not None else 0

    def list_events_after(
        self, run_id: str, after_seq: int, limit: int = 200
    ) -> list[tuple[int, str]]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT id, data FROM run_events WHERE run_id = %s AND id > %s "
                "ORDER BY id LIMIT %s",
                (run_id, after_seq, limit),
            )
            rows = cur.fetchall()
        return [(int(r[0]), str(r[1])) for r in rows]

    def hit_rate_limit(
        self, bucket_key: str, window_seconds: int, now: float
    ) -> tuple[int, float]:
        """固定窗口计数 +1。用单条 UPSERT 完成"过期则重置、否则累加"，避免读改写竞态。"""
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO rate_limits (bucket_key, window_start, count) "
                "VALUES (%s, %s, 1) "
                "ON CONFLICT (bucket_key) DO UPDATE SET "
                "  count = CASE WHEN %s - rate_limits.window_start >= %s "
                "               THEN 1 ELSE rate_limits.count + 1 END, "
                "  window_start = CASE WHEN %s - rate_limits.window_start >= %s "
                "                      THEN %s ELSE rate_limits.window_start END "
                "RETURNING count, window_start",
                (bucket_key, now, now, window_seconds, now, window_seconds, now),
            )
            row = cur.fetchone()
        self.conn.commit()
        return (int(row[0]), float(row[1])) if row is not None else (1, now)

    def close(self) -> None:
        self.conn.close()

    # ---- 凭证保管库（CredentialVault 协议，见 credential/vault.py）----
    def save_credential(self, credential: StoredCredential) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO credentials (scope, name, data, updated_at) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (scope, name) DO UPDATE SET "
                "data = EXCLUDED.data, updated_at = EXCLUDED.updated_at",
                (
                    credential.scope,
                    credential.name,
                    encode_fields(credential.encrypted),
                    _now_iso(),
                ),
            )
        self.conn.commit()

    def load_credential(self, scope: str, name: str) -> StoredCredential | None:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT data FROM credentials WHERE scope = %s AND name = %s",
                (scope, name),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return StoredCredential(scope=scope, name=name, encrypted=decode_fields(row[0]))

    def delete_credential(self, scope: str, name: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM credentials WHERE scope = %s AND name = %s", (scope, name)
            )
        self.conn.commit()

    def list_credential_names(self, scope: str) -> list[str]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT name FROM credentials WHERE scope = %s ORDER BY name", (scope,)
            )
            rows = cur.fetchall()
        return [str(r[0]) for r in rows]

    def save_credential_lease(self, lease: StoredLease) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO credential_leases "
                "(scope, lease_id, name, issued_at, expires_at) VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (scope, lease_id) DO UPDATE SET "
                "name = EXCLUDED.name, issued_at = EXCLUDED.issued_at, "
                "expires_at = EXCLUDED.expires_at",
                (
                    lease.scope,
                    lease.lease_id,
                    lease.name,
                    lease.issued_at.isoformat(),
                    lease.expires_at.isoformat(),
                ),
            )
        self.conn.commit()

    def load_credential_lease(self, scope: str, lease_id: str) -> StoredLease | None:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT name, issued_at, expires_at FROM credential_leases "
                "WHERE scope = %s AND lease_id = %s",
                (scope, lease_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return StoredLease(
            scope=scope,
            lease_id=lease_id,
            name=str(row[0]),
            issued_at=datetime.fromisoformat(str(row[1])),
            expires_at=datetime.fromisoformat(str(row[2])),
        )

    def delete_credential_lease(self, scope: str, lease_id: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM credential_leases WHERE scope = %s AND lease_id = %s",
                (scope, lease_id),
            )
        self.conn.commit()

    def purge_expired_credential_leases(self, scope: str, now: datetime) -> int:
        """删掉某作用域下已过期的租约记录，返回删除条数（惰性清理）。"""
        with self.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM credential_leases WHERE scope = %s AND expires_at <= %s",
                (scope, now.isoformat()),
            )
            count = cur.rowcount
        self.conn.commit()
        return int(count or 0)
