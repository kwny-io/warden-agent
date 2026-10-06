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
    """PG 是否可用。**必须走 test_postgres_integration 里那一套判据**，别自己连一次。

    自己连的代价（第六十五批实测，两个方向都错）：
      1. `WARDEN_TEST_PG_PORT` 这类旋钮对它无效——切「无 PG 口径」时它会**继续连真库**
         （把端口旋钮指向空端口后，它照样真跑）。口径就不纯了。
      2. PG 不在默认端口时（远端库 / CI 换端口）它连不上 ⇒ **静默跳过**，而整套仍然
         报「0 failed」。这正是第五十二批修掉的形状，当时漏了这个文件。

    静态守卫（防再有人手写一次）：`tests/test_skipped_tests.py::test_所有PG门控都走同一个可用性判据`。
    """
    from tests.test_postgres_integration import _pg_available

    return _pg_available()


@pytest.mark.skipif(
    not _postgres_available(), reason="需要可用的 PostgreSQL 服务器才会运行"
)
def test_postgres_存储全流程() -> None:
    """真实 PG 集成测试。没有数据库时自动跳过。

    断言写在 `_postgres_full_flow()` 里（存完读回来必须真是 QUEUED 状态）。
    形如"测试函数体内无 assert"的静态扫描会把这一条当成无断言测试——
    这里是误报，在此注明以免下次又去"修"它。
    """
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
