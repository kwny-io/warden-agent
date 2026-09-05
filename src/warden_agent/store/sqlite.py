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

import json
import sqlite3
import threading
from pathlib import Path

from warden_agent.core.run.status import AgentRun, RunStatus
from warden_agent.model.model import Message, ToolCall
from warden_agent.store.codec import DEFAULT_CODEC_REGISTRY, VersionedCodecRegistry


class SqliteStore:
    """一个最简单的 SQLite 存档点。存 Run 状态 + 对话历史。

    线程安全说明：FastAPI 的同步接口跑在线程池里，SQLite 连接默认是"线程绑定"的
    （在哪线程创建就只能在哪线程用）。所以这里用 check_same_thread=False 允许跨线程，
    并用一把 Lock 串行化所有写操作，避免并发写冲突——这是 SQLite 在多线程 Web 服务里的
    标准做法。
    """

    def __init__(self, db_path: str | Path) -> None:
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._lock = threading.Lock()
        self._codec: VersionedCodecRegistry = DEFAULT_CODEC_REGISTRY
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
                reason      TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS checkpoints (
                run_id  TEXT PRIMARY KEY,
                data    TEXT NOT NULL       -- 版本化 checkpoint JSON
            );
            """
        )
        self.conn.commit()

    def _init_migrations(self) -> None:
        """运维 schema 版本：记录这套表结构当前是第几版，供未来迁移判断起点。"""
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS __schema_version__ ("
            " version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        cur = self.conn.execute("SELECT version FROM __schema_version__")
        row = cur.fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO __schema_version__ (version, applied_at) VALUES (1, ?)",
                ("now",),
            )
        self.conn.commit()

    def schema_version(self) -> int:
        row = self.conn.execute("SELECT version FROM __schema_version__").fetchone()
        return int(row[0]) if row else 0

    def ping(self) -> None:
        """健康检查探针：执行一句无害查询，确认连接与底层文件可用。

        挂了会抛异常，由健康检查捕获后把该依赖标记为 unreachable。
        """
        self.conn.execute("SELECT 1").fetchone()

    # ---- 保存 ----
    def save_run(self, run: AgentRun) -> None:
        """把 Run 当前状态写进数据库（KEY 覆盖写）。"""
        with self._lock:
            self.conn.execute(
                "INSERT INTO runs (run_id, status) VALUES (?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET status = excluded.status",
                (run.run_id, run.status.name),
            )
            self.conn.commit()

    def append_message(self, run_id: str, message: Message) -> None:
        """往某次 Run 的对话历史里追加一条消息。

        写入走版本化 codec：把 tool_call 编码成 "v<ver>:" + 内容，
        让未来结构变更时可对历史数据分别读取。
        """
        tool_json = None
        if message.tool_call is not None:
            ver, encoded = self._codec.encode(None, message.tool_call.to_dict())
            tool_json = f"v{ver}:{encoded}"
        with self._lock:
            self.conn.execute(
                "INSERT INTO messages (run_id, role, content, tool_call) VALUES (?, ?, ?, ?)",
                (run_id, message.role, message.content, tool_json),
            )
            self.conn.commit()

    # ---- 读取（恢复用）----
    def load_run(self, run_id: str) -> AgentRun | None:
        """读回某个 Run 的状态；不存在返回 None。"""
        row = self.conn.execute("SELECT status FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        run = AgentRun(run_id)
        run.status = RunStatus[row[0]]  # 从名字恢复枚举
        return run

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
                except json.JSONDecodeError:
                    tool_call = None
            out.append(Message(role=role, content=content, tool_call=tool_call))
        return out

    def _decode_tool_call(self, raw: str) -> ToolCall | None:
        """按版本前缀解码 tool_call；无前缀的老数据按 v1 JSON 兜底。"""
        ver: int = 1
        data: str = raw
        if raw.startswith("v") and ":" in raw:
            head, _, body = raw.partition(":")
            if head[1:].isdigit():
                ver = int(head[1:])
                data = body
        obj = self._codec.decode(ver, data)
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
        ver, encoded_args = self._codec.encode(None, arguments)
        with self._lock:
            self.conn.execute(
                "INSERT INTO pending_approvals "
                "(run_id, approval_id, tool_name, arguments, reason) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET "
                "approval_id=excluded.approval_id, tool_name=excluded.tool_name, "
                "arguments=excluded.arguments, reason=excluded.reason",
                (run_id, approval_id, tool_name, f"v{ver}:{encoded_args}", reason),
            )
            self.conn.commit()

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
            ver = 1
            data = raw_args
            if raw_args.startswith("v") and ":" in raw_args:
                head, _, body = raw_args.partition(":")
                if head[1:].isdigit():
                    ver = int(head[1:])
                    data = body
            args = self._codec.decode(ver, data)
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
        ver, encoded = self._codec.encode(None, checkpoint.to_dict())
        with self._lock:
            self.conn.execute(
                "INSERT INTO checkpoints (run_id, data) VALUES (?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET data = excluded.data",
                (checkpoint.run_id, f"v{ver}:{encoded}"),
            )
            self.conn.commit()

    def load_checkpoint(self, run_id: str) -> object | None:
        """读回某个 run 的最新存档点；没有返回 None。"""
        row = self.conn.execute(
            "SELECT data FROM checkpoints WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        return self._decode_checkpoint(row[0])

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
        for run_id, raw in rows:
            cp = self._decode_checkpoint(raw)
            if cp is not None:
                out.append(cp)
        return out

    def _decode_checkpoint(self, raw: str) -> object | None:
        """按版本前缀解码一条 checkpoint；损坏/旧版本兜底返回 None。"""
        from warden_agent.runtime.checkpoint import Checkpoint

        ver = 1
        data = raw
        if raw.startswith("v") and ":" in raw:
            head, _, body = raw.partition(":")
            if head[1:].isdigit():
                ver = int(head[1:])
                data = body
        try:
            obj = self._codec.decode(ver, data)
        except (json.JSONDecodeError, KeyError):
            return None
        if isinstance(obj, dict):
            return Checkpoint.from_dict(obj)
        return None

    def close(self) -> None:
        self.conn.close()
