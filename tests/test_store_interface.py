"""存储可互换性测试：证明 Sqlite 和 Postgres 都满足同一个 RunStore 接口，
会话不关心背后是哪个库 —— 这就是"可换存储"能力的体现。

- RunStore 接口：store/base.py
- 文件版实现：sqlite.py   （零配置，单机）
- 服务器版实现：postgres.py（需 PostgreSQL，测试里用 pytest.mark.skipif 跳过，
  没装/没起数据库时也不会红，CI 保持绿。）
"""
import tempfile
from pathlib import Path

import pytest
from tests.conftest import weather_tool  # noqa: F401  仅示例 RunStore 依赖

from warden_agent.core.run.status import AgentRun, RunStatus
from warden_agent.model.model import Message
from warden_agent.store.base import RunStore
from warden_agent.store.sqlite import SqliteStore


def test_sqlite满足RunStore接口() -> None:
    """SqliteStore 能被当成 RunStore 用（类型兼容）+ 三种数据都能存读。"""
    store: RunStore = SqliteStore(Path(tempfile.mkdtemp()) / "t.db")
    run = AgentRun("r1")
    run.mark_queued()
    store.save_run(run)
    assert store.load_run("r1").status == RunStatus.QUEUED

    store.append_message("r1", Message(role="user", content="你好"))
    assert store.load_messages("r1")[0].content == "你好"

    store.save_pending_approval("r1", "appr-1", "fs.delete", {"path": "/x"}, "需批准")
    assert store.load_pending_approval("r1")[1] == "fs.delete"
    store.clear_pending_approval("r1")
    assert store.load_pending_approval("r1") is None
    store.close()


def _postgres_available() -> bool:
    try:
        import psycopg

        conn = psycopg.connect(host="localhost", dbname="postgres",
                               user="postgres", password="", connect_timeout=2)
        conn.close()
        return True
    except Exception:
        return False


@pytest.mark.skipif(
    not _postgres_available(), reason="需要可用的 PostgreSQL 服务器才会运行"
)
def test_postgres_存储全流程() -> None:
    """真实 PG 集成测试。没有数据库时自动跳过。"""
    _postgres_full_flow()


def _postgres_full_flow() -> None:
    from warden_agent.store.postgres import PostgresStore

    store = PostgresStore(host="localhost", dbname="warden",
                          user="postgres", password="")
    run = AgentRun("pg-1")
    run.mark_queued()
    store.save_run(run)
    assert store.load_run("pg-1").status == RunStatus.QUEUED
    store.close()
