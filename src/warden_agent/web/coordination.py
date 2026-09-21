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
"""

from __future__ import annotations

import base64
import json
import threading
import time
from typing import Any, Protocol


# ---------------------------------------------------------------------------
# 协议（Port）
# ---------------------------------------------------------------------------
class IdempotencyStore(Protocol):
    """`Idempotency-Key` → 缓存的响应快照。"""

    def get(self, key: str) -> dict[str, Any] | None: ...

    def put(self, key: str, value: dict[str, Any]) -> None: ...


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
            self._data[key] = value


class InProcessEventBus:
    """进程内事件总线（条件变量唤醒，无轮询延迟）。

    每个 run 的事件保留最近 `_KEEP` 条，防止无人订阅时无限增长。
    """

    _KEEP = 500

    def __init__(self) -> None:
        self._buckets: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        self._seq = 0
        self._cv = threading.Condition()

    def publish(self, run_id: str, event: dict[str, Any]) -> None:
        with self._cv:
            self._seq += 1
            bucket = self._buckets.setdefault(run_id, [])
            bucket.append((self._seq, event))
            if len(bucket) > self._KEEP:
                del bucket[: len(bucket) - self._KEEP]
            self._cv.notify_all()

    def poll(
        self, run_id: str, after_seq: int, timeout: float
    ) -> list[tuple[int, dict[str, Any]]]:
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                items = [
                    (seq, ev)
                    for seq, ev in self._buckets.get(run_id, [])
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
def coordination_for(store: Any, *, shared: bool) -> tuple[
    IdempotencyStore, EventBus, RateLimitStore
]:
    """按"是否多副本"造一套协调组件。

    `shared=False`：进程内实现（单副本默认，行为与历史一致）。
    `shared=True`：存储实现（多副本共享）——要求 store 具备共享状态方法
    （`get_idempotent` / `append_event` / `hit_rate_limit`），否则回落到进程内并告警。
    """
    if shared and all(
        hasattr(store, m)
        for m in ("get_idempotent", "append_event", "hit_rate_limit")
    ):
        return SqlIdempotencyStore(store), SqlEventBus(store), SqlRateLimitStore(store)
    if shared:
        import logging

        logging.getLogger(__name__).warning(
            "请求共享协调状态，但存储 %s 不支持（缺共享状态方法）——回落为进程内实现，"
            "多副本下幂等/事件/限流将各自为政。",
            type(store).__name__,
        )
    return InProcessIdempotencyStore(), InProcessEventBus(), InProcessRateLimitStore()
