"""PostgreSQL **启动期建表**的并发安全底座（唯一入口）。

## 为什么需要这个模块

多副本部署（k8s 同时拉起 N 个 pod、滚动重启、HPA 扩容）时，每个副本启动都会跑一遍建表 DDL。
而 `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` **不是并发安全的**：
它们在 PG 里实现为"先查存在性、再创建"，两个会话可以同时通过存在性检查，
然后其中一个撞上 `pg_class` 的命名唯一索引：

    psycopg.errors.UniqueViolation: duplicate key value violates unique constraint
    "pg_class_relname_nsp_index"
    DETAIL:  Key (relname, relnamespace)=(idx_memories_status, 2200) already exists.

后果是**其中一个副本启动即失败**。在生产里这表现为间歇性的 CrashLoopBackOff：
重试一次也许就好了（因为那次 schema 已经建好），所以它极难被归因——
"偶发启动失败"往往被当成基础设施抖动，而根因在应用自己的 DDL 上。

## 为什么是"唯一入口"而不是各存储自己写一遍

这个坑在本仓库被踩过**三次**，每一次都是"哪个存储出问题就修哪个"：

1. `store/postgres.py`：`__schema_version__` 的行写入撞主键（已修，见该文件的注释）；
2. `memory/store.py`：`idx_memories_status` 撞索引名（多副本 e2e 首跑即现形）；
3. `web/audit.py`：`_init()` 里裸跑的 `CREATE TABLE IF NOT EXISTS audit_log` —— 同一类问题，
   **当时还没爆出来**，是顺着 2 反查出来的。

三次里没有一次是"新问题"，全是"同一个修法没被复用到下一个存储"。所以这里把它收敛成
唯一入口：任何 PG 存储在构造期跑 DDL，都从 `serialized_schema_init` 走。
锁只在这一处拿，`tests/test_pg_concurrent_init.py` 会**逐个存储**断言"它的建表确实走了这里"，
于是"新加的存储忘了拿锁"会在测试里立刻红，而不是等下一次线上扩容才炸。

## 锁的语义

- 用 `pg_advisory_xact_lock`（事务级）：随事务提交/回滚**自动释放**，
  不会因为忘了 `unlock` 或进程被杀而把库锁死（会话级 `pg_advisory_lock` 要依赖连接断开）。
- 配 `SET LOCAL lock_timeout`：拿不到锁时在有限时间内**明确失败**（抛出可读的异常），
  而不是让副本启动无限期挂在等锁上——"启动卡住"比"启动失败"更难排查。
- 锁键是固定常量：**同一个数据库实例上只有一个 schema**，所以所有存储共用一把锁。
  这不只是省事——共用才意味着 `runs/messages` 与 `memories` 与 `audit_log` 的建表
  彼此之间也是串行的，不会交叉出锁等待环。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from types import TracebackType
from typing import Any, Protocol

#: 启动期 schema 初始化的建议锁键。
#:
#: ⚠️ **不要改这个值**：改成新值等于换了一把锁，正在滚动更新、还在跑老代码的副本
#: 拿的是旧锁，新老副本之间就不再互斥了（"升级期恰好是竞态最容易发生的时候"）。
#: 值本身无含义，只要求稳定且不与其它用途撞（审计链用的 `_AUDIT_CHAIN_LOCK`、
#: run 锁用的键都是另外的值，别混用）。
SCHEMA_INIT_LOCK_KEY = 20260928001

#: 拿不到锁时的等待上限。建表本身是毫秒级，所以 30 秒意味着"确实卡住了，该失败"。
DEFAULT_LOCK_TIMEOUT = "30s"


class LocalLock(Protocol):
    """同进程内互斥锁的形状：能 `with` 即可（`Lock` 与 `RLock` 都满足）。

    参数类型写全是因为 typeshed 里 `RLock.__exit__` 就是这三个位置参数，
    用 `*exc: object` 这种宽松写法反而会被判为不兼容（协议参数是逆变的）。
    """

    def __enter__(self) -> Any: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
        /,
    ) -> bool | None: ...


@contextmanager
def serialized_schema_init(
    conn: Any,
    *,
    lock_timeout: str = DEFAULT_LOCK_TIMEOUT,
    local_lock: LocalLock | None = None,
) -> Iterator[Any]:
    """在建议锁保护下跑一段启动期 DDL，`yield` 一个可直接 `execute` 的游标。

    用法（三个存储现在都是这个形状）::

        with self._lock, serialized_schema_init(self._conn) as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS ...")
            ...

    `local_lock` 是**同进程内**的多线程互斥（psycopg 的连接在同一时刻只应有一个游标在跑 DDL）。
    它是可选的：跨进程/跨副本的互斥完全由 PG 的建议锁负责，本参数只是把"同一进程里
    两个线程同时建表"这件事也挡住（避免同一连接上事务嵌套交错）。
    """
    # 顺序很重要：先本地锁，再开事务拿 PG 锁。反过来会出现"本地锁被占着、
    # 而它持有者正在等 PG 锁"的窗口，多线程下容易演变成自锁式等待。
    # 没传本地锁时用 `nullcontext()`（空上下文）——这里**不能**自己 new 一个
    # `threading.Lock()`：调用方可能已经持有同一把锁（嵌套调用），再拿一次就是自己等自己。
    with (
        local_lock if local_lock is not None else nullcontext()
    ), conn.transaction(), conn.cursor() as cur:
        # ⚠️ 用 `set_config(..., is_local => true)` 而不是 `SET LOCAL lock_timeout = %s`：
        # `SET` **不接受占位符**（PG 的 SET 不是普通 DML，`$1` 会被当成语法错误：
        # syntax error at or near "$1"）。set_config 的第三个参数 true 等价于 LOCAL
        # （事务结束即失效），所以语义一样但可参数化（不然就得把字符串拼进 SQL）。
        cur.execute("SELECT set_config('lock_timeout', %s, true)", (lock_timeout,))
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_INIT_LOCK_KEY,))
        yield cur
