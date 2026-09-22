"""`web/coordination.py` 的边界分支补充测试。

已有 `test_shared_state.py` / `test_notify_event_bus.py` 覆盖了主路径与装配选择；
这里补**错误/边界分支**的真实行为：
  - 进程内总线的「等待超时返回空」与「追上后清除缺口标记」；
  - 进程内限流的「窗口翻转后重新计数」；
  - `_decode_idempotent` 对坏 JSON / 非 dict 的容错；
  - `SqlEventBus.poll` 跳过坏 JSON、全部坏 JSON 时超时返回空；
  - `SqlRateLimitStore.hit` 的转发；
  - `_event_bus_for` 在 notify 模式但存储不支持监听连接时的回落。
"""

from __future__ import annotations

import logging
from typing import Any

from warden_agent.web.coordination import (
    InProcessEventBus,
    InProcessRateLimitStore,
    SqlEventBus,
    SqlRateLimitStore,
    _decode_idempotent,
    _event_bus_for,
)


def test_进程内总线_无事件时等待超时返回空() -> None:
    bus = InProcessEventBus(keep=5)
    assert bus.poll("never", after_seq=0, timeout=0.02) == []


def test_进程内总线_追上后清除缺口标记_可再次告警() -> None:
    """缺口告警每 run 只报一次；消费者追上（after_seq 不再落后）后应解除，允许下次再报。"""
    bus = InProcessEventBus(keep=2)
    for i in range(4):
        bus.publish("run-x", {"n": i})  # 保留最近 2 条（seq 3、4）

    logger_obj = logging.getLogger("warden_agent.web.coordination")
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture()
    logger_obj.addHandler(handler)
    try:
        # 第一次：after_seq=1 落后于最早 seq=3 → 告警
        bus.poll("run-x", after_seq=1, timeout=0.01)
        # 追上：after_seq=5 不落后 → 走 else 分支，清除缺口标记
        assert bus.poll("run-x", after_seq=5, timeout=0.01) == []
        # 再次落后 → 又应告警（标记已被清除）
        bus.poll("run-x", after_seq=1, timeout=0.01)
    finally:
        logger_obj.removeHandler(handler)

    gaps = [r for r in records if "缺口" in r.getMessage()]
    assert len(gaps) == 2, "追上后应允许再次报告缺口"


def test_进程内限流_窗口翻转后重新计数() -> None:
    rl = InProcessRateLimitStore()
    assert rl.hit("k", window_seconds=10, now=100.0) == (1, 100.0)
    assert rl.hit("k", window_seconds=10, now=105.0) == (2, 100.0)
    # now-start == 10 >= window → 窗口翻转，计数归零重来
    assert rl.hit("k", window_seconds=10, now=110.0) == (1, 110.0)


def test_幂等解码_坏JSON与非dict返回None() -> None:
    assert _decode_idempotent("not-json") is None       # JSONDecodeError
    assert _decode_idempotent("123") is None            # 非 dict
    assert _decode_idempotent('["a", "b"]') is None     # 非 dict


class _FakeEventStore:
    def __init__(self, rows: list[tuple[int, str]]) -> None:
        self._rows = rows
        self.appended: list[tuple[str, str, int | None]] = []

    def list_events_after(self, run_id: str, after_seq: int) -> list[tuple[int, str]]:
        return list(self._rows)

    def append_event(self, run_id: str, data: str, keep: int | None = None) -> None:
        self.appended.append((run_id, data, keep))


def test_SqlEventBus_poll跳过坏JSON_全坏时超时返回空() -> None:
    bus = SqlEventBus(_FakeEventStore([(1, "not-json"), (2, "{bad")]), poll_interval=0.005)
    assert bus.poll("r", after_seq=0, timeout=0.02) == []


def test_SqlEventBus_poll_正常解析返回事件() -> None:
    bus = SqlEventBus(_FakeEventStore([(1, '{"a": 1}'), (2, '{"b": 2}')]), poll_interval=0.005)
    assert bus.poll("r", after_seq=0, timeout=0.02) == [(1, {"a": 1}), (2, {"b": 2})]


def test_SqlEventBus_publish转发到存储并带keep() -> None:
    store = _FakeEventStore([])
    bus = SqlEventBus(store, keep=7)
    bus.publish("r", {"x": 1})
    assert len(store.appended) == 1
    run_id, data, keep = store.appended[0]
    assert run_id == "r" and keep == 7
    assert '"x": 1' in data


def test_SqlRateLimitStore_hit转发到存储() -> None:
    class _RL:
        def hit_rate_limit(self, key: str, window: int, now: float) -> tuple[Any, Any]:
            return 3, 1.5

    assert SqlRateLimitStore(_RL()).hit("k", 60, 1.0) == (3, 1.5)


def test_event_bus_for_notify模式不支持监听连接时回落轮询() -> None:
    bus = _event_bus_for(object(), "notify")
    assert type(bus) is SqlEventBus


def test_event_bus_for_poll模式返回轮询() -> None:
    bus = _event_bus_for(object(), "poll", keep=3)
    assert type(bus) is SqlEventBus
    assert bus._keep == 3  # noqa: SLF001
