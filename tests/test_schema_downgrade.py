"""降级（老代码 + 新库）必须响亮失败，且"静默丢数据"必须留下计数。

这条判据来自一次审计里最像真实事故的一条：`kubectl rollout undo` / `scripts/rollback.sh` 之后，
旧版进程健康检查全绿，但

  1. `_init_migrations` 结尾**无条件** `DELETE` + `INSERT` 自己的版本号（`store/sqlite.py`）
     ⇒ 库里"这是一套更新过的 schema"这个**唯一证据被旧版擦掉了**，事后没人知道库是新的；
  2. 遇到不认识的 payload 版本（如新版写下的 `v2:`），读路径 `except KeyError` 之后把整条
     **静默丢掉**（`tool_call` 变 `None`）——日志没有、指标没有、面板全绿。

于是"数据损失"要几小时后才被人发现，而且发现时已经无法自证发生过。这里钉三件事：
库比代码新时启动就抛；抛之前**不许改版本号**；丢弃 payload 时必须计数。
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from warden_agent.core.metrics import metrics
from warden_agent.model.model import Message, ToolCall
from warden_agent.store.base import SchemaTooNewError
from warden_agent.store.sqlite import SqliteStore

DROP_METRIC = "warden_store_payload_dropped_total"


def _db() -> Path:
    return Path(tempfile.mkdtemp()) / "t.db"


def _drop_count() -> float:
    """指标里 payload 丢弃的总数（0 表示还没出现过）。"""
    total = 0.0
    for line in metrics().render().splitlines():
        if line.startswith(DROP_METRIC):
            total += float(line.rsplit(" ", 1)[1])
    return total


def test_库版本比代码新时启动必须响亮失败() -> None:
    """唯一会发生的情形：降级/回滚，或者新库给了老代码。两种都不该让它继续写。"""
    db = _db()
    SqliteStore(db).close()

    # 模拟"这套库是更新版本写的（v99）"
    with sqlite3.connect(db) as c:
        c.execute("DELETE FROM __schema_version__")
        c.execute(
            "INSERT INTO __schema_version__ (version, applied_at) "
            "VALUES (99, '2026-10-17T00:00:00Z')"
        )

    with pytest.raises(SchemaTooNewError) as ei:
        SqliteStore(db)
    msg = str(ei.value)
    assert "99" in msg, "报错必须说出库里的版本号，否则值班人还得自己去查"
    assert "回滚" in msg or "降级" in msg, "报错必须说清这是回滚/降级场景及其后果"

    # 关键：失败是"响亮"，不是"顺手把证据改掉"
    with sqlite3.connect(db) as c:
        got = c.execute("SELECT version FROM __schema_version__").fetchone()
    assert got is not None and int(got[0]) == 99, "拒绝启动之前不许把版本号改写成代码版本"


def test_同版本与全新库照常启动() -> None:
    """反向对照：加了守卫不许把正常启动弄死（否则天天炸）。"""
    db = _db()
    first = SqliteStore(db)
    assert first.schema_version() == SqliteStore._SCHEMA_VERSION
    first.close()

    again = SqliteStore(db)  # 第二次打开 = 同版本
    assert again.schema_version() == SqliteStore._SCHEMA_VERSION
    again.close()


def test_未知payload版本被丢弃时要留下计数() -> None:
    db = _db()
    store = SqliteStore(db)
    store.append_message(
        "run-1",
        Message(
            role="assistant",
            content="",
            tool_call=ToolCall(id="c1", name="weather.get", arguments={}),
        ),
    )
    store.close()

    # 把它改成"未来版本写下的 payload"（当前代码不认识）
    with sqlite3.connect(db) as c:
        c.execute("UPDATE messages SET tool_call = 'v2:{\"n\":1}' WHERE run_id = 'run-1'")

    before = _drop_count()
    reopened = SqliteStore(db)
    msgs = reopened.load_messages("run-1")
    reopened.close()

    assert msgs and msgs[0].tool_call is None, "单行坏数据不该拖垮整次加载——这条行为不变"
    assert _drop_count() == before + 1, "静默丢弃必须计数：否则回滚后的数据损失在面板上完全隐形"


def test_能解码的payload不计数() -> None:
    """反向对照：计数只对"真丢了"生效，否则这个指标就成了噪声。"""
    store = SqliteStore(_db())
    store.append_message(
        "run-2",
        Message(
            role="assistant",
            content="",
            tool_call=ToolCall(id="c1", name="weather.get", arguments={}),
        ),
    )
    before = _drop_count()
    msgs = store.load_messages("run-2")
    store.close()

    assert msgs and msgs[0].tool_call is not None
    assert _drop_count() == before
