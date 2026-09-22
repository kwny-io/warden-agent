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

    backend = "postgres"

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
        # autocommit=True 是刻意的，有两个原因（都是 Postgres 与 SQLite 的行为差异）：
        #  1) **避免"毒丸连接"**：Postgres 里事务内任一语句报错后，整个事务进入 aborted 状态，
        #     此后**所有**语句（包括读、甚至健康探针）都会失败，直到有人 rollback。
        #     本项目此前没有任何 rollback()，一次坏写就能把这条连接废掉、且不会自愈。
        #     SQLite 没有这个语义（错的只是那一条语句），所以这个坑只在 Postgres 上暴露。
        #  2) 读操作不再长期占着 idle-in-transaction（长连接下的常见运维问题）。
        # 需要原子性的多语句写入，用 `with self.conn.transaction():` 显式开事务块
        # （psycopg3 在 autocommit 模式下也支持），见 delete_run / append_message。
        # 注：各写方法末尾原本的 `self.conn.commit()` 在 autocommit 下是无害的空操作，
        # 保留它不影响语义（单语句写入已经即时提交）。
        # 连接参数留一份：需要**另开连接**时用（例如 LISTEN/NOTIFY 的事件总线
        # 要独占一条连接，不能和 store 自己的读写抢同一条）
        self._connect_kwargs: dict[str, Any] = {
            "host": host, "port": port, "dbname": dbname,
            "user": user, "password": password, "connect_timeout": connect_timeout,
        }
        self.conn = psycopg.connect(**self._connect_kwargs, autocommit=True)
        self._init_schema()

    def new_connection(self) -> Any:
        """再开一条 autocommit 连接（调用方负责关）。

        用途：LISTEN/NOTIFY 需要一条**专门等待通知**的连接——等待期间它被占住，
        不能和 store 的读写共用（否则会互相阻塞）。
        """
        return self._psycopg.connect(**self._connect_kwargs, autocommit=True)

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
                    reason      TEXT NOT NULL,
                    -- 进入"等待审批"的时刻：告警算"等了多久"要准确，不能靠 run 的最后活动时间近似
                    created_at  TEXT
                )
            """)
            cur.execute(
                "ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS created_at TEXT"
            )
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
            # Run 级锁（租约式）：多副本下防止同一个 run 被两个副本同时驱动。
            # 带 expires_at：持有者崩了无需人工解锁，租约到期即可被接管。
            cur.execute("""
                CREATE TABLE IF NOT EXISTS run_locks (
                    run_id      TEXT PRIMARY KEY,
                    owner       TEXT NOT NULL,
                    acquired_at DOUBLE PRECISION NOT NULL,
                    expires_at  DOUBLE PRECISION NOT NULL
                )
            """)
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
        """删除整个会话：对话、待审批、checkpoint、事件、状态一并清掉（与 SqliteStore 一致）。

        五条 DELETE 必须**同生共死**，所以显式开事务块（autocommit 模式下 `transaction()`
        会真的发 BEGIN/COMMIT）。中途失败则整体回滚，不会留下删了一半的会话。
        """
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute("DELETE FROM messages WHERE run_id = %s", (run_id,))
            cur.execute("DELETE FROM pending_approvals WHERE run_id = %s", (run_id,))
            cur.execute("DELETE FROM checkpoints WHERE run_id = %s", (run_id,))
            cur.execute("DELETE FROM run_events WHERE run_id = %s", (run_id,))
            cur.execute("DELETE FROM runs WHERE run_id = %s", (run_id,))

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
        # "查重 + 插入"是一对读改写，放进同一个事务块里（autocommit 模式下显式 BEGIN/COMMIT），
        # 否则查完与写之间可能被别的写入插进来，查重的意义就打折了。
        with self.conn.transaction(), self.conn.cursor() as cur:
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
                "(run_id, approval_id, tool_name, arguments, reason, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (run_id) DO UPDATE SET "
                "approval_id = EXCLUDED.approval_id, tool_name = EXCLUDED.tool_name, "
                "arguments = EXCLUDED.arguments, reason = EXCLUDED.reason, "
                "created_at = EXCLUDED.created_at",
                (
                    run_id, approval_id, tool_name,
                    json.dumps(arguments), reason, _now_iso(),
                ),
            )
        self.conn.commit()

    def pending_approval_created_at(self, run_id: str) -> str | None:
        """该 run 进入"等待审批"的时刻（ISO 字符串）；没有待审批或老数据没记则 None。"""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT created_at FROM pending_approvals WHERE run_id = %s", (run_id,)
            )
            row = cur.fetchone()
        if row is None or row[0] is None:
            return None
        return str(row[0])

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

    def reserve_idempotent(self, key: str, payload: str) -> bool:
        """原子占位：`ON CONFLICT DO NOTHING` + rowcount 判断是否占到（防并发 TOCTOU）。"""
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO idempotency (key, payload, created_at) VALUES (%s, %s, %s) "
                "ON CONFLICT (key) DO NOTHING",
                (key, payload, _now_iso()),
            )
            reserved = cur.rowcount == 1
        self.conn.commit()
        return reserved

    def release_idempotent(self, key: str, payload: str) -> None:
        """仅当内容仍是那条占位时才删（否则会把已缓存的响应快照删掉）。"""
        with self.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM idempotency WHERE key = %s AND payload = %s", (key, payload)
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

    # ---- Run 级锁（租约式；RunLockStore 协议，见 runtime/locking.py）----
    #
    # 取锁是**单条原子语句**：`ON CONFLICT ... DO UPDATE ... WHERE expires_at <= now`
    # —— 键存在且租约未过期时不更新（抢不到）；已过期则被接管。语句本身原子，
    # 所以并发抢同一把锁只有一方能拿到；随后读回来核对 (owner, expires_at) 确认归属。
    # 注：autocommit 模式下「UPSERT + 读回」是两条语句，但不存在竞态——我们写入的
    # expires_at 在未来，别人只有在其过期后（即 <= now）才可能接管。
    def acquire_run_lock(
        self, run_id: str, owner: str, expires_at: float, now: float
    ) -> bool:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO run_locks (run_id, owner, acquired_at, expires_at) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (run_id) DO UPDATE SET "
                "  owner = EXCLUDED.owner, "
                "  acquired_at = EXCLUDED.acquired_at, "
                "  expires_at = EXCLUDED.expires_at "
                "WHERE run_locks.expires_at <= %s",
                (run_id, owner, now, expires_at, now),
            )
            cur.execute(
                "SELECT owner, expires_at FROM run_locks WHERE run_id = %s", (run_id,)
            )
            row = cur.fetchone()
        if row is None:
            return False
        return str(row[0]) == owner and float(row[1]) == expires_at

    def renew_run_lock(
        self, run_id: str, owner: str, expires_at: float, now: float
    ) -> bool:
        """续租：只有**当前持有者且未过期**才能续（避免续到别人的锁上）。"""
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE run_locks SET expires_at = %s "
                "WHERE run_id = %s AND owner = %s AND expires_at > %s",
                (expires_at, run_id, owner, now),
            )
            changed = cur.rowcount
        return int(changed or 0) > 0

    def release_run_lock(self, run_id: str, owner: str) -> None:
        """释放：只删自己的锁（owner 不匹配时不动，防止误删他人的锁）。"""
        with self.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM run_locks WHERE run_id = %s AND owner = %s", (run_id, owner)
            )

    def run_lock_owner(self, run_id: str, now: float) -> str | None:
        """当前持有者（已过期视为无人持有，并顺手清掉那行）。"""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT owner, expires_at FROM run_locks WHERE run_id = %s", (run_id,)
            )
            row = cur.fetchone()
            if row is None:
                return None
            if float(row[1]) <= now:
                cur.execute("DELETE FROM run_locks WHERE run_id = %s", (run_id,))
                return None
            return str(row[0])

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
