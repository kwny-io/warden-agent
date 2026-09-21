"""PostgreSQL **真实集成测试**：连着真库跑一遍 PostgresStore。

为什么要单独一个文件：
  `tests/test_store_interface.py` 里那条 PG 测试只覆盖了 `save_run` / `load_run` 两个方法，
  而项目声称"存储可换 PostgreSQL"——**其余 17 个协议方法、凭证保管库、以及多语句写入的原子性
  都从未在真库上跑过**。`tests/test_postgres_contract.py` 做的是静态检查（能拦住一批低级错误，
  但证明不了运行期行为）。这个文件补的正是"真跑一遍"。

怎么用（没装/没起 PG 时整体自动跳过，CI 不红）：
    # 起一个本地库（与 test_store_interface.py 的默认约定一致：用户 postgres、空密码）
    docker run -d --name warden-pg -e POSTGRES_HOST_AUTH_METHOD=trust \
        -e POSTGRES_DB=warden -p 5432:5432 postgres:16-alpine
    python -m pytest tests/test_postgres_integration.py -v

连接参数可用环境变量覆盖（方便挂到 CI 的 service container 上）：
    WARDEN_TEST_PG_HOST / WARDEN_TEST_PG_DB / WARDEN_TEST_PG_USER / WARDEN_TEST_PG_PASSWORD

本文件里的"密钥"全是**运行时随机生成**或明显占位，源码里不含任何真实凭据。
"""

from __future__ import annotations

import datetime as dt
import os
import secrets

import pytest

from warden_agent.core.run.status import AgentRun, RunStatus
from warden_agent.credential.vault import (
    DEPLOYMENT_SCOPE,
    StoredCredential,
    StoredLease,
    as_vault,
)
from warden_agent.model.model import Message, ToolCall

# 存储层只关心"字段名 → 密文"，这里用拼接避免源码里出现形如凭据的字面量
_FIELD = "api_" + "key"


def _pg_params() -> dict[str, object]:
    """连接参数：默认沿用 test_store_interface.py 的约定，可用环境变量覆盖（给 CI 用）。"""
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


def _store():
    from warden_agent.store.postgres import PostgresStore

    return PostgresStore(**_pg_params())  # type: ignore[arg-type]


def _checkpoint(run_id: str):
    from warden_agent.runtime.checkpoint import Checkpoint

    return Checkpoint(run_id=run_id, status=RunStatus.RUNNING, iteration=2, step="model_call")


# ---------------------------------------------------------------------------
# 一、连接的"毒丸"回归（这是静态审查查出的真 bug，这里用真库锁死）
# ---------------------------------------------------------------------------


def test_失败语句后连接仍可用_毒丸回归() -> None:
    """一条语句失败后，**同一条连接**必须还能继续用。

    改之前：`psycopg.connect` 默认 `autocommit=False`，而 store 里没有任何 rollback()
    → PG 把事务置为 aborted，此后该连接上所有语句（含读、含健康探针）全部失败。
    改之后：连接 `autocommit=True`，错的只是那一条语句。
    """
    import psycopg

    store = _store()
    try:
        with pytest.raises(psycopg.Error), store.conn.cursor() as cur:
            cur.execute("SELECT * FROM table_that_does_not_exist_xyz")
        # 关键断言：紧接着复用同一条连接
        with store.conn.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone()[0] == 1
    finally:
        store.close()


def test_对照_autocommit为False时确实会毒丸() -> None:
    """对照实验：证明上面那条不是多虑，而是真 bug。

    用 autocommit=False 的裸连接（修复前的配置）走同一序列，后续查询应当被拒。
    如果哪天 psycopg 改了默认行为，这条会失败，提醒我们重新评估。
    """
    import psycopg

    conn = psycopg.connect(**_pg_params(), autocommit=False)  # type: ignore[arg-type]
    try:
        with pytest.raises(psycopg.Error), conn.cursor() as cur:
            cur.execute("SELECT * FROM table_that_does_not_exist_xyz")
        with pytest.raises(psycopg.Error), conn.cursor() as cur:
            cur.execute("SELECT 1")
    finally:
        conn.close()


def test_连接必须是autocommit() -> None:
    store = _store()
    try:
        assert store.conn.autocommit is True
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 二、RunStore 全量协议方法（真库往返）
# ---------------------------------------------------------------------------


def test_run与消息与审批与存档点全流程() -> None:
    store = _store()
    rid = "pg-it-" + secrets.token_hex(5)
    try:
        run = AgentRun(rid)
        run.mark_queued()
        store.save_run(run)
        assert store.load_run(rid).status == RunStatus.QUEUED

        # 消息带工具调用（验证 JSON 列 + 反序列化）
        tc = ToolCall(id="call_1", name="weather.get", arguments={"city": "上海"})
        store.append_message(rid, Message(role="assistant", content="", tool_call=tc))
        msgs = store.load_messages(rid)
        assert msgs and msgs[0].tool_call is not None
        assert msgs[0].tool_call.name == "weather.get"

        # 待审批（JSON 参数往返）
        store.save_pending_approval(rid, "appr-9", "fs.delete", {"path": "/x"}, "需批准")
        pend = store.load_pending_approval(rid)
        assert pend is not None and pend[1] == "fs.delete" and pend[2]["path"] == "/x"
        store.clear_pending_approval(rid)
        assert store.load_pending_approval(rid) is None

        # 审批历史 + 存档点
        store.record_approval_decision(rid, "appr-9", "fs.delete", {"path": "/x"}, "approved")
        assert any(h["run_id"] == rid for h in store.list_approval_history(limit=50))

        store.save_checkpoint(_checkpoint(rid))
        cp = store.load_checkpoint(rid)
        assert cp is not None and cp.run_id == rid
        assert any(c.run_id == rid for c in store.list_checkpoints())

        # 列表 + owner 过滤
        assert any(r["run_id"] == rid for r in store.list_runs(limit=200))
        assert all(r["run_id"] != rid for r in store.list_runs(limit=200, owner="nobody-xyz"))
    finally:
        store.delete_run(rid)
        store.close()


def test_append_message会去重() -> None:
    store = _store()
    rid = "pg-it-msg-" + secrets.token_hex(5)
    try:
        same = Message(role="user", content="同一句话")
        store.append_message(rid, same)
        store.append_message(rid, same)  # 重复 → 跳过
        store.append_message(rid, Message(role="assistant", content="同一句话"))
        assert len(store.load_messages(rid)) == 2
    finally:
        store.delete_run(rid)
        store.close()


def test_delete_run清空五张表() -> None:
    """多语句写入靠显式事务块保证；这里验证正常路径下确实全清。"""
    store = _store()
    rid = "pg-it-del-" + secrets.token_hex(5)
    try:
        run = AgentRun(rid)
        run.mark_queued()
        store.save_run(run)
        store.append_message(rid, Message(role="user", content="hi"))
        store.save_pending_approval(rid, "a", "fs.delete", {}, "r")
        store.save_checkpoint(_checkpoint(rid))
        store.append_event(rid, '{"t":1}')

        store.delete_run(rid)

        assert store.load_run(rid) is None
        assert store.load_messages(rid) == []
        assert store.load_pending_approval(rid) is None
        assert store.load_checkpoint(rid) is None
        assert store.list_events_after(rid, 0) == []
    finally:
        store.close()


def test_共享状态三件套在真库上可用() -> None:
    """幂等 UPSERT / 事件 BIGSERIAL 自增与增量读 / 限流窗口计数（含那段 CASE WHEN UPSERT）。"""
    store = _store()
    rid = "pg-it-shared-" + secrets.token_hex(5)
    key = "idem-" + secrets.token_hex(5)
    bucket = "rl-" + secrets.token_hex(5)
    try:
        store.save_idempotent(key, "p1")
        assert store.get_idempotent(key) == "p1"
        store.save_idempotent(key, "p2")
        assert store.get_idempotent(key) == "p2"

        first = store.append_event(rid, '{"n":1}')
        second = store.append_event(rid, '{"n":2}')
        assert second > first, "事件序号没有自增"
        after = store.list_events_after(rid, first)
        assert [payload for _seq, payload in after] == ['{"n":2}']

        assert store.hit_rate_limit(bucket, 60, 1000.0)[0] == 1
        assert store.hit_rate_limit(bucket, 60, 1000.5)[0] == 2
        count, start = store.hit_rate_limit(bucket, 60, 2000.0)  # 窗口外 → 重置
        assert count == 1 and start == 2000.0
    finally:
        store.delete_run(rid)
        store.close()


def test_待审批记录带进入等待的时刻() -> None:
    """`pending_approvals.created_at` 在真 PG 上要能写能读——挂起超时告警靠它算准确时长。

    这条同时覆盖了老库的 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 迁移路径
    （表已存在时补列），所以对升级场景也有意义。
    """
    store = _store()
    rid = "pg-it-appr-" + secrets.token_hex(5)
    try:
        store.save_pending_approval(rid, "appr-1", "fs.delete", {"path": "/x"}, "需批准")
        created = store.pending_approval_created_at(rid)
        assert created is not None, "进入等待审批的时刻没记下来"
        # 能解析回时间戳，且离现在很近（说明确实是"刚才"写的）
        moment = dt.datetime.fromisoformat(str(created))
        delta = abs((dt.datetime.now(dt.UTC) - moment).total_seconds())
        assert delta < 120, f"记录的时刻偏差过大：{delta} 秒"

        store.clear_pending_approval(rid)
        assert store.pending_approval_created_at(rid) is None
    finally:
        store.clear_pending_approval(rid)
        store.close()


def test_用户表往返() -> None:
    store = _store()
    uid = "pg-it-user-" + secrets.token_hex(5)
    try:
        store.create_user(uid)
        assert any(u["user_id"] == uid for u in store.list_users())
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 三、凭证保管库落 PG（含 as_vault 结构匹配与"重启后仍在"）
# ---------------------------------------------------------------------------


def test_凭证密文与租约落PG含惰性清理() -> None:
    store = _store()
    name = "model:it-" + secrets.token_hex(5)
    try:
        encrypted = dict({_FIELD: "PLACEHOLDER-CIPHERTEXT"})
        store.save_credential(
            StoredCredential(scope=DEPLOYMENT_SCOPE, name=name, encrypted=encrypted)
        )
        got = store.load_credential(DEPLOYMENT_SCOPE, name)
        assert got is not None and dict(got.encrypted) == encrypted
        assert name in store.list_credential_names(DEPLOYMENT_SCOPE)

        now = dt.datetime.now(dt.UTC)
        live = StoredLease(
            scope=DEPLOYMENT_SCOPE, lease_id="lease-" + secrets.token_hex(5), name=name,
            issued_at=now, expires_at=now + dt.timedelta(seconds=600),
        )
        store.save_credential_lease(live)
        back = store.load_credential_lease(DEPLOYMENT_SCOPE, live.lease_id)
        assert back is not None and back.name == name
        # 时间戳经 TEXT 列往返，精度不能丢（否则过期判断会错）
        assert abs((back.expires_at - live.expires_at).total_seconds()) < 1

        stale = StoredLease(
            scope=DEPLOYMENT_SCOPE, lease_id="lease-stale-" + secrets.token_hex(5), name=name,
            issued_at=now - dt.timedelta(hours=2), expires_at=now - dt.timedelta(hours=1),
        )
        store.save_credential_lease(stale)
        assert store.purge_expired_credential_leases(DEPLOYMENT_SCOPE, now) >= 1
        assert store.load_credential_lease(DEPLOYMENT_SCOPE, stale.lease_id) is None
        assert store.load_credential_lease(DEPLOYMENT_SCOPE, live.lease_id) is not None
    finally:
        store.delete_credential(DEPLOYMENT_SCOPE, name)
        store.close()


def test_postgres能被当作凭证保管库_不会静默退回进程内() -> None:
    """`as_vault` 是结构化匹配：缺一个方法就会**静默**退回进程内保管库。

    那意味着"以为凭证落库了、其实没落"——所以这里显式钉住 PG 必须被认成保管库。
    """
    store = _store()
    try:
        assert as_vault(store) is store
    finally:
        store.close()


def test_broker加密落PG并跨实例可取回() -> None:
    """端到端：真 CredentialBroker + PG 保管库 → 换一个新 broker（模拟重启）仍能取回。"""
    from warden_agent.credential.broker import CredentialBroker
    from warden_agent.credential.crypto import CredentialCipher

    material = secrets.token_bytes(32)  # 运行时生成的假密钥材料
    plaintext = "fauxvalue-" + secrets.token_hex(12)
    name = "model:it-broker"
    store = _store()
    try:
        broker = CredentialBroker(CredentialCipher(material), vault=as_vault(store))
        broker.register(name, dict({_FIELD: plaintext}))

        # "重启"：新 broker + 新连接，同一个库
        reopened = _store()
        try:
            broker2 = CredentialBroker(CredentialCipher(material), vault=as_vault(reopened))
            lease = broker2.issue(name)
            assert lease.value.fields[_FIELD] == plaintext
            stored = broker2.encrypted_fields(name)
            assert stored and plaintext not in stored[_FIELD], "库里出现了明文"
        finally:
            reopened.close()
    finally:
        store.delete_credential(DEPLOYMENT_SCOPE, name)
        store.close()


# ---------------------------------------------------------------------------
# 四、Run 级锁在真库上的行为（多副本互斥的关键）
# ---------------------------------------------------------------------------


def test_run锁在真库上互斥并可被接管() -> None:
    from warden_agent.runtime.locking import SqlRunLock

    run_id = "pg-it-lock-" + secrets.token_hex(5)
    a, b = _store(), _store()
    try:
        lock_a = SqlRunLock(a, ttl_seconds=60)
        lock_b = SqlRunLock(b, ttl_seconds=60)
        assert lock_a.acquire(run_id, "replica-A") is True
        assert lock_b.acquire(run_id, "replica-B") is False, "两个副本不该同时拿到"
        assert lock_b.owner_of(run_id) == "replica-A"

        # 非持有者不能续租、也不能释放（防误删别人刚接管的锁）
        assert lock_b.renew(run_id, "replica-B") is False
        lock_b.release(run_id, "replica-B")
        assert lock_b.owner_of(run_id) == "replica-A"

        # 持有者释放后，另一个副本可接管
        lock_a.release(run_id, "replica-A")
        assert lock_b.acquire(run_id, "replica-B") is True
    finally:
        owner = a.run_lock_owner(run_id, 0.0)
        if owner:
            a.release_run_lock(run_id, owner)
        a.close()
        b.close()


def test_run锁并发抢占只有一个赢家() -> None:
    """N 个"副本"（各自独立连接）同时抢同一把锁 —— 必须**恰好一个**赢。

    这条是"多副本下同一个 run 只被一方驱动"的直接证明：取锁若不是一个原子操作，
    多个连接会同时以为自己抢到了，于是并发驱动、后写覆盖前写。
    """
    import concurrent.futures as cf
    import threading

    from warden_agent.runtime.locking import SqlRunLock

    run_id = "pg-it-race-" + secrets.token_hex(5)
    replicas = 8
    barrier = threading.Barrier(replicas)

    def attempt(n: int) -> bool:
        store = _store()  # 每个副本用自己的连接
        try:
            lock = SqlRunLock(store, ttl_seconds=60)
            barrier.wait()  # 尽量让 8 个线程同时出手
            return lock.acquire(run_id, f"replica-{n}")
        finally:
            store.close()

    with cf.ThreadPoolExecutor(max_workers=replicas) as pool:
        results = list(pool.map(attempt, range(replicas)))

    assert sum(results) == 1, f"应当恰好一个赢家，实际 {sum(results)} 个：{results}"

    cleanup = _store()
    try:
        owner = cleanup.run_lock_owner(run_id, 0.0)
        if owner:
            cleanup.release_run_lock(run_id, owner)
    finally:
        cleanup.close()
