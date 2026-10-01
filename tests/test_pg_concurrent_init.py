"""PG 存储在**多副本同时启动**时建表安全（这一族问题在本仓库踩过三次的回归件）。

## 被回归的那个真问题

`CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` 在 PostgreSQL 里
**不是并发安全的**：它们实现成"先查存在性、再创建"，两个副本可以同时通过存在性检查，
后到的那个撞 `pg_class` 的命名唯一索引：

    psycopg.errors.UniqueViolation: duplicate key value violates unique constraint
    "pg_class_relname_nsp_index"
    DETAIL:  Key (relname, relnamespace)=(idx_memories_status, 2200) already exists.

后果是**其中一个副本启动即死**（k8s 同时拉起 / 滚动重启就是这时序；生产里表现为
间歇性 CrashLoopBackOff，重试一次往往就好了，所以极难归因）。

这不是新问题，而是**同一个修法没被复用**：
`store/postgres.py` 因为版本行撞主键已经拿过建议锁，但没推广，
于是 `memory/store.py` 又踩一次（多副本 e2e 首跑即现形），
而 `web/audit.py` 的 `_init()` 当时还裸着——第三个是顺着第二个反查出来的。
现在三个都走 `store/pg_schema.py` 的唯一入口，本文件负责"以后不许再漏"。

## 三类断言，各自解决不同的问题

1. `test_建表时会持有迁移建议锁`：**机制证明**。用 `pg_locks` 直接观察到
   "有一个未授予的 advisory 锁，键就是 SCHEMA_INIT_LOCK_KEY"，而不是靠计时猜
   （"等了 1 秒还没结束"这种写法在慢机器上会假绿）。去掉锁它必红。
2. `test_N个副本同时启动建表全部成功`：**现场回归**。复现线上时序（N 个进程同时构造）。
   注意它是"烟囱"不是"证明"——竞态无法被测试证明不存在，机制保证由 1 负责。
3. `test_PG侧的建表语句都必须走唯一入口`：**静态守卫**。扫 src 的 AST，
   凡是 PG 侧的 DDL 字面量必须出现在 `serialized_schema_init` 的 `with` 块里
   （SQLite 侧走显式白名单）。这条才是"新加一个 PG 存储忘了拿锁"会在 CI 变红的原因。
"""

from __future__ import annotations

import ast
import concurrent.futures as cf
import os
import pathlib
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest

from warden_agent.store.pg_schema import SCHEMA_INIT_LOCK_KEY

# ---------------------------------------------------------------------------
# 与其它 PG 测试同一套口径（起法见 tests/test_postgres_integration.py 顶部注释）
# ---------------------------------------------------------------------------


def _pg_params() -> dict[str, object]:
    return {
        "host": os.environ.get("WARDEN_TEST_PG_HOST", "localhost"),
        "dbname": os.environ.get("WARDEN_TEST_PG_DB", "warden"),
        "user": os.environ.get("WARDEN_TEST_PG_USER", "postgres"),
        "password": os.environ.get("WARDEN_TEST_PG_PASSWORD", ""),
    }


def _pg_available() -> bool:
    try:
        import psycopg

        conn = psycopg.connect(connect_timeout=2, **_pg_params())  # type: ignore[arg-type]
        conn.close()
        return True
    except Exception:  # noqa: BLE001 - 任何失败都视为"没有可用的库"
        return False


pytestmark = pytest.mark.skipif(
    not _pg_available(),
    reason="需要可用的 PostgreSQL 才会运行（起法见本文件顶部注释）",
)


def _raw_conn():
    import psycopg

    return psycopg.connect(**_pg_params(), autocommit=True)  # type: ignore[arg-type]


def _build_store() -> Any:
    from warden_agent.store.postgres import PostgresStore

    return PostgresStore(**_pg_params())  # type: ignore[arg-type]


def _build_memory_store() -> Any:
    from warden_agent.memory import PostgresMemoryStore

    return PostgresMemoryStore(connect_kwargs=_pg_params())  # type: ignore[arg-type]


def _build_audit_store() -> Any:
    from warden_agent.web.audit import PostgresAuditStore

    return PostgresAuditStore(connect_kwargs=_pg_params(), chain_key=None)  # type: ignore[arg-type]


def _close_quietly(store: Any) -> None:
    """把构造出来的存储连的连接放掉。

    三个存储的收尾方式不一致（`PostgresStore` 有 `close()`，另两个把连接放在 `_conn`），
    所以这里两條路都试。不关的后果在 psycopg 里是 `ResourceWarning`，
    而 pytest 会把它升级成一条 ERROR——那不是被测行为，不该干扰结论。
    """
    close = getattr(store, "close", None)
    if callable(close):
        close()
        return
    conn = getattr(store, "_conn", None)
    if conn is not None:
        conn.close()


#: 三个 PG 存储各自的构造方式（构造即建表——被测行为就在构造里）。
_BUILDERS: list[tuple[str, Callable[[], Any]]] = [
    ("PostgresStore", _build_store),
    ("PostgresMemoryStore", _build_memory_store),
    ("PostgresAuditStore", _build_audit_store),
]


# ---------------------------------------------------------------------------
# 一、机制证明：建表时真的持有（并等待）那把建议锁
# ---------------------------------------------------------------------------


def _waiting_on_schema_lock() -> bool:
    """现在库里有没有"正在等这把建议锁"的会话（未授予状态）。

    PG 把单参数 `pg_advisory_xact_lock(bigint)` 的键拆成 (classid, objid) 两列存，
    所以这里按这两个值精确匹配，而不是"只要有未授予的 advisory 锁就算"——
    后者会把审计链锁（`_AUDIT_CHAIN_LOCK`）的等待也算进来，那就证明不了什么。
    """
    classid = SCHEMA_INIT_LOCK_KEY >> 32
    objid = SCHEMA_INIT_LOCK_KEY & 0xFFFFFFFF
    conn = _raw_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"
                " AND NOT granted AND classid::bigint = %s AND objid::bigint = %s",
                (classid, objid),
            )
            row = cur.fetchone()
            return bool(row and row[0])
    finally:
        conn.close()


@pytest.mark.parametrize("name,build", _BUILDERS, ids=[b[0] for b in _BUILDERS])
def test_建表时会持有迁移建议锁(name: str, build: Callable[[], Any]) -> None:
    """另一个会话占着锁时，构造存储必须**在等**这把锁。

    这条为什么比"等 1 秒还没结束"强：慢机器上"没结束"可能只是慢，
    而 `pg_locks` 里的未授予记录直接说明"它在等的是哪把锁"，
    所以在任何机器上都只有"走了唯一入口"才可能通过。
    """
    blocker = _raw_conn()
    started = threading.Event()
    finished = threading.Event()
    errors: list[BaseException] = []

    def boot() -> None:
        started.set()
        try:
            store = build()
            _close_quietly(store)
        except BaseException as exc:  # noqa: BLE001 - 原样带回主线程断言
            errors.append(exc)
        finally:
            finished.set()

    # 用 try/finally 收掉堵锁的连接：它要是漏了，psycopg 会在 __del__ 里发
    # ResourceWarning，而本仓库的 filterwarnings 把 Warning 升级成 ERROR——
    # 那会把真正的断言结论埋掉（调试这条测试时就被埋过一次）。
    try:
        with blocker.transaction(), blocker.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_INIT_LOCK_KEY,))
            thread = threading.Thread(target=boot, name=f"boot-{name}", daemon=True)
            thread.start()
            assert started.wait(5), f"{name}: 启动线程没跑起来"

            deadline = time.monotonic() + 10
            waiting = False
            while time.monotonic() < deadline:
                if _waiting_on_schema_lock():
                    waiting = True
                    break
                time.sleep(0.05)
            assert waiting, (
                f"{name} 的建表没有在等 pg_advisory_xact_lock({SCHEMA_INIT_LOCK_KEY})："
                "说明它没走 store/pg_schema.serialized_schema_init。"
                "多副本同时启动时这会让其中一个副本直接启动失败"
                "（UniqueViolation: pg_class_relname_nsp_index）"
            )
            assert not finished.is_set(), f"{name}: 锁被占着，建表却已经结束了"

        # 事务结束 → 建议锁释放 → 建表应该能继续（反过来卡住说明锁没被正确释放）
        assert finished.wait(30), f"{name}: 锁释放后 30s 内建表仍未完成（锁泄漏？）"
        assert not errors, f"{name}: 建表抛异常 {errors!r}"
    finally:
        blocker.close()


# ---------------------------------------------------------------------------
# 二、现场回归：N 个副本同时启动
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,build", _BUILDERS, ids=[b[0] for b in _BUILDERS])
def test_N个副本同时启动建表全部成功(name: str, build: Callable[[], Any]) -> None:
    """N 个"副本"同时构造：必须全部成功，不允许任何一个是 UniqueViolation。

    真并发是必须的——串行调用时第二次起表/索引已存在，`IF NOT EXISTS` 会直接跳过，
    这条测试就白跑了（这也正是"只有多副本才碰得到"的原因）。
    """
    replicas = 6
    start_together = threading.Barrier(replicas)

    def boot(_n: int) -> None:
        # barrier 放在构造之前：让 6 个副本尽量同一瞬间进建表那段，
        # 否则线程池调度会把它们错开，竞态就测不出来
        start_together.wait(timeout=30)
        _close_quietly(build())

    with cf.ThreadPoolExecutor(max_workers=replicas) as pool:
        futures = [pool.submit(boot, n) for n in range(replicas)]
        # 不用 pool.map：它没有超时，任一副本挂住会把 pytest 会话吊死
        done, not_done = cf.wait(futures, timeout=60)
        assert not not_done, (
            f"{name}: {len(not_done)}/{replicas} 个副本 60s 内没起来——并发初始化卡住了。"
            "先看 pg_stat_activity 里有没有等锁的会话"
        )
        for future in done:
            future.result()  # UniqueViolation 会在这里原样抛出（就是被测的失败）


# ---------------------------------------------------------------------------
# 三、静态守卫：PG 侧的建表语句只能出现在唯一入口里
# ---------------------------------------------------------------------------

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "warden_agent"

#: DDL 字面量的关键字（SQLite 侧也有，靠下面的白名单区分）。
_DDL_MARKERS = ("CREATE TABLE", "CREATE INDEX", "CREATE UNIQUE INDEX")

#: 允许"不拿 PG 锁就跑 DDL"的地方：键是相对 `src/warden_agent` 的路径，
#: 值是**豁免的函数名集合**（None = 整个文件豁免）。
#:
#: 为什么要显式白名单而不是"按后端猜"：猜错的那天就是又一个副本启动失败。
#: 往这里加东西必须是有意识的行为——加错的代价只是一行代码，漏加的代价是线上 CrashLoop。
_SQLITE_EXEMPT: dict[str, set[str] | None] = {
    # 整个文件都是 SQLite 实现
    "store/sqlite.py": None,
    "rag/knowledge.py": None,
    # 这两种是"同一个文件里既有 PG 实现又有 SQLite 实现"，所以只能精确到函数
    "web/audit.py": {"SqliteAuditStore._init"},
    "memory/store.py": {"SqliteMemoryStore._init"},
}


def _callee_name(node: ast.expr) -> str:
    """取一个调用的函数名（`a.b.c(...)` 取 `c`）。"""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


class _DdlSiteFinder(ast.NodeVisitor):
    """收集：(相对路径, 所属函数, 是否在 serialized_schema_init 的 with 块里)。"""

    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.classes: list[str] = []
        self.functions: list[str] = []
        self.guard_depth = 0
        self.sites: list[tuple[str, str, bool]] = []

    # ——— 作用域 ———
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.classes.append(node.name)
        self.generic_visit(node)
        self.classes.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    # ——— 唯一入口：with serialized_schema_init(...) as cur: ———
    def visit_With(self, node: ast.With) -> None:
        guarded = any(
            isinstance(item.context_expr, ast.Call)
            and _callee_name(item.context_expr.func) == "serialized_schema_init"
            for item in node.items
        )
        if guarded:
            self.guard_depth += 1
        self.generic_visit(node)
        if guarded:
            self.guard_depth -= 1

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self.visit_With(node)  # type: ignore[arg-type]

    # ——— DDL ———
    def visit_Call(self, node: ast.Call) -> None:
        if _callee_name(node.func).startswith("execute"):
            literal = self._literal_sql(node)
            if literal is not None and any(m in literal for m in _DDL_MARKERS):
                owner = ".".join([*self.classes, *self.functions]) or "<module>"
                self.sites.append((self.rel, owner, self.guard_depth > 0))
        self.generic_visit(node)

    @staticmethod
    def _literal_sql(node: ast.Call) -> str | None:
        """调用里有没有一个字符串字面量参数（三引号、隐式拼接都算同一个 Constant）。"""
        for arg in [*node.args, *(kw.value for kw in node.keywords)]:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                return arg.value
        return None


def _ddl_sites() -> list[tuple[str, str, bool]]:
    sites: list[tuple[str, str, bool]] = []
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        finder = _DdlSiteFinder(rel)
        finder.visit(ast.parse(path.read_text(encoding="utf-8")))
        sites.extend(finder.sites)
    return sites


def test_PG侧的建表语句都必须走唯一入口() -> None:
    """扫 src 的 AST：PG 侧 DDL 必须出现在 `serialized_schema_init` 的 with 块里。

    这是"以后新加的 PG 存储忘了拿锁"唯一的防线：前两次踩坑都不是写错代码，
    而是"修法没被复用到下一个存储"，靠人记是记不住的。
    """
    sites = _ddl_sites()

    # 先证明这次扫描**不是空跑**：三个 PG 存储都必须被扫到（否则断言恒真）
    by_file = {rel for rel, _owner, _guarded in sites}
    for expected in ("store/postgres.py", "memory/store.py", "web/audit.py"):
        assert expected in by_file, (
            f"扫描没在 {expected} 里找到任何 DDL 字面量——扫描本身失效了"
            f"（找到的文件：{sorted(by_file)}）"
        )

    offenders = [
        (rel, owner)
        for rel, owner, guarded in sites
        if not guarded and owner not in (_SQLITE_EXEMPT.get(rel) or set())
        and rel not in {k for k, v in _SQLITE_EXEMPT.items() if v is None}
    ]
    assert not offenders, (
        "下面这些建表语句没有走 store/pg_schema.serialized_schema_init"
        "（PG 的 CREATE ... IF NOT EXISTS 不是并发安全的，多副本同时启动会撞 "
        "pg_class_relname_nsp_index 直接启动失败）：\n"
        + "\n".join(f"  - src/warden_agent/{rel} :: {owner}" for rel, owner in offenders)
        + "\n如果这是 SQLite 实现（不需要 PG 锁），把它加进本文件的 _SQLITE_EXEMPT 白名单，"
        "并在提交信息里说明为什么；如果是 PG 实现，用 "
        "`with serialized_schema_init(conn, local_lock=...):` 包住。"
    )
