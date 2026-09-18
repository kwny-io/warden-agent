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
        """删除整个会话：对话、待审批、状态一并清掉（与 SqliteStore 语义一致）。"""
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM messages WHERE run_id = %s", (run_id,))
            cur.execute("DELETE FROM pending_approvals WHERE run_id = %s", (run_id,))
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

    def list_approval_history(self, limit: int = 20) -> list[dict[str, Any]]:
        """审批决策历史（最新的在前）。"""
        with self.conn.cursor() as cur:
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

    def close(self) -> None:
        self.conn.close()
