"""记忆存储层的**并发**回归：一条共享 SQLite 连接不能被两个线程同时用。

背景同 `test_store_concurrency.py`：连接是 `check_same_thread=False` 跨线程共享的，
而 `SqliteMemoryStore` 之前**只有写方法加锁**——"一个线程读 + 另一个线程写"并发使用
同一条连接会抛 `sqlite3.InterfaceError`，在 HTTP 层表现为偶发 500。

修法：读方法也进同一把锁（`SqliteMemoryStore._locked` 装饰器 + `RLock`）。

⚠️ 并发测试天然带不确定性：它可能偶尔"恰好没撞上"。价值在于"修之前会红、修之后稳定绿"，
而不是"证明并发绝对安全"。
"""

from __future__ import annotations

import threading
from pathlib import Path

from warden_agent.memory import (
    MemoryContent,
    MemoryItem,
    MemoryScope,
    MemoryStatus,
    SqliteMemoryStore,
)
from warden_agent.memory.models import new_uid


def _item(key: str, text: str) -> MemoryItem:
    return MemoryItem(
        uid=new_uid(), scope=MemoryScope.USER, key=key, content=MemoryContent(text=text)
    )


def test_记忆读写并发使用同一连接不报错(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "mem.db")
    for i in range(5):
        store.save(_item(f"seed-{i}", f"seed {i}"))

    errors: list[BaseException] = []
    start = threading.Barrier(8)

    def worker(n: int) -> None:
        start.wait()  # 尽量让 8 个线程同时冲
        try:
            for i in range(40):
                item = _item(f"w{n}-{i}", f"hi {i}")
                store.save(item)  # 写
                # 读（与别的线程的写给同一条连接用 → 修之前这里会抛 InterfaceError）
                store.find(item.uid)
                store.find_ref(MemoryScope.USER, item.key)
                store.latest(MemoryScope.USER, item.key)
                store.search(MemoryScope.USER)
                store.list_by_status(MemoryStatus.ACTIVE, 5)
        except BaseException as e:  # noqa: BLE001 - 测试要把任何异常都收集起来
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发读写不应报错，实际：{errors[:3]}"


def test_记忆读方法也持锁(tmp_path: Path) -> None:
    """结构性断言：读方法确实被 `_locked` 包住了（防止有人"顺手"把装饰器删掉）。"""
    locked = {
        name for name, value in vars(SqliteMemoryStore).items()
        if getattr(value, "__wrapped__", None) is not None
    }
    for name in ("find", "find_ref", "latest", "search", "list_by_status"):
        assert name in locked, f"{name} 没有加锁（读也必须在锁内——见本文件说明）"
