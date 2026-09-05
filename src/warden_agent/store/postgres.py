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

from warden_agent.core.run.status import AgentRun, RunStatus
from warden_agent.model.model import Message, ToolCall


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
        self.conn.commit()

    # ---- Run 状态 ----
    def save_run(self, run: AgentRun) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO runs (run_id, status) VALUES (%s, %s) "
                "ON CONFLICT (run_id) DO UPDATE SET status = EXCLUDED.status",
                (run.run_id, run.status.name),
            )
        self.conn.commit()

    def load_run(self, run_id: str) -> AgentRun | None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT status FROM runs WHERE run_id = %s", (run_id,))
            row = cur.fetchone()
        if row is None:
            return None
        run = AgentRun(run_id)
        run.status = RunStatus[row[0]]
        return run

    # ---- 对话消息 ----
    def append_message(self, run_id: str, message: Message) -> None:
        tool_json = (
            json.dumps(message.tool_call.to_dict())
            if message.tool_call else None
        )
        with self.conn.cursor() as cur:
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
