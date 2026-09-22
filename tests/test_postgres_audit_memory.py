"""PostgreSQL 上的**审计**与**记忆**后端真库测试。

为什么需要这个文件：
  多副本交付的前提是"审计与记忆也进共享库"。此前它们只有 SQLite 实现，于是
  `deploy/k8s/configmap.yaml` 被迫写 `WARDEN_AUDIT=0`——等于多副本交付**没有审计**。
  本文件在真库里把两个新后端跑一遍，重点是**审计链在多副本并发写时不分叉**
  （靠 `pg_advisory_xact_lock` 串行化链头推进）。

怎么用（没起 PG 时整体自动跳过，CI 不红）：
    docker run -d --name warden-pg -e POSTGRES_HOST_AUTH_METHOD=trust \
        -e POSTGRES_DB=warden -p 5432:5432 postgres:16-alpine
    python -m pytest tests/test_postgres_audit_memory.py -v

连接参数用 `WARDEN_TEST_PG_*` 覆盖（与 test_postgres_integration.py 同一套约定）。
"""

from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import os
import secrets
import threading

import pytest

from warden_agent.memory.models import (
    MemoryContent,
    MemoryItem,
    MemoryScope,
    MemoryStatus,
    new_uid,
)
from warden_agent.web.audit import AuditRecord


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


def _audit_store(chain_key: bytes | str | None = "env"):
    """默认用环境键（与 `warden audit-verify --pg` 一致），保证同一张表里的链可被 CLI 复核。"""
    from warden_agent.web.audit import PostgresAuditStore

    return PostgresAuditStore(connect_kwargs=_pg_params(), chain_key=chain_key)


def _memory_store():
    from warden_agent.memory import PostgresMemoryStore

    return PostgresMemoryStore(connect_kwargs=_pg_params())


def _record(tag: str, status: int = 200) -> AuditRecord:
    return AuditRecord(
        correlation_id=f"corr-{tag}",
        tenant_id="t-it",
        principal_type="user",
        principal_id="alice",
        product_id="http",
        operation="QUERY",
        run_id=f"run-{tag}",
        method="GET",
        path=f"/status/{tag}",
        status=status,
        at=1700000000.0,
    )


# ---------------------------------------------------------------------------
# 一、审计：链完整 / 篡改必被发现 / 导出带链字段 / 过滤
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _trim_own_audit_rows():
    """每个用例后删掉**本次新增**的审计行（只删尾部，不动已有链——剩余链仍完整）。

    审计表是全局 append-only 的，删除中间行会断链；但删除**末尾**若干行不影响
    剩余各行之间的 prev_hash→hash 衔接。这样测试不会把共享开发库的链越堆越长，
    也不会让 `warden audit-verify --pg` 因为残留测试数据而变红。
    """
    conn = _raw_conn()
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT COALESCE(MAX(id), 0) FROM audit_log")
                baseline = int(cur.fetchone()[0])
            except Exception:  # noqa: BLE001 - 表还没建（首次运行）→ 视作 0
                baseline = 0
    finally:
        conn.close()
    yield
    conn = _raw_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(id), 0) FROM audit_log")
            if int(cur.fetchone()[0]) > baseline:
                cur.execute("DELETE FROM audit_log WHERE id > %s", (baseline,))
    except Exception:  # noqa: BLE001 - 清理失败不该让用例失败
        pass
    finally:
        conn.close()


def test_审计写入后链完整且查询过滤可用() -> None:
    store = _audit_store()
    tag = secrets.token_hex(6)
    try:
        for i in range(3):
            store.append(_record(f"{tag}-{i}"))

        ok, detail = store.verify_chain()
        assert ok, f"新写的链应当是完整的：{detail}"

        # 按 run_id 过滤能查到自己刚写的那条（其余记录也在表里，用 run_id 精确定位）
        got = store.query(run_id=f"run-{tag}-1", limit=50)
        assert any(r.correlation_id == f"corr-{tag}-1" for r in got)

        # 导出带链字段（归档/取证要能独立复核）
        rows = store.export_records()
        mine = [r for r in rows if r["correlation_id"] == f"corr-{tag}-0"]
        assert mine and mine[0]["hash"] and "prev_hash" in mine[0]
    finally:
        store.close()


def test_篡改一条记录会被验链发现_还原后又变回完整() -> None:
    """防篡改的兑现：改字段 → 断链；改回来 → 恢复完整（净零，不污染共享链）。"""
    store = _audit_store()
    tag = secrets.token_hex(6)
    store.append(_record(tag))
    try:
        rows = [r for r in store.export_records() if r["correlation_id"] == f"corr-{tag}"]
        assert rows, "刚写的记录应能导出"
        row_id, original_hash = int(rows[0]["id"]), str(rows[0]["hash"])

        raw = _raw_conn()
        try:
            with raw.cursor() as cur:
                cur.execute("UPDATE audit_log SET hash = %s WHERE id = %s", ("deadbeef", row_id))
        finally:
            raw.close()

        ok, _ = store.verify_chain()
        assert not ok, "被改过的记录必须导致验链失败"

        # 还原（把哈希原样写回）→ 链恢复完整
        raw = _raw_conn()
        try:
            with raw.cursor() as cur:
                cur.execute(
                    "UPDATE audit_log SET hash = %s WHERE id = %s", (original_hash, row_id)
                )
        finally:
            raw.close()
        ok, detail = store.verify_chain()
        assert ok, f"还原后链应恢复完整：{detail}"
    finally:
        store.close()


def test_多副本并发写链不分叉() -> None:
    """**这是 advisory 锁的核心验收**：N 个副本（各自连接）并发追加，链必须仍完整。

    没有 `pg_advisory_xact_lock` 时，多个连接会读到同一个链头、各自接一条，
    链立刻分叉，`verify_chain()` 报断链。这条测试就是"多副本能同时写审计"的证明。
    """
    replicas = 6
    per_replica = 5
    barrier = threading.Barrier(replicas)

    def worker(n: int) -> None:
        store = _audit_store()
        try:
            barrier.wait()  # 尽量同时出手
            for i in range(per_replica):
                store.append(_record(f"race-{n}-{i}"))
        finally:
            store.close()

    with cf.ThreadPoolExecutor(max_workers=replicas) as pool:
        list(pool.map(worker, range(replicas)))

    checker = _audit_store()
    try:
        ok, detail = checker.verify_chain()
        assert ok, f"并发写入后链断了（advisory 锁没生效？）：{detail}"
    finally:
        checker.close()


# ---------------------------------------------------------------------------
# 二、记忆：往返 / 归属隔离 / 跨实例持久
# ---------------------------------------------------------------------------


def _item(scope: MemoryScope, key: str, text: str, owner: str) -> MemoryItem:
    return MemoryItem(
        uid=new_uid(), scope=scope, key=key,
        content=MemoryContent(text=text), owner=owner,
    )


def test_记忆落PG可跨实例读回且按归属隔离() -> None:
    key = "k-it-" + secrets.token_hex(6)
    alice, bob = "alice-it", "bob-it"
    store = _memory_store()
    try:
        store.save(_item(MemoryScope.USER, key, "alice 的偏好", alice))
        store.save(_item(MemoryScope.USER, key, "bob 的偏好", bob))

        # 模拟重启：换一个连接/实例读回
        reopened = _memory_store()
        try:
            only_alice = reopened.find_ref(MemoryScope.USER, key, owner=alice)
            assert len(only_alice) == 1 and only_alice[0].owner == alice
            only_bob = reopened.find_ref(MemoryScope.USER, key, owner=bob)
            assert len(only_bob) == 1 and only_bob[0].owner == bob
            # 不过滤则两条都在
            assert len(reopened.find_ref(MemoryScope.USER, key)) == 2

            latest = reopened.latest(MemoryScope.USER, key, owner=alice)
            assert latest is not None and latest.content.text == "alice 的偏好"

            hits = reopened.search(MemoryScope.USER, "偏好", owner=alice)
            assert any(h.owner == alice for h in hits)
            assert all(h.owner == alice for h in hits), "检索不能带出别人的记忆"
        finally:
            reopened.close()
    finally:
        _cleanup_memory(key)
        store.close()


def test_记忆状态与过期时间往返不丢() -> None:
    key = "k-it2-" + secrets.token_hex(6)
    owner = "u-it"
    store = _memory_store()
    try:
        item = _item(MemoryScope.SESSION, key, "临时事实", owner)
        item.status = MemoryStatus.PENDING
        item.expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
        store.save(item)

        back = store.find(item.uid)
        assert back is not None
        assert back.status == MemoryStatus.PENDING
        assert back.expires_at is not None
        assert abs((back.expires_at - item.expires_at).total_seconds()) < 1
    finally:
        _cleanup_memory(key)
        store.close()


def _cleanup_memory(key: str) -> None:
    """清掉测试写入的记忆行（PG 记忆后端无 delete API，直接按 key 清，避免污染开发库）。"""
    conn = _raw_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM memories WHERE key = %s", (key,))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 三、装配：后端选择正确（PG 主存储 → PG 审计；否则 SQLite）
# ---------------------------------------------------------------------------


def test_postgres_store有ping_就绪探针才可用() -> None:
    """`/health/ready` 会对存储调 `ping()`。PostgresStore 曾**没有**这个方法 →
    PG 部署下就绪探针抛 AttributeError 被吞 → **永远 503**、K8s pod 永不 Ready。"""
    from warden_agent.store.postgres import PostgresStore
    from warden_agent.web.health import readiness

    store = PostgresStore(**_pg_params())  # type: ignore[arg-type]
    try:
        store.ping()  # 不抛即通过
        result = readiness(store)
        assert result.status == "ok", result.checks
    finally:
        store.close()


def test_审计后端跟着主存储后端走(tmp_path) -> None:
    from warden_agent.store.postgres import PostgresStore
    from warden_agent.store.sqlite import SqliteStore
    from warden_agent.web.audit import audit_store_for_backend

    pg = PostgresStore(**_pg_params())  # type: ignore[arg-type]
    try:
        got = audit_store_for_backend(pg, sqlite_db_path=":memory:")
        from warden_agent.web.audit import PostgresAuditStore

        assert isinstance(got, PostgresAuditStore)
    finally:
        got.close()   # 审计用的是另开的一条连接，必须显式关（否则 psycopg __del__ 告警）
        pg.close()

    # SQLite 主存储 → SQLite 审计（用 tmp_path，不碰真库）
    from warden_agent.web.audit import SqliteAuditStore

    db_path = str(tmp_path / "t.db")
    sqlite = SqliteStore(db_path)
    try:
        got = audit_store_for_backend(sqlite, sqlite_db_path=db_path)
        assert isinstance(got, SqliteAuditStore)
    finally:
        # 先关审计连接再关主存储：Windows 上文件被占用时清理会失败
        got.close()
        sqlite.close()
