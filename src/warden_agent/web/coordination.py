"""跨副本协调状态：把"必须全局一致"的三样东西做成可插拔实现。

多副本部署时，这三样状态如果留在进程内，每个副本各算一份，就会出现：

  - **幂等表**：同一 `Idempotency-Key` 打到不同副本 → 不幂等（重复执行）；
  - **SSE 事件流**：客户端连副本 A，事件产生在副本 B → 事件收不到；
  - **限流计数**：实际总限额 ≈ 配置值 × 副本数（保护形同虚设）。

所以这里把三者抽成 Protocol，给两套实现：

  | 组件 | 进程内（单副本默认） | 存储版（多副本共享） |
  |---|---|---|
  | `IdempotencyStore` | `InProcessIdempotencyStore` | `SqlIdempotencyStore` |
  | `EventBus`         | `InProcessEventBus`         | `SqlEventBus`         |
  | `RateLimitStore`   | `InProcessRateLimitStore`   | `SqlRateLimitStore`   |

存储版复用 `RunStore` 的共享表（SQLite 或 PostgreSQL），不引入 Redis 等新依赖。
`SqlEventBus` 用**轮询增量**读事件表，比 Pub/Sub 延迟高一点（默认 250ms，可调），
但胜在零新依赖、通用（SQLite/Postgres 都行）；对延迟敏感可换成 Redis/NATS 实现，
只要满足 `EventBus` 协议，上层一行不用改。
`PostgresNotifyEventBus` 则是"轮询 + LISTEN/NOTIFY 唤醒"：事件仍落表、订阅仍读表，
通知只用来把等待从"睡满间隔"变成"变化即醒"——所以**丢通知最多退回轮询，不会丢事件**。
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import threading
import time
from typing import Any, Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 协议（Port）
# ---------------------------------------------------------------------------
class IdempotencyStore(Protocol):
    """`Idempotency-Key` → 缓存的响应快照。

    幂等必须**防并发**：只做"先查后写"会有 TOCTOU——两个同 key 的请求在"查"处都读到空、
    于是**各执行一次副作用**（正是多副本/网关重试要防的场景）。所以提供"占位"原语：
    执行前先 `reserve`（原子地"仅当不存在时占位"），只有占到位的那个请求才真正执行。
    """

    def get(self, key: str) -> dict[str, Any] | None: ...

    def put(self, key: str, value: dict[str, Any]) -> None: ...

    def reserve(self, key: str) -> bool:
        """原子占位：**仅当该 key 尚不存在时**写入一条"处理中"标记，返回是否占到。"""
        ...

    def release(self, key: str) -> None:
        """释放占位（仅当它仍是"处理中"标记时）——失败请求要允许后续重试。"""
        ...


# "处理中"占位标记：body 为 None（正常响应一定有 body 或至少 status）。
_PENDING: dict[str, Any] = {"status_code": 0, "headers": {}, "body": None}


def _is_pending(item: dict[str, Any] | None) -> bool:
    return item is not None and item.get("body") is None and item.get("status_code") == 0


class EventBus(Protocol):
    """按 run 分频道的事件流：publish 产出、poll 增量拉取。"""

    def publish(self, run_id: str, event: dict[str, Any]) -> None: ...

    def poll(
        self, run_id: str, after_seq: int, timeout: float
    ) -> list[tuple[int, dict[str, Any]]]: ...


class RateLimitStore(Protocol):
    """固定窗口计数：同一 key 在 window_seconds 内累计了多少次。"""

    def hit(
        self, bucket_key: str, window_seconds: int, now: float
    ) -> tuple[int, float]: ...


# ---------------------------------------------------------------------------
# 进程内实现（单副本默认；零依赖、无轮询延迟）
# ---------------------------------------------------------------------------
class InProcessIdempotencyStore:
    """进程内幂等表。多副本下不共享——需要共享请用 `SqlIdempotencyStore`。"""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._data.get(key)
            return None if item is None else dict(item)

    def put(self, key: str, value: dict[str, Any]) -> None:
        with self._lock:
            self._data[key] = dict(value)

    def reserve(self, key: str) -> bool:
        """原子占位（锁内 check-and-set），防"两个同 key 请求都读到空"的 TOCTOU。"""
        with self._lock:
            if key in self._data:
                return False
            self._data[key] = dict(_PENDING)
            return True

    def release(self, key: str) -> None:
        """只在仍是"处理中"标记时删除——别把已经写好的响应快照误删。"""
        with self._lock:
            if _is_pending(self._data.get(key)):
                self._data.pop(key, None)


DEFAULT_EVENT_KEEP = 500


class InProcessEventBus:
    """进程内事件总线（条件变量唤醒，无轮询延迟）。

    每个 run 只保留最近 `keep` 条事件，防止无人订阅时无限增长。**这是"保留条数"上限**：
    如果消费者在两次轮询之间积累的事件超过它，最早的会被丢弃——所以：
      - 丢弃时**打警告**（含累计条数），不让它静默发生；
      - 消费者（`poll`）若发现自己的 `after_seq` 比保留的最早事件还旧，也会**收到缺口语义**
        的警告（此前是"悄悄少几条"，消费方无从察觉）。
    注意：事件只承载**进度展示**；最终结果与消息走 messages/存档，不因为这些事件丢而丢。
    `keep` 可用 `WARDEN_EVENT_KEEP` 配置（见 run_server）。
    """

    def __init__(self, keep: int | None = None) -> None:
        self._keep = int(keep) if keep and int(keep) > 0 else DEFAULT_EVENT_KEEP
        self._buckets: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        self._seq = 0
        self._dropped: dict[str, int] = {}
        self._gap_warned: set[str] = set()
        self._cv = threading.Condition()

    @property
    def keep(self) -> int:
        """单个 run 最多保留多少条事件（运维可见）。"""
        return self._keep

    def dropped_count(self, run_id: str) -> int:
        """该 run 累计被丢弃的事件数（0 = 从未丢过）。"""
        with self._cv:
            return self._dropped.get(run_id, 0)

    def publish(self, run_id: str, event: dict[str, Any]) -> None:
        with self._cv:
            self._seq += 1
            bucket = self._buckets.setdefault(run_id, [])
            bucket.append((self._seq, event))
            overflow = len(bucket) - self._keep
            if overflow > 0:
                del bucket[:overflow]
                before = self._dropped.get(run_id, 0)
                self._dropped[run_id] = before + overflow
                if before == 0:
                    # 首次丢弃才告警（避免刷屏），并说明"只影响进度展示"
                    logger.warning(
                        "事件保留上限 %d 被突破：run=%s 开始丢弃最早事件（累计已丢 %d 条）；"
                        "慢消费者可能看不到部分进度事件，但最终结果不受影响",
                        self._keep, run_id, overflow,
                    )
            self._cv.notify_all()

    def poll(
        self, run_id: str, after_seq: int, timeout: float
    ) -> list[tuple[int, dict[str, Any]]]:
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                bucket = self._buckets.get(run_id, [])
                # 缺口检测：消费者要的起点比"保留的最早事件"还旧 → 中间有事件被丢
                if bucket and after_seq and bucket[0][0] > after_seq + 1:
                    if run_id not in self._gap_warned:
                        self._gap_warned.add(run_id)
                        logger.warning(
                            "事件流存在缺口：run=%s 请求 seq>%d，但最早可读的是 %d"
                            "（更早的已被保留上限丢弃），消费者会看到事件不连续",
                            run_id, after_seq, bucket[0][0],
                        )
                else:
                    self._gap_warned.discard(run_id)  # 追上了 → 允许下次再报
                items = [
                    (seq, ev)
                    for seq, ev in bucket
                    if seq > after_seq
                ]
                if items:
                    return items
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._cv.wait(remaining)


class InProcessRateLimitStore:
    """进程内固定窗口计数。多副本下总限额会翻倍——需要共享请用 `SqlRateLimitStore`。"""

    def __init__(self) -> None:
        self._buckets: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    def hit(
        self, bucket_key: str, window_seconds: int, now: float
    ) -> tuple[int, float]:
        with self._lock:
            start, count = self._buckets.get(bucket_key, (now, 0))
            if now - start >= window_seconds:
                start, count = now, 0
            count += 1
            self._buckets[bucket_key] = (start, count)
            return count, start


# ---------------------------------------------------------------------------
# 存储实现（多副本共享；复用 RunStore 的共享表）
# ---------------------------------------------------------------------------
def _encode_idempotent(value: dict[str, Any]) -> str:
    """把响应快照序列化进库：body 是 bytes，用 base64 装进 JSON。"""
    body = value.get("body")
    return json.dumps(
        {
            "status_code": value.get("status_code", 200),
            "headers": value.get("headers", {}),
            "body": base64.b64encode(body).decode("ascii")
            if isinstance(body, bytes)
            else None,
        },
        ensure_ascii=False,
    )


def _decode_idempotent(raw: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    body = payload.get("body")
    return {
        "status_code": payload.get("status_code", 200),
        "headers": payload.get("headers", {}),
        "body": base64.b64decode(body) if body else None,
    }


class SqlIdempotencyStore:
    """存储版幂等表：多副本读写同一张表，幂等才真正跨副本成立。"""

    def __init__(self, store: Any) -> None:
        self._store = store

    def get(self, key: str) -> dict[str, Any] | None:
        raw = self._store.get_idempotent(key)
        return None if raw is None else _decode_idempotent(str(raw))

    def put(self, key: str, value: dict[str, Any]) -> None:
        self._store.save_idempotent(key, _encode_idempotent(value))

    def reserve(self, key: str) -> bool:
        """原子占位——落在存储层用 `INSERT ... ON CONFLICT DO NOTHING`（跨副本也原子）。"""
        return bool(self._store.reserve_idempotent(key, _encode_idempotent(_PENDING)))

    def release(self, key: str) -> None:
        self._store.release_idempotent(key, _encode_idempotent(_PENDING))


class SqlEventBus:
    """存储版事件总线：事件落库，多副本按 id 增量轮询。

    轮询间隔默认 250ms——比 Pub/Sub 慢，但零新依赖且对 SQLite/Postgres 通用。
    """

    def __init__(self, store: Any, poll_interval: float = 0.25) -> None:
        self._store = store
        self._poll_interval = poll_interval

    def publish(self, run_id: str, event: dict[str, Any]) -> None:
        self._store.append_event(run_id, json.dumps(event, ensure_ascii=False))

    def poll(
        self, run_id: str, after_seq: int, timeout: float
    ) -> list[tuple[int, dict[str, Any]]]:
        deadline = time.monotonic() + timeout
        while True:
            rows = self._store.list_events_after(run_id, after_seq)
            if rows:
                out: list[tuple[int, dict[str, Any]]] = []
                for seq, data in rows:
                    try:
                        out.append((int(seq), json.loads(str(data))))
                    except json.JSONDecodeError:
                        continue
                if out:
                    return out
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return []
            time.sleep(min(self._poll_interval, remaining))


# 通知频道名（唯一事实源，供文档/测试引用）。
# 注意：下面的 LISTEN / NOTIFY 语句里**内联写死**了这个名字——Postgres 的
# LISTEN/NOTIFY 不接受参数占位符（频道名是标识符而不是值），所以没法参数化；
# 而把 SQL 存成变量再 execute 又正是要避免的写法。两处的名字由
# `tests/test_notify_event_bus.py` 断言一致，防止漂移。
NOTIFY_CHANNEL = "warden_events"


class PostgresNotifyEventBus(SqlEventBus):
    """在 SqlEventBus 之上加 LISTEN/NOTIFY：把事件延迟从"轮询间隔"降到"通知到达"。

    设计原则（关键）：**通知只是提示，正确性仍然靠落表 + 读表**。
      - `publish` 先把事件写进 `run_events`（durable），再 NOTIFY 一句"有新东西了"；
      - 订阅端醒来后**一律回表读增量**，不把通知内容当数据；
      - 通知可能丢（发出时对面还没 LISTEN），所以等待超时后会**退回轮询**再查一次。

    结果：丢通知最多让这一次退回轮询间隔（不丢事件、不乱序），
    而正常路径的延迟只有通知往返时间（实测远低于默认 250ms 轮询间隔）。

    为什么不让通知携带事件本体：Postgres 的 NOTIFY 载荷上限 8000 字节（事件可能超），
    而且一旦依赖载荷，"丢通知"就等于"丢事件"。
    """

    def __init__(
        self,
        store: Any,
        poll_interval: float = 0.25,
        *,
        notify_timeout: float = 1.0,
        listener: Any = None,
    ) -> None:
        super().__init__(store, poll_interval)
        self._notify_timeout = notify_timeout
        # LISTEN 需要**独占一条连接**（等待通知期间它被占住，不能与 store 的读写共用）
        self._listener = listener if listener is not None else store.new_connection()
        self._owns_listener = listener is None
        with self._listener.cursor() as cur:
            cur.execute("LISTEN warden_events")   # 频道名与 NOTIFY_CHANNEL 一致（有测试守着）

    def publish(self, run_id: str, event: dict[str, Any]) -> None:
        super().publish(run_id, event)                    # 先落表（durable）
        with self._store.conn.cursor() as cur:            # 再喊一声（只是提示，不带数据）
            cur.execute("NOTIFY warden_events")

    def poll(
        self, run_id: str, after_seq: int, timeout: float
    ) -> list[tuple[int, dict[str, Any]]]:
        deadline = time.monotonic() + timeout
        while True:
            rows = self._read(run_id, after_seq)          # 快路径：可能已经有事件了
            if rows:
                return rows
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return []
            self._wait_for_notify(min(self._notify_timeout, remaining))
            # 醒来后回到循环开头**再读表**——通知不带数据，也可能丢

    def _read(self, run_id: str, after_seq: int) -> list[tuple[int, dict[str, Any]]]:
        out: list[tuple[int, dict[str, Any]]] = []
        for seq, data in self._store.list_events_after(run_id, after_seq):
            try:
                out.append((int(seq), json.loads(str(data))))
            except json.JSONDecodeError:
                continue
        return out

    def _wait_for_notify(self, timeout: float) -> None:
        """等一条通知（最多 timeout 秒）。拿不到就正常返回，由调用方退回轮询。

        用 `notifies(timeout=...)` 而不是自己 select：psycopg 会顺手处理连接保活。
        监听连接不可用时**不能让订阅崩掉**——退回轮询即可（事件不会丢）。
        """
        try:
            for _ in self._listener.notifies(timeout=timeout, stop_after=1):
                return
        except Exception:  # noqa: BLE001 - 监听连接坏了不该拖垮订阅
            logger.warning("LISTEN 连接异常，本次退回轮询等待（事件不会丢）")
            time.sleep(min(self._poll_interval, timeout))

    def close(self) -> None:
        if self._owns_listener:
            with contextlib.suppress(Exception):
                self._listener.close()


class SqlRateLimitStore:
    """存储版限流计数：多副本共享同一窗口计数，全局限额不翻倍。"""

    def __init__(self, store: Any) -> None:
        self._store = store

    def hit(
        self, bucket_key: str, window_seconds: int, now: float
    ) -> tuple[int, float]:
        count, start = self._store.hit_rate_limit(bucket_key, window_seconds, now)
        return int(count), float(start)


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------
def _event_bus_for(store: Any, mode: str) -> EventBus:
    """按需造事件总线：`notify` 用 LISTEN/NOTIFY 唤醒，其余用纯轮询。

    LISTEN/NOTIFY 只有 Postgres 有，且需要能"再开一条连接"（`new_connection`）——
    不满足就**回落为轮询并告警**：能力不够要说出来，而不是假装用了通知。
    """
    if mode != "notify":
        return SqlEventBus(store)
    if callable(getattr(store, "new_connection", None)):
        return PostgresNotifyEventBus(store)
    logger.warning(
        "请求 WARDEN_EVENT_BUS=notify，但存储 %s 不支持另开监听连接（LISTEN 需要 Postgres）"
        "——回落为轮询实现。事件不会丢，只是延迟等于轮询间隔。",
        type(store).__name__,
    )
    return SqlEventBus(store)


def coordination_for(
    store: Any, *, shared: bool, event_bus: str = "poll",
    event_keep: int | None = None,
) -> tuple[IdempotencyStore, EventBus, RateLimitStore]:
    """按"是否多副本"造一套协调组件。

    `shared=False`：进程内实现（单副本默认，行为与历史一致）。
    `shared=True`：存储实现（多副本共享）——要求 store 具备共享状态方法
    （`get_idempotent` / `append_event` / `hit_rate_limit`），否则回落到进程内并告警。
    `event_bus="notify"`：多副本时用 LISTEN/NOTIFY 唤醒（只对 Postgres 生效，见
    `PostgresNotifyEventBus`）；不满足条件时回落为轮询并告警——**能力不够就明说**。
    `event_keep`：进程内事件总线**每个 run 保留的最近事件数**（默认 500）。
    """
    if shared and all(
        hasattr(store, m)
        for m in ("get_idempotent", "append_event", "hit_rate_limit")
    ):
        return (
            SqlIdempotencyStore(store),
            _event_bus_for(store, event_bus),
            SqlRateLimitStore(store),
        )
    if shared:
        import logging

        logging.getLogger(__name__).warning(
            "请求共享协调状态，但存储 %s 不支持（缺共享状态方法）——回落为进程内实现，"
            "多副本下幂等/事件/限流将各自为政。",
            type(store).__name__,
        )
    return (
        InProcessIdempotencyStore(),
        InProcessEventBus(keep=event_keep),
        InProcessRateLimitStore(),
    )
