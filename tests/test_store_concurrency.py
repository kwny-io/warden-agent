"""存储层的**并发**回归：一条共享连接不能被两个线程同时用。

这是从压测里抓到的真 bug：并发 8~12 时偶发 **HTTP 500**，服务端日志是
`sqlite3.InterfaceError: not an error`，位置在 `load_run`（授权路径读 Run 归属时）。
根因：连接是 `check_same_thread=False` **跨线程共享**，而**只有写方法加了锁**——
"一个线程读 + 另一个线程写"这条最常见的组合并发使用同一条 SQLite 连接，SQLite 就会抛
`InterfaceError`（"not an error" 这个措辞很有误导性，它其实是"连接被并发使用"）。

修法：读方法也进同一把锁（`SqliteStore._locked` 装饰器 + `RLock`）。

⚠️ 这条测试是**并发**的，天然带不确定性：它可能偶尔"恰好没撞上"。所以它的价值是
"**修之前会红**（压测里稳定复现）、修之后稳定绿"，而不是"能证明并发绝对安全"。
"""

from __future__ import annotations

import threading
from pathlib import Path

from warden_agent.core.run.status import AgentRun
from warden_agent.model.model import Message
from warden_agent.store.sqlite import SqliteStore


def test_读写并发使用同一连接不报错(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "t.db")
    # 先放一点数据，让读有东西可读
    for i in range(5):
        store.save_run(AgentRun(run_id=f"seed-{i}"))

    errors: list[BaseException] = []
    start = threading.Barrier(8)

    def worker(n: int) -> None:
        start.wait()                      # 尽量让 8 个线程同时冲
        try:
            for i in range(40):
                rid = f"w{n}-{i}"
                # 写
                store.save_run(AgentRun(run_id=rid))
                store.append_message(rid, Message(role="user", content=f"hi {i}"))
                # 读（和别的线程的写给同一条连接用 → 修之前这里会抛 InterfaceError）
                store.load_run(rid)
                store.load_messages(rid)
                store.list_runs(limit=5)
        except BaseException as e:        # noqa: BLE001 - 测试要把任何异常都收集起来
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发读写不应报错，实际：{errors[:3]}"


def test_读方法也持锁_避免与写交错(tmp_path: Path) -> None:
    """结构性断言：读方法确实被 `_locked` 包住了（防止有人"顺手"把装饰器删掉）。"""
    locked = {
        name for name, value in vars(SqliteStore).items()
        if getattr(value, "__wrapped__", None) is not None
    }
    for name in ("load_run", "load_messages", "list_runs", "list_approval_history",
                 "load_checkpoint", "list_checkpoints", "get_idempotent"):
        assert name in locked, f"{name} 没有加锁（读也必须在锁内——见本文件说明）"
