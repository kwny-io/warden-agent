"""SQLite 持久化与恢复测试。"""
import tempfile
from pathlib import Path

from warden_agent.core.run.status import AgentRun, RunStatus
from warden_agent.model.model import Message
from warden_agent.store.recovery import RecoveryAssistant
from warden_agent.store.sqlite import SqliteStore


def test_保存并读回Run状态() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = SqliteStore(Path(tmp) / "test.db")
        run = AgentRun("run-abc")
        run.mark_queued()  # 让它进 QUEUED
        store.save_run(run)
        loaded = store.load_run("run-abc")
        assert loaded is not None
        assert loaded.status == RunStatus.QUEUED
        assert store.load_run("no-such") is None
        store.close()


def test_保存并读回对话历史() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = SqliteStore(Path(tmp) / "test.db")
        store.append_message("run-1", Message(role="user", content="你好"))
        store.append_message("run-1", Message(role="assistant", content="hi"))
        msgs = store.load_messages("run-1")
        assert [m.role for m in msgs] == ["user", "assistant"]
        assert msgs[1].content == "hi"
        store.close()


def test_崩溃后从存档点恢复() -> None:
    """模拟：跑了一轮并存盘 -> "崩溃关闭" -> 打开新的 store -> 接着聊还能看到上下文。"""
    from warden_agent.loop.loop import AgentLoop
    from warden_agent.model.fake import FakeModel
    from warden_agent.tool.catalog import ToolCatalog

    tmp = Path(tempfile.mkdtemp())
    db = tmp / "recover.db"

    # 第一轮：normal 模型 + 空目录，跑一句并存盘
    store = SqliteStore(db)
    loop = AgentLoop(model=FakeModel(), catalog=ToolCatalog())
    first = RecoveryAssistant(store, loop).resume("run-9", "你好")
    assert first
    # 假装程序崩溃、关闭连接
    store.close()

    # 崩溃后重启：用新的连接读同一份数据库
    store2 = SqliteStore(db)
    assert len(store2.load_messages("run-9")) > 0  # 存档还在
    store2.close()
