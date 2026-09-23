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

import contextlib
import functools
import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Concatenate, cast

from warden_agent.core.run.status import AgentRun, RunStatus
from warden_agent.credential.vault import (
    StoredCredential,
    StoredLease,
    decode_fields,
    encode_fields,
)
from warden_agent.model.model import Message, ToolCall
from warden_agent.store.codec import (
    decode_versioned,
    encode_versioned,
    normalize_utc_iso,
)


def _now_iso() -> str:
    """当前 UTC 时间的 ISO 字符串（秒级），与 SqliteStore 语义一致。"""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _reconnecting[**P, R](
    method: Callable[Concatenate[PostgresStore, P], R],
) -> Callable[Concatenate[PostgresStore, P], R]:
    """给**会碰到连接**的方法套一层「连接坏了就重连并有限重试」。

    为什么需要它（这是修过的一个真问题）：`PostgresStore` 是**单连接**设计，一条 `self.conn`
    坏了（网络闪断、DB 重启、连接被服务端回收）之后，此前每个方法都会直接抛
    `OperationalError`——而且**不会自愈**，直到进程重启。SQLite 版没有这个问题（本地文件
    没有网络），所以这个坑只在 PG 上暴露。

    为什么是装饰器而不是改每个方法体：和 `store/sqlite.py` 的 `_locked` 同款——diff 集中在
    一处、不误伤方法体里的 SQL 与事务逻辑（那些正是"毒丸连接"防护的所在，不能动）。

    边界（诚实说明）：只对**连接类错误**重连重试；SQL 语法错、约束冲突等非连接错误原样抛出，
    不会被当成"连接坏了"而无限重试。重试用尽仍失败 → 原样 re-raise（不吞异常）。

    泛型参数（PEP 695）必须保留签名，否则 mypy 会把被装饰方法的返回值退化成 Any、
    连累所有调用点（`store/sqlite.py` 里那种错一次报了十几条）。
    """
    @functools.wraps(method)
    def wrapper(self: PostgresStore, *args: P.args, **kwargs: P.kwargs) -> R:
        attempt = 0
        while True:
            try:
                return method(self, *args, **kwargs)
            except Exception as exc:
                # 非连接错，或重试次数已用尽 → 原样抛（绝不吞真失败）。
                if attempt >= self._RECONNECT_ATTEMPTS or not self._is_connection_error(exc):
                    raise
                attempt += 1
                self._reconnect()
    # functools.wraps 的静态返回类型是 `_Wrapped[...]`，与声明的 Callable 形式不完全等价；
    # 它保留的正是原签名，所以这里显式 cast 一次即可（同 sqlite._locked）。
    return cast("Callable[Concatenate[PostgresStore, P], R]", wrapper)


class PostgresStore:
    """PostgreSQL 持久化实现，接口与 SqliteStore 一致（见 store/base.py）。"""

    backend = "postgres"
    # 目标 schema 版本（与 SqliteStore 对齐；每次加表/加列 +1）
    _SCHEMA_VERSION = 4
    # 连接坏掉后的**有界**重连重试次数（不含首次尝试）。刻意很小：
    # 重连本应几乎必成，连续失败说明 DB 真的不可用，尽早把错误抛给上层比死等更好。
    _RECONNECT_ATTEMPTS = 2
    # 期望的会话级 row_factory（默认 None = 用 psycopg 自带 tuple_row）。
    # 重连时按它重新应用：否则将来若有人改成 dict_row，坏一次连接就会静默退回元组行。
    _row_factory: Any = None

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
        self.conn = self._connect()
        # 一把 RLock 串行化**事务块**路径。为什么需要（SQLite 版也有同款 `_locked`）：
        # psycopg 的单条语句本身受连接内部锁保护，但 `with conn.transaction():` 这种
        # **事务块**在多线程下会在同一条连接上互相嵌套成 savepoint，交错时抛
        # `OutOfOrderTransactionNesting`，甚至可能让一个线程的 COMMIT 提交另一个线程的半成品。
        # FastAPI 的同步端点跑在线程池里，所以"同一条连接被两个请求并发用"是常态。
        self._lock = threading.RLock()
        # 本进程见过的最大限流窗口（hit_rate_limit 记录）。
        # 用于让 purge_stale_rate_limits 判断"窗口是否还可能活着"（见该方法不变量）。
        self._rate_window_seconds = 0.0
        self._init_schema()

    @_reconnecting
    def ping(self) -> None:
        """健康检查探针：执行一句无害查询确认连接可用。

        **必须存在**：`/health/ready` 会对存储调 `ping()`（见 web/health.py）；此前
        `PostgresStore` 没有这个方法 → 探针抛 `AttributeError` 被吞掉 → **PG 部署下就绪探针
        永远 503**，K8s pod 永远不 Ready、LB 不路由。SQLite 版一直有 `ping`，所以只坑 PG。
        """
        with self.conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()

    def new_connection(self) -> Any:
        """再开一条 autocommit 连接（调用方负责关）。

        用途：LISTEN/NOTIFY 需要一条**专门等待通知**的连接——等待期间它被占住，
        不能和 store 的读写共用（否则会互相阻塞）。
        """
        psycopg = self._psycopg
        return psycopg.connect(**self._connect_kwargs, autocommit=True)

    def _connect(self) -> Any:
        """建立一条连接并**一次性应用全部会话设置**。

        单独抽出是给重连用的：连接坏掉后重建时必须把 autocommit、row_factory 这些
        会话级设置重新应用一遍，否则重连出来的连接行为会和首连不一致（例如 autocommit
        丢了就又回到"毒丸连接"那条老路）。
        """
        psycopg = self._psycopg
        # autocommit=True 每次连接都显式传入 —— 重连后也必须保持，否则又回到
        # 事务内报错就污染整条连接的老路。
        conn = psycopg.connect(**self._connect_kwargs, autocommit=True)
        # row_factory 是会话级设置，重连时要重新应用（默认 None 时不动，保持 psycopg 默认）。
        if self._row_factory is not None:
            conn.row_factory = self._row_factory
        return conn

    def _is_connection_error(self, exc: BaseException) -> bool:
        """该异常是否属于"连接已坏、重连后应可恢复"这一类。

        只认 `OperationalError`（psycopg 用它在连接关闭 / 网络断开 / 服务端回收时抛）与
        `InterfaceError`。SQL 语法错、约束冲突、序列化错等**不是**连接错，不应触发重连重试。
        """
        psycopg = self._psycopg
        operational = getattr(psycopg, "OperationalError", None)
        if isinstance(operational, type) and isinstance(exc, operational):
            return True
        interface = getattr(psycopg, "InterfaceError", None)
        return isinstance(interface, type) and isinstance(exc, interface)

    def _reconnect(self) -> None:
        """关掉坏连接、重建一条（并重新应用会话设置）。

        旧连接 close 失败（很多情况下它已经坏了）不影响重连，故忽略其异常。
        """
        old = self.conn
        with contextlib.suppress(Exception):
            old.close()
        self.conn = self._connect()

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
            # 保留清扫按 created_at 扫描：没索引会随幂等记录量线性变慢
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_idempotency_created "
                "ON idempotency (created_at)"
            )
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
            # 保留清扫按 window_start 扫描：没索引会随限流桶数量线性变慢
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_rate_limits_window "
                "ON rate_limits (window_start)"
            )
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
            # schema 版本（与 SqliteStore 对齐）：单行表，每次启动记录目标版本。
            # PG 的补列用 `ADD COLUMN IF NOT EXISTS`——它本身就是幂等的、不会用异常做控制流，
            # 所以不需要 SQLite 那套 pragma 判定。
            cur.execute("""
                CREATE TABLE IF NOT EXISTS __schema_version__ (
                    version    INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
            """)
            cur.execute("DELETE FROM __schema_version__")
            cur.execute(
                "INSERT INTO __schema_version__ (version, applied_at) VALUES (%s, %s)",
                (self._SCHEMA_VERSION, _now_iso()),
            )
        self.conn.commit()

    @_reconnecting
    def schema_version(self) -> int:
        """当前 schema 版本（真实记录，不再硬编码在别处）。"""
        with self.conn.cursor() as cur:
            cur.execute("SELECT version FROM __schema_version__ LIMIT 1")
            row = cur.fetchone()
        return int(row[0]) if row else 0

    # ---- Run 状态 ----
    @_reconnecting
    def save_run(self, run: AgentRun) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO runs (run_id, status, user_id, updated_at) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (run_id) DO UPDATE SET status = EXCLUDED.status, "
                "updated_at = EXCLUDED.updated_at",
                (run.run_id, run.status.name, run.user_id, _now_iso()),
            )
        self.conn.commit()

    @_reconnecting
    def load_run(self, run_id: str) -> AgentRun | None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT status, user_id FROM runs WHERE run_id = %s", (run_id,))
            row = cur.fetchone()
        if row is None:
            return None
        run = AgentRun(run_id, user_id=row[1] or "")
        run.status = RunStatus[row[0]]
        return run

    @_reconnecting
    def delete_run(self, run_id: str) -> None:
        """删除整个会话：对话、待审批、checkpoint、事件、状态一并清掉（与 SqliteStore 一致）。

        五条 DELETE 必须**同生共死**，所以显式开事务块（autocommit 模式下 `transaction()`
        会真的发 BEGIN/COMMIT）。中途失败则整体回滚，不会留下删了一半的会话。

        ⚠️ 持锁执行：`transaction()` 不可在多线程间嵌套同一条连接（见 __init__ 的说明）。
        """
        with self._lock, self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute("DELETE FROM messages WHERE run_id = %s", (run_id,))
            cur.execute("DELETE FROM pending_approvals WHERE run_id = %s", (run_id,))
            cur.execute("DELETE FROM checkpoints WHERE run_id = %s", (run_id,))
            cur.execute("DELETE FROM run_events WHERE run_id = %s", (run_id,))
            cur.execute("DELETE FROM runs WHERE run_id = %s", (run_id,))

    @_reconnecting
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

    @_reconnecting
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

    @_reconnecting
    def list_runs(self, limit: int = 50, owner: str | None = None) -> list[dict[str, Any]]:
        """列出会话概要（前端会话列表用）：按最近活跃排序，语义与 SqliteStore 一致。

        owner 给定时的过滤**下推到 SQL 的 WHERE**（与 SqliteStore 一致）——取完 LIMIT n
        再在应用层过滤会"先截断再筛"，返回条数少于 n，即使该 owner 还有更多匹配。
        """
        sql = (
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
            """
        )
        params: list[object] = []
        if owner:
            sql += " WHERE r.user_id = %s"
            params.append(owner)
        sql += " ORDER BY last_id DESC NULLS LAST LIMIT %s"
        params.append(limit)
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
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
        return out

    # ---- 用户（中控台账号）----
    @_reconnecting
    def create_user(self, user_id: str) -> None:
        """登记一个中控台用户（幂等：已存在则不动）。"""
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (user_id, created_at) VALUES (%s, %s) "
                "ON CONFLICT (user_id) DO NOTHING",
                (user_id, _now_iso()),
            )
        self.conn.commit()

    @_reconnecting
    def list_users(self) -> list[dict[str, Any]]:
        """已登记的用户列表（按创建时间）。"""
        with self.conn.cursor() as cur:
            cur.execute("SELECT user_id, created_at FROM users ORDER BY created_at")
            rows = cur.fetchall()
        return [{"user_id": r[0], "created_at": r[1]} for r in rows]

    # ---- 对话消息 ----
    @_reconnecting
    def append_message(self, run_id: str, message: Message) -> None:
        """追加一条消息。入库前按会话 + 角色 + 内容 + 工具调用查重，完全相同的不重复落库。"""
        # 与 SqliteStore 共用同一编码助手，保证跨后端落盘格式（含版本前缀）一致
        tool_json = (
            encode_versioned(message.tool_call.to_dict())
            if message.tool_call else None
        )
        # "查重 + 插入"是一对读改写，放进同一个事务块里（autocommit 模式下显式 BEGIN/COMMIT），
        # 否则查完与写之间可能被别的写入插进来，查重的意义就打折了。
        with self._lock, self.conn.transaction(), self.conn.cursor() as cur:
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

    @_reconnecting
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
                    obj = decode_versioned(str(tool_json))
                    tool_call = ToolCall.from_dict(obj) if isinstance(obj, dict) else None
                except (json.JSONDecodeError, KeyError):
                    # KeyError：未知版本；单行坏数据不能拖垮整次加载。
                    tool_call = None
            out.append(Message(role=role, content=content, tool_call=tool_call))
        return out

    # ---- 待审批 ----
    @_reconnecting
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
                    encode_versioned(arguments), reason, _now_iso(),
                ),
            )
        self.conn.commit()

    @_reconnecting
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

    @_reconnecting
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
        args: object = {}
        try:
            args = decode_versioned(str(row[2]))
        except (json.JSONDecodeError, IndexError, KeyError):
            args = {}
        return row[0], row[1], args if isinstance(args, dict) else {}, row[3]

    @_reconnecting
    def clear_pending_approval(self, run_id: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM pending_approvals WHERE run_id = %s", (run_id,)
            )
        self.conn.commit()

    # ---- 检查点（与 SqliteStore 对齐）----
    @_reconnecting
    def save_checkpoint(self, checkpoint: object) -> None:
        from warden_agent.runtime.checkpoint import Checkpoint

        assert isinstance(checkpoint, Checkpoint)
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO checkpoints (run_id, data) VALUES (%s, %s) "
                "ON CONFLICT (run_id) DO UPDATE SET data = EXCLUDED.data",
                (checkpoint.run_id, encode_versioned(checkpoint.to_dict())),
            )
        self.conn.commit()

    @_reconnecting
    def load_checkpoint(self, run_id: str) -> object | None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT data FROM checkpoints WHERE run_id = %s", (run_id,))
            row = cur.fetchone()
        return None if row is None else self._decode_checkpoint(row[0])

    @_reconnecting
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
            obj = decode_versioned(str(raw))
        except (json.JSONDecodeError, KeyError):
            return None
        return Checkpoint.from_dict(obj) if isinstance(obj, dict) else None

    # ---- 跨副本共享状态（幂等 / 事件流 / 限流计数）----
    @_reconnecting
    def get_idempotent(self, key: str) -> str | None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT payload FROM idempotency WHERE key = %s", (key,))
            row = cur.fetchone()
        return None if row is None else str(row[0])

    @_reconnecting
    def save_idempotent(self, key: str, payload: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO idempotency (key, payload, created_at) VALUES (%s, %s, %s) "
                "ON CONFLICT (key) DO UPDATE SET payload = EXCLUDED.payload",
                (key, payload, _now_iso()),
            )
        self.conn.commit()

    @_reconnecting
    def reserve_idempotent(self, key: str, payload: str) -> bool:
        """原子占位：`ON CONFLICT DO NOTHING` + rowcount 判断是否占到（防并发 TOCTOU）。"""
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO idempotency (key, payload, created_at) VALUES (%s, %s, %s) "
                "ON CONFLICT (key) DO NOTHING",
                (key, payload, _now_iso()),
            )
            reserved = bool(cur.rowcount == 1)
        self.conn.commit()
        return reserved

    @_reconnecting
    def release_idempotent(self, key: str, payload: str) -> None:
        """仅当内容仍是那条占位时才删（否则会把已缓存的响应快照删掉）。"""
        with self.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM idempotency WHERE key = %s AND payload = %s", (key, payload)
            )
        self.conn.commit()

    @_reconnecting
    def append_event(self, run_id: str, payload: str, keep: int | None = None) -> int:
        # INSERT + 裁剪必须同生共死：autocommit 模式下拆成两条语句时，中间可见的
        # "插入未裁剪"状态会被其它副本读到。用显式事务块包住（与 SQLite 的加锁两步对齐）。
        with self._lock, self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO run_events (run_id, data, created_at) VALUES (%s, %s, %s) "
                "RETURNING id",
                (run_id, payload, _now_iso()),
            )
            row = cur.fetchone()
            seq = int(row[0]) if row is not None else 0
            if keep and keep > 0:
                # 保留策略：只留该 run 最近 keep 条（否则共享事件表会无界增长）
                cur.execute(
                    "DELETE FROM run_events WHERE run_id = %s AND id <= %s",
                    (run_id, seq - keep),
                )
        return seq

    @_reconnecting
    def purge_expired_idempotency(self, before_iso: str) -> int:
        """删掉早于 `before_iso` 的幂等记录（成功快照原先永不删除）。

        比较前把阈值归一化成 UTC-aware ISO：生产方若传 naive datetime，字符串比较会误判。
        """
        with self.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM idempotency WHERE created_at < %s",
                (normalize_utc_iso(before_iso),),
            )
            count = cur.rowcount
        self.conn.commit()
        return int(count or 0)

    @_reconnecting
    def purge_stale_rate_limits(self, before_epoch: float) -> int:
        """删掉窗口确实已结束的限流计数行（行永不自己消失）。

        不变量：只有当 `window_start + window_seconds <= before_epoch` 时才删。
        `before_epoch` 由维护清扫算成 `now - max_age`（≤ now），所以满足该条件的桶，
        其窗口在 `before_epoch`（因而也在 now）之前就已结束，不可能仍在生效。
        窗口上界取本进程见过的最大窗口（`hit_rate_limit` 记录），故是"按配置窗口"比较，
        而非只比固定的 max_age（后者可能小于实际窗口、误删仍活着的桶）。
        """
        with self.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM rate_limits WHERE window_start + %s <= %s",
                (self._rate_window_seconds, before_epoch),
            )
            count = cur.rowcount
        self.conn.commit()
        return int(count or 0)

    @_reconnecting
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

    @_reconnecting
    def hit_rate_limit(
        self, bucket_key: str, window_seconds: int, now: float
    ) -> tuple[int, float]:
        """固定窗口计数 +1。用单条 UPSERT 完成"过期则重置、否则累加"，避免读改写竞态。"""
        # 记下见过的最大窗口，供 purge_stale_rate_limits 判断窗口是否可能仍活着。
        self._rate_window_seconds = max(
            self._rate_window_seconds, float(window_seconds)
        )
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
    @_reconnecting
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

    @_reconnecting
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

    @_reconnecting
    def release_run_lock(self, run_id: str, owner: str) -> None:
        """释放：只删自己的锁（owner 不匹配时不动，防止误删他人的锁）。"""
        with self.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM run_locks WHERE run_id = %s AND owner = %s", (run_id, owner)
            )

    @_reconnecting
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
    @_reconnecting
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

    @_reconnecting
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

    @_reconnecting
    def delete_credential(self, scope: str, name: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM credentials WHERE scope = %s AND name = %s", (scope, name)
            )
        self.conn.commit()

    @_reconnecting
    def list_credential_names(self, scope: str) -> list[str]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT name FROM credentials WHERE scope = %s ORDER BY name", (scope,)
            )
            rows = cur.fetchall()
        return [str(r[0]) for r in rows]

    @_reconnecting
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

    @_reconnecting
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

    @_reconnecting
    def delete_credential_lease(self, scope: str, lease_id: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM credential_leases WHERE scope = %s AND lease_id = %s",
                (scope, lease_id),
            )
        self.conn.commit()

    @_reconnecting
    def purge_expired_credential_leases(self, scope: str, now: datetime) -> int:
        """删掉某作用域下已过期的租约记录，返回删除条数（惰性清理）。"""
        with self.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM credential_leases WHERE scope = %s AND expires_at <= %s",
                (scope, normalize_utc_iso(now)),
            )
            count = cur.rowcount
        self.conn.commit()
        return int(count or 0)
