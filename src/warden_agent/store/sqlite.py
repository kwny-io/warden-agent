"""SQLite 持久化与恢复：把 Agent 干活的进度存进数据库，坏了/关机了能接着干。

  - SQLite 是"唯一事实源"：所有该记下来的东西都写进数据库文件。
  - 我们只保存最基本的两种东西：
      1. Run 的状态（进行到哪一步了）
      2. 对话历史（每句话、每次工具调用、每次工具结果）
  只要这两样都在，程序崩溃后重来，把状态和对话读回来，AgentLoop 就能从上次的地方继续。

白话解释：
就像游戏存档。你打游戏中途关机了，下次打开读档，从存档点继续。
这里每次"模型说了一句话 / 调了一次工具 / 拿到结果"，我们都认为值得**存一档**，
写进 SQLite 文件（一个 .db 文件，就在项目目录里）。

注意：这个 Python 版是"学习版"，只做最朴素的保存和读取，帮助理解原理。
真正生产级会做事务、并发、恢复一致性校验等，这里一律略过，但思路是一致的。
"""

from __future__ import annotations

import functools
import json
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
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
    DEFAULT_CODEC_REGISTRY,
    VersionedCodecRegistry,
    decode_versioned,
    encode_versioned,
    normalize_utc_iso,
)


def _locked[**P, R](
    method: Callable[Concatenate[SqliteStore, P], R],
) -> Callable[Concatenate[SqliteStore, P], R]:
    """把方法体放进**存储自己的锁**里执行。

    为什么读也要加锁（这是修过的一个真 bug）：连接是 `check_same_thread=False` **跨线程共享**的，
    而 SQLite 的一条连接**不允许并发使用**——两个线程同时用它（哪怕一个读一个写）会抛
    `sqlite3.InterfaceError: not an error`，在 HTTP 层表现为**偶发 500**（压测并发 8~12 时实测到）。
    早先只有写方法 `with self._lock`，读方法没加，于是"读+写并发"这条最常见的组合会翻车。
    改法用装饰器而不是给每个方法体重排缩进：diff 小、不会误伤相邻代码。

    ⚠️ 锁用 **RLock**：某个读方法将来若调用另一个已加锁的方法，不会自锁死。
    泛型参数（PEP 695）必须保留签名，否则 mypy 会把被装饰方法的返回值退化成 Any、
    连累所有调用点（本文件里那种错一次报了 13 条）。
    """
    @functools.wraps(method)
    def wrapper(self: SqliteStore, *args: P.args, **kwargs: P.kwargs) -> R:
        with self._lock:
            return method(self, *args, **kwargs)
    # functools.wraps 的静态返回类型是 `_Wrapped[...]`，与声明的 Callable 形式不完全等价；
    # 它保留的正是原签名，所以这里显式 cast 一次即可。
    return cast("Callable[Concatenate[SqliteStore, P], R]", wrapper)


def _now_iso() -> str:
    """当前 UTC 时间的 ISO 字符串（秒级），存库和展示都用它。"""
    return datetime.now(UTC).isoformat(timespec="seconds")


class SqliteStore:
    """一个最简单的 SQLite 存档点。存 Run 状态 + 对话历史。

    `backend` 是给装配层看的后端标识：`run_server` 据此决定审计/记忆该用 SQLite 还是
    跟着换到 PG（避免"存储换了、审计记忆还各自留在 SQLite"这种静默不一致）。

    线程安全说明：FastAPI 的同步接口跑在线程池里，SQLite 连接默认是"线程绑定"的
    （在哪线程创建就只能在哪线程用）。所以这里用 check_same_thread=False 允许跨线程，
    并用一把 Lock 串行化所有写操作，避免并发写冲突——这是 SQLite 在多线程 Web 服务里的
    标准做法。
    """

    backend = "sqlite"

    def __init__(self, db_path: str | Path) -> None:
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._lock = threading.RLock()
        self._codec: VersionedCodecRegistry = DEFAULT_CODEC_REGISTRY
        # 本进程见过的最大限流窗口（hit_rate_limit 记录）。
        # 用于让 purge_stale_rate_limits 判断"窗口是否还可能活着"——光靠固定的
        # max_age 不够，因为 max_age 可能小于实际窗口。见该方法的不变量说明。
        self._rate_window_seconds = 0.0
        self._init_schema()
        self._init_migrations()

    @property
    def codec(self) -> VersionedCodecRegistry:
        return self._codec

    def _init_schema(self) -> None:
        """建三张表：run 状态 + 对话 + 待审批。阶段2 增加 pending_approvals 表，
        让"等待审批"这种中间态也能完整恢复（不只是恢复对话和状态，还恢复卡在那里的那一步）。"""
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id   TEXT PRIMARY KEY,
                status   TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id    TEXT NOT NULL,
                role      TEXT NOT NULL,
                content   TEXT NOT NULL,
                tool_call TEXT            -- 如果这条是"AI想调工具"，这里存工具调用JSON
            );
            CREATE TABLE IF NOT EXISTS pending_approvals (
                run_id      TEXT PRIMARY KEY,
                approval_id TEXT NOT NULL,
                tool_name   TEXT NOT NULL,
                arguments   TEXT NOT NULL,   -- JSON
                reason      TEXT NOT NULL,
                -- 进入"等待审批"的时刻：告警要算"等了多久"。用 run 的最后活动时间
                -- 近似会被后续操作刷新、从而**低估**等待时长，所以单独记一个。
                created_at  TEXT
            );
            CREATE TABLE IF NOT EXISTS checkpoints (
                run_id  TEXT PRIMARY KEY,
                data    TEXT NOT NULL       -- 版本化 checkpoint JSON
            );
            CREATE TABLE IF NOT EXISTS approval_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id      TEXT NOT NULL,
                approval_id TEXT NOT NULL,
                tool_name   TEXT NOT NULL,
                arguments   TEXT,
                decision    TEXT NOT NULL,     -- approved / rejected
                created_at  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS users (
                user_id    TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            );
            -- 幂等表：多副本共享同一张表，同一 Idempotency-Key 才不会各缓存一份
            CREATE TABLE IF NOT EXISTS idempotency (
                key        TEXT PRIMARY KEY,
                payload    TEXT NOT NULL,   -- JSON（body 以 base64 存放）
                created_at TEXT NOT NULL
            );
            -- 保留清扫按 created_at 扫描：没索引会随幂等记录量线性变慢
            CREATE INDEX IF NOT EXISTS idx_idempotency_created ON idempotency (created_at);
            -- 事件流：SSE 事件落库，多副本订阅同一张表（轮询增量）
            CREATE TABLE IF NOT EXISTS run_events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id     TEXT NOT NULL,
                data       TEXT NOT NULL,   -- 事件 JSON
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_run_events_run ON run_events (run_id, id);
            -- 限流计数：固定窗口，多副本共享 → 全局限额不翻倍
            CREATE TABLE IF NOT EXISTS rate_limits (
                bucket_key   TEXT PRIMARY KEY,
                window_start REAL NOT NULL,
                count        INTEGER NOT NULL
            );
            -- 保留清扫按 window_start 扫描：没索引会随限流桶数量线性变慢
            CREATE INDEX IF NOT EXISTS idx_rate_limits_window ON rate_limits (window_start);
            -- 凭证密文：AES-GCM 密文（外加随机 nonce），明文绝不落这张表。
            -- 没有它，"加密"只发生在内存里，进程一退凭证就没了。
            CREATE TABLE IF NOT EXISTS credentials (
                scope      TEXT NOT NULL,   -- 作用域：部署级 / 租户级 / 用户级
                name       TEXT NOT NULL,
                data       TEXT NOT NULL,   -- JSON: 字段名 -> 密文
                updated_at TEXT NOT NULL,
                PRIMARY KEY (scope, name)
            );
            -- 凭证租约：只存元数据（无明文）。取租约时按 name 回查密文现解，
            -- 所以"租约跨重启存活"与"明文不落盘"可以并存。
            CREATE TABLE IF NOT EXISTS credential_leases (
                scope      TEXT NOT NULL,
                lease_id   TEXT NOT NULL,
                name       TEXT NOT NULL,
                issued_at  TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                PRIMARY KEY (scope, lease_id)
            );
            CREATE INDEX IF NOT EXISTS idx_credential_leases_expiry
                ON credential_leases (scope, expires_at);
            -- Run 级锁（租约式）：多副本下防止同一个 run 被两个副本同时驱动。
            -- 带 expires_at 是刻意的：持有者崩了不需要人工解锁，租约到期即可被接管。
            -- 只有针对 run_id 的原子 UPSERT 才算数（见 acquire_run_lock）。
            CREATE TABLE IF NOT EXISTS run_locks (
                run_id      TEXT PRIMARY KEY,
                owner       TEXT NOT NULL,
                acquired_at REAL NOT NULL,
                expires_at  REAL NOT NULL
            );
            """
        )
        self.conn.commit()

    # 目标 schema 版本：每次加表/加列就 +1。老库补完列后版本被更新到它。
    _SCHEMA_VERSION = 4

    def _has_column(self, table: str, column: str) -> bool:
        """该表是否已有某列（用 `pragma_table_info` 表值函数 + 参数化查询，不拼 SQL）。"""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM pragma_table_info(?) WHERE name = ?", (table, column)
        ).fetchone()
        return bool(row and row[0])

    def _init_migrations(self) -> None:
        """schema 迁移：按 `pragma_table_info` 判定**是否缺列**，缺了才加。

        为什么不再用 `try: ALTER ... except OperationalError: pass`（这是修过的一个真问题）：
        那种写法把"列已存在"和**真失败**（磁盘满、库被锁、权限不足）混为一谈——
        真失败会被静默吞掉，于是老库"以为升级了、其实没加列"，之后所有写入按新 schema
        走就会静默出错。改成显式判定后，真失败会**抛出**，启动即暴露。
        """
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS __schema_version__ ("
            " version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        self.conn.commit()

        # v2：runs 加 updated_at（会话列表展示"最后活跃时间"）
        if not self._has_column("runs", "updated_at"):
            self.conn.execute("ALTER TABLE runs ADD COLUMN updated_at TEXT")
            self.conn.commit()
        # v3：runs 加 user_id（多用户隔离）；无归属的历史会话归到 demo-user
        if not self._has_column("runs", "user_id"):
            self.conn.execute("ALTER TABLE runs ADD COLUMN user_id TEXT NOT NULL DEFAULT ''")
            self.conn.execute("UPDATE runs SET user_id = 'demo-user' WHERE user_id = ''")
            self.conn.commit()
        # v4：pending_approvals 加 created_at（挂起超时告警算准确时长；历史行为 NULL 时退回近似）
        if not self._has_column("pending_approvals", "created_at"):
            self.conn.execute("ALTER TABLE pending_approvals ADD COLUMN created_at TEXT")
            self.conn.commit()

        # 记录当前版本（单行表：先清再写，避免多行）
        self.conn.execute("DELETE FROM __schema_version__")
        self.conn.execute(
            "INSERT INTO __schema_version__ (version, applied_at) VALUES (?, ?)",
            (self._SCHEMA_VERSION, _now_iso()),
        )
        self.conn.commit()

    @_locked
    def schema_version(self) -> int:
        row = self.conn.execute("SELECT version FROM __schema_version__").fetchone()
        return int(row[0]) if row else 0

    @_locked
    def ping(self) -> None:
        """健康检查探针：执行一句无害查询，确认连接与底层文件可用。

        挂了会抛异常，由健康检查捕获后把该依赖标记为 unreachable。
        """
        self.conn.execute("SELECT 1").fetchone()

    # ---- 保存 ----
    def save_run(self, run: AgentRun) -> None:
        """把 Run 当前状态写进数据库（KEY 覆盖写），同时刷新最后活跃时间。"""
        with self._lock:
            self.conn.execute(
                "INSERT INTO runs (run_id, status, user_id, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET "
                "status = excluded.status, updated_at = excluded.updated_at",
                (run.run_id, run.status.name, run.user_id, _now_iso()),
            )
            self.conn.commit()

    def append_message(self, run_id: str, message: Message) -> None:
        """往某次 Run 的对话历史里追加一条消息。

        写入走版本化 codec：把 tool_call 编码成 "v<ver>:" + 内容，
        让未来结构变更时可对历史数据分别读取。

        查重：入库前先按会话（run_id）+ 角色 + 内容 + 工具调用查一遍，
        完全相同的消息不重复落库（用户重发同一句话 / 流重复结束时历史不会翻倍）。
        """
        tool_json = None
        if message.tool_call is not None:
            # 与 PostgreSQL 共用同一编码助手，保证跨后端落盘格式一致
            tool_json = encode_versioned(message.tool_call.to_dict(), self._codec)
        with self._lock:
            dup = self.conn.execute(
                "SELECT 1 FROM messages "
                "WHERE run_id = ? AND role = ? AND content = ? "
                "AND COALESCE(tool_call, '') = COALESCE(?, '') LIMIT 1",
                (run_id, message.role, message.content, tool_json),
            ).fetchone()
            if dup is not None:
                return  # 已有完全相同的一条，跳过
            self.conn.execute(
                "INSERT INTO messages (run_id, role, content, tool_call) VALUES (?, ?, ?, ?)",
                (run_id, message.role, message.content, tool_json),
            )
            self.conn.commit()

    # ---- 读取（恢复用）----
    @_locked
    def load_run(self, run_id: str) -> AgentRun | None:
        """读回某个 Run 的状态；不存在返回 None。"""
        row = self.conn.execute(
            "SELECT status, user_id FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        run = AgentRun(run_id, user_id=row[1] or "")
        run.status = RunStatus[row[0]]  # 从名字恢复枚举
        return run

    @_locked
    def load_messages(self, run_id: str) -> list[Message]:
        """读回某个 Run 的完整对话历史，顺序和存的时候一样。"""
        rows = self.conn.execute(
            "SELECT role, content, tool_call FROM messages WHERE run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
        out: list[Message] = []
        for role, content, tool_json in rows:
            tool_call = None
            if tool_json:
                try:
                    tool_call = self._decode_tool_call(tool_json)
                except (json.JSONDecodeError, KeyError):
                    # KeyError：未知版本（如未来写下的 v2）。单行坏数据不能拖垮整次加载。
                    tool_call = None
            out.append(Message(role=role, content=content, tool_call=tool_call))
        return out

    def delete_run(self, run_id: str) -> None:
        """删除整个会话：对话、待审批、checkpoint、事件、状态一并清掉。"""
        with self._lock:
            self.conn.execute("DELETE FROM messages WHERE run_id = ?", (run_id,))
            self.conn.execute("DELETE FROM pending_approvals WHERE run_id = ?", (run_id,))
            self.conn.execute("DELETE FROM checkpoints WHERE run_id = ?", (run_id,))
            self.conn.execute("DELETE FROM run_events WHERE run_id = ?", (run_id,))
            self.conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
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
        with self._lock:
            self.conn.execute(
                "INSERT INTO approval_history "
                "(run_id, approval_id, tool_name, arguments, decision, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, approval_id, tool_name,
                 json.dumps(arguments, ensure_ascii=False), decision, _now_iso()),
            )
            self.conn.commit()

    @_locked
    def list_approval_history(
        self, limit: int = 20, owner: str | None = None
    ) -> list[dict[str, Any]]:
        """审批决策历史（最新的在前）。

        owner 给定时只返回该用户名下 Run 的决策——审批历史记着"谁批准了哪个高危工具、
        带什么参数"，属于租户数据，不能跨租户可见。
        """
        if owner:
            rows = self.conn.execute(
                "SELECT h.run_id, h.approval_id, h.tool_name, h.decision, h.created_at "
                "FROM approval_history h JOIN runs r ON r.run_id = h.run_id "
                "WHERE r.user_id = ? ORDER BY h.id DESC LIMIT ?",
                (owner, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT run_id, approval_id, tool_name, decision, created_at "
                "FROM approval_history ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {"run_id": r[0], "approval_id": r[1], "tool_name": r[2],
             "decision": r[3], "created_at": r[4]}
            for r in rows
        ]

    @_locked
    def list_runs(self, limit: int = 50, owner: str | None = None) -> list[dict[str, Any]]:
        """列出会话概要（前端会话列表用）：按最近活跃排序。

        title 取首条用户消息（没有消息的 run 回退用 run_id），msg_count 是对话条数。
        owner 给定时的过滤**下推到 SQL 的 WHERE**（而不是取完 LIMIT n 条再在应用层过滤）——
        后者在混合归属下会"先截断再筛"，返回条数少于 n，即使该 owner 名下还有更多匹配。
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
            sql += " WHERE r.user_id = ?"
            params.append(owner)
        sql += " ORDER BY last_id IS NULL, last_id DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        out = [
            {
                "run_id": r[0],
                "status": r[1],
                "msg_count": r[2],
                "title": (r[3] or r[0])[:60],
                "updated_at": r[5],
                "user_id": r[6],
            }
            for r in rows
        ]
        return out

    # ---- 用户（中控台账号）----
    def create_user(self, user_id: str) -> None:
        """登记一个中控台用户（幂等：已存在则不动）。"""
        with self._lock:
            self.conn.execute(
                "INSERT INTO users (user_id, created_at) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO NOTHING",
                (user_id, _now_iso()),
            )
            self.conn.commit()

    @_locked
    def list_users(self) -> list[dict[str, Any]]:
        """已登记的用户列表（按创建时间）。"""
        rows = self.conn.execute(
            "SELECT user_id, created_at FROM users ORDER BY created_at"
        ).fetchall()
        return [{"user_id": r[0], "created_at": r[1]} for r in rows]

    def _decode_tool_call(self, raw: str) -> ToolCall | None:
        """按版本前缀解码 tool_call；无前缀的老数据按 v1 JSON 兜底。"""
        obj = decode_versioned(raw, self._codec)
        if isinstance(obj, dict):
            return ToolCall.from_dict(obj)
        return None

    # ---- 待审批持久化（阶段2：让"等待审批"中间态可恢复）----
    def save_pending_approval(
        self,
        run_id: str,
        approval_id: str,
        tool_name: str,
        arguments: dict[str, object],
        reason: str,
    ) -> None:
        """把"卡在等待审批的那一步"存下来。arguments 走版本化 codec。"""
        encoded_args = encode_versioned(arguments, self._codec)
        with self._lock:
            self.conn.execute(
                "INSERT INTO pending_approvals "
                "(run_id, approval_id, tool_name, arguments, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET "
                "approval_id=excluded.approval_id, tool_name=excluded.tool_name, "
                "arguments=excluded.arguments, reason=excluded.reason, "
                "created_at=excluded.created_at",
                (run_id, approval_id, tool_name, encoded_args, reason, _now_iso()),
            )
            self.conn.commit()

    @_locked
    def pending_approval_created_at(self, run_id: str) -> str | None:
        """该 run 进入"等待审批"的时刻（ISO 字符串）；没有待审批或老数据没记则返回 None。

        给"挂起超时告警"用：比拿 run 的最后活动时间来近似更准——后者会被等待期间的
        任何一次操作刷新，从而**低估**等待时长。
        """
        row = self.conn.execute(
            "SELECT created_at FROM pending_approvals WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return str(row[0])

    @_locked
    def load_pending_approval(self, run_id: str) -> tuple[str, str, dict[str, object], str] | None:
        """读回某 run 待审批的一步；没有返回 None。"""
        row = self.conn.execute(
            "SELECT approval_id, tool_name, arguments, reason "
            "FROM pending_approvals WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        raw_args = row[2]
        args: object = {}
        try:
            args = decode_versioned(raw_args, self._codec)
        except (json.JSONDecodeError, IndexError, KeyError):
            args = {}
        if not isinstance(args, dict):
            args = {}
        return row[0], row[1], args, row[3]

    def clear_pending_approval(self, run_id: str) -> None:
        """批准/拒绝后清除待审批记录。"""
        with self._lock:
            self.conn.execute("DELETE FROM pending_approvals WHERE run_id = ?", (run_id,))
            self.conn.commit()

    # ---- Checkpoint（存档点）持久化 ----
    def save_checkpoint(self, checkpoint: object) -> None:
        """把一个 Checkpoint 落库。内容走版本化 codec。"""
        from warden_agent.runtime.checkpoint import Checkpoint

        assert isinstance(checkpoint, Checkpoint)
        encoded = encode_versioned(checkpoint.to_dict(), self._codec)
        with self._lock:
            self.conn.execute(
                "INSERT INTO checkpoints (run_id, data) VALUES (?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET data = excluded.data",
                (checkpoint.run_id, encoded),
            )
            self.conn.commit()

    @_locked
    def load_checkpoint(self, run_id: str) -> object | None:
        """读回某个 run 的最新存档点；没有返回 None。"""
        row = self.conn.execute(
            "SELECT data FROM checkpoints WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        return self._decode_checkpoint(row[0])

    @_locked
    def list_checkpoints(self) -> list[object]:
        """枚举所有 run 的存档点（跨 run 协调恢复用）。

        恢复控制器需要"看到全部 run 各自存到哪了"，才能决定哪些该续、
        哪些已完成、哪些该重试。这里一次性把整张 checkpoints 表读出
        并按统一逻辑解码。
        """
        rows = self.conn.execute(
            "SELECT run_id, data FROM checkpoints ORDER BY run_id"
        ).fetchall()
        out: list[object] = []
        for _run_id, raw in rows:
            cp = self._decode_checkpoint(raw)
            if cp is not None:
                out.append(cp)
        return out

    def _decode_checkpoint(self, raw: str) -> object | None:
        """按版本前缀解码一条 checkpoint；损坏/旧版本兜底返回 None。"""
        from warden_agent.runtime.checkpoint import Checkpoint

        try:
            obj = decode_versioned(raw, self._codec)
        except (json.JSONDecodeError, KeyError):
            return None
        if isinstance(obj, dict):
            return Checkpoint.from_dict(obj)
        return None

    # ---- 跨副本共享状态（幂等 / 事件流 / 限流计数）----
    @_locked
    def get_idempotent(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT payload FROM idempotency WHERE key = ?", (key,)
        ).fetchone()
        return str(row[0]) if row is not None else None

    def save_idempotent(self, key: str, payload: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO idempotency (key, payload, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET payload = excluded.payload",
                (key, payload, _now_iso()),
            )
            self.conn.commit()

    def reserve_idempotent(self, key: str, payload: str) -> bool:
        """原子占位：`ON CONFLICT DO NOTHING` + rowcount 判断是否占到（防并发 TOCTOU）。"""
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO idempotency (key, payload, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO NOTHING",
                (key, payload, _now_iso()),
            )
            self.conn.commit()
            return cur.rowcount == 1

    def release_idempotent(self, key: str, payload: str) -> None:
        """仅当内容仍是那条占位时才删（否则会把已缓存的响应快照删掉）。"""
        with self._lock:
            self.conn.execute(
                "DELETE FROM idempotency WHERE key = ? AND payload = ?", (key, payload)
            )
            self.conn.commit()

    def append_event(self, run_id: str, payload: str, keep: int | None = None) -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO run_events (run_id, data, created_at) VALUES (?, ?, ?)",
                (run_id, payload, _now_iso()),
            )
            self.conn.commit()
            seq = int(cur.lastrowid or 0)
            if keep and keep > 0:
                # 保留策略：只留该 run 最近 keep 条（否则共享事件表会无界增长）
                self.conn.execute(
                    "DELETE FROM run_events WHERE run_id = ? AND id <= ?",
                    (run_id, seq - keep),
                )
                self.conn.commit()
            return seq

    def purge_expired_idempotency(self, before_iso: str) -> int:
        """删掉早于 `before_iso` 的幂等记录（成功快照原先永不删除）。

        比较前把阈值归一化成 UTC-aware ISO：生产方若传 naive datetime，字符串比较会误判。
        """
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM idempotency WHERE created_at < ?",
                (normalize_utc_iso(before_iso),),
            )
            self.conn.commit()
            return int(cur.rowcount or 0)

    def purge_stale_rate_limits(self, before_epoch: float) -> int:
        """删掉窗口确实已结束的限流计数行（行永不自己消失）。

        不变量：只有当 `window_start + window_seconds <= before_epoch` 时才删。
        `before_epoch` 由维护清扫算成 `now - max_age`（≤ now），所以满足该条件的桶，
        其窗口在 `before_epoch`（因而也在 now）之前就已结束，不可能仍在生效。

        为什么不能只比 `window_start < before_epoch`：max_age 是固定值，可能小于实际窗口，
        那样一个"刚开始不久、窗口还活着"的桶会被误删。这里用本进程见过的最大窗口
        （`hit_rate_limit` 记录，见 `_rate_window_seconds`）作为窗口上界，故是"按配置窗口"比较。
        """
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM rate_limits WHERE window_start + ? <= ?",
                (self._rate_window_seconds, before_epoch),
            )
            self.conn.commit()
            return int(cur.rowcount or 0)

    @_locked
    def list_events_after(
        self, run_id: str, after_seq: int, limit: int = 200
    ) -> list[tuple[int, str]]:
        rows = self.conn.execute(
            "SELECT id, data FROM run_events WHERE run_id = ? AND id > ? "
            "ORDER BY id LIMIT ?",
            (run_id, after_seq, limit),
        ).fetchall()
        return [(int(r[0]), str(r[1])) for r in rows]

    def hit_rate_limit(
        self, bucket_key: str, window_seconds: int, now: float
    ) -> tuple[int, float]:
        """固定窗口计数 +1，返回 (本窗口累计次数, 窗口起始时刻)。

        读-改-写在同一把锁内完成，保证多线程下计数不丢。
        """
        # 记下见过的最大窗口，供 purge_stale_rate_limits 判断窗口是否可能仍活着。
        self._rate_window_seconds = max(
            self._rate_window_seconds, float(window_seconds)
        )
        with self._lock:
            row = self.conn.execute(
                "SELECT window_start, count FROM rate_limits WHERE bucket_key = ?",
                (bucket_key,),
            ).fetchone()
            if row is None or now - float(row[0]) >= window_seconds:
                start, count = now, 1
            else:
                start, count = float(row[0]), int(row[1]) + 1
            self.conn.execute(
                "INSERT INTO rate_limits (bucket_key, window_start, count) VALUES (?, ?, ?) "
                "ON CONFLICT(bucket_key) DO UPDATE SET "
                "window_start = excluded.window_start, count = excluded.count",
                (bucket_key, start, count),
            )
            self.conn.commit()
        return count, start

    # ---- 凭证保管库（CredentialVault 协议，见 credential/vault.py）----
    #
    # 结构化满足协议：本类不需要 import 协议的实现，只要方法名/签名一致即可被当作
    # vault 使用。这里存的一律是**密文**（加密在 credential 层完成），存储层不碰明文。
    def save_credential(self, credential: StoredCredential) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO credentials (scope, name, data, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(scope, name) DO UPDATE SET "
                "data = excluded.data, updated_at = excluded.updated_at",
                (
                    credential.scope,
                    credential.name,
                    encode_fields(credential.encrypted),
                    _now_iso(),
                ),
            )
            self.conn.commit()

    @_locked
    def load_credential(self, scope: str, name: str) -> StoredCredential | None:
        row = self.conn.execute(
            "SELECT data FROM credentials WHERE scope = ? AND name = ?", (scope, name)
        ).fetchone()
        if row is None:
            return None
        return StoredCredential(scope=scope, name=name, encrypted=decode_fields(row[0]))

    def delete_credential(self, scope: str, name: str) -> None:
        with self._lock:
            self.conn.execute(
                "DELETE FROM credentials WHERE scope = ? AND name = ?", (scope, name)
            )
            self.conn.commit()

    @_locked
    def list_credential_names(self, scope: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT name FROM credentials WHERE scope = ? ORDER BY name", (scope,)
        ).fetchall()
        return [str(r[0]) for r in rows]

    def save_credential_lease(self, lease: StoredLease) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO credential_leases "
                "(scope, lease_id, name, issued_at, expires_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(scope, lease_id) DO UPDATE SET "
                "name = excluded.name, issued_at = excluded.issued_at, "
                "expires_at = excluded.expires_at",
                (
                    lease.scope,
                    lease.lease_id,
                    lease.name,
                    lease.issued_at.isoformat(),
                    lease.expires_at.isoformat(),
                ),
            )
            self.conn.commit()

    @_locked
    def load_credential_lease(self, scope: str, lease_id: str) -> StoredLease | None:
        row = self.conn.execute(
            "SELECT name, issued_at, expires_at FROM credential_leases "
            "WHERE scope = ? AND lease_id = ?",
            (scope, lease_id),
        ).fetchone()
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
        with self._lock:
            self.conn.execute(
                "DELETE FROM credential_leases WHERE scope = ? AND lease_id = ?",
                (scope, lease_id),
            )
            self.conn.commit()

    def purge_expired_credential_leases(self, scope: str, now: datetime) -> int:
        """删掉某作用域下已过期的租约记录，返回删除条数（惰性清理，防止表只增不减）。"""
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM credential_leases WHERE scope = ? AND expires_at <= ?",
                (scope, normalize_utc_iso(now)),
            )
            self.conn.commit()
            return int(cur.rowcount or 0)

    # ---- Run 级锁（租约式；RunLockStore 协议，见 runtime/locking.py）----
    #
    # 取锁是**单条原子语句**：`ON CONFLICT ... DO UPDATE ... WHERE expires_at <= now`
    # —— 键已存在且租约未过期时不更新（等于抢不到）；已过期则被接管。这条语句本身是原子的，
    # 所以多进程/多副本并发抢同一把锁时，只有一方能拿到（再读回来核对 owner 即可确认）。
    def acquire_run_lock(
        self, run_id: str, owner: str, expires_at: float, now: float
    ) -> bool:
        with self._lock:
            self.conn.execute(
                "INSERT INTO run_locks (run_id, owner, acquired_at, expires_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET "
                "  owner = excluded.owner, "
                "  acquired_at = excluded.acquired_at, "
                "  expires_at = excluded.expires_at "
                "WHERE run_locks.expires_at <= ?",
                (run_id, owner, now, expires_at, now),
            )
            row = self.conn.execute(
                "SELECT owner, expires_at FROM run_locks WHERE run_id = ?", (run_id,)
            ).fetchone()
            self.conn.commit()
        if row is None:
            return False
        return str(row[0]) == owner and float(row[1]) == expires_at

    def renew_run_lock(
        self, run_id: str, owner: str, expires_at: float, now: float
    ) -> bool:
        """续租：只有**当前持有者且未过期**才能续（避免续到别人的锁上）。"""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE run_locks SET expires_at = ? "
                "WHERE run_id = ? AND owner = ? AND expires_at > ?",
                (expires_at, run_id, owner, now),
            )
            self.conn.commit()
            return int(cur.rowcount or 0) > 0

    def release_run_lock(self, run_id: str, owner: str) -> None:
        """释放：只删自己的锁（owner 不匹配时不动，防止误删他人的锁）。"""
        with self._lock:
            self.conn.execute(
                "DELETE FROM run_locks WHERE run_id = ? AND owner = ?", (run_id, owner)
            )
            self.conn.commit()

    def run_lock_owner(self, run_id: str, now: float) -> str | None:
        """当前持有者（已过期视为无人持有，并顺手清掉那行）。"""
        with self._lock:
            row = self.conn.execute(
                "SELECT owner, expires_at FROM run_locks WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return None
            if float(row[1]) <= now:
                self.conn.execute("DELETE FROM run_locks WHERE run_id = ?", (run_id,))
                self.conn.commit()
                return None
            return str(row[0])

    def close(self) -> None:
        self.conn.close()
