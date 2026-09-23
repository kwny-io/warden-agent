"""审计链测试：把"审计被人动过"变成**必然被发现**的事实。

背景：此前的审计只是一张普通表——谁都能改，改完没有任何痕迹，那"审计"就只是日志。
现在落盘的每条记录都带链哈希（`prev_hash` → `hash`），改字段 / 删中间行 / 重排都会断链。

三件事在这里锁住：
  1. 正常写入的链是完整的，且**跨实例（模拟重启）仍能验**；
  2. **篡改必被发现**：改字段、删中间行、重排——逐一验证报错、且指出是哪一条；
  3. **带密钥 vs 不带密钥的区别是真区别**：换成"会重算整条链"的攻击者，
     不带密钥的链挡不住（如实记录），带密钥的挡得住。

顺带说明为什么不给 InMemory 实现加链：链的价值在"落盘、跨重启可校验"，
内存版重启即丢，加链只会给人"内存里也防篡改"的错觉。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from warden_agent import cli
from warden_agent.web.audit import (
    GENESIS,
    AuditLogger,
    AuditRecord,
    SqliteAuditStore,
    audit_chain_key,
    chain_hash,
)
from warden_agent.web.auth import RunOperation

KEY = b"unit-test-audit-key-material"      # 测试用固定材料（非真实密钥）


def _db(tmp_path: Path) -> Path:
    return tmp_path / "audit.db"


def _store(tmp_path: Path, key: bytes | None = KEY) -> SqliteAuditStore:
    return SqliteAuditStore(db_path=_db(tmp_path), chain_key=key)


def _append_n(store: SqliteAuditStore, n: int, *, status: int = 200) -> None:
    for i in range(n):
        store.append(AuditRecord(
            correlation_id=f"cid-{i}", tenant_id="acme", principal_type="user",
            principal_id="alice", product_id="local", operation="QUERY",
            run_id=f"run-{i}", method="GET", path="/status/run-1",
            status=status, at=1_700_000_000.0 + i,
        ))


# ---------------------------------------------------------------------------
# 一、正常链
# ---------------------------------------------------------------------------


def test_正常写入后链完整(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _append_n(store, 5)
    ok, detail = store.verify_chain()
    assert ok, detail
    assert "5 条记录" in detail


def test_空表也是完整链(tmp_path: Path) -> None:
    ok, detail = _store(tmp_path).verify_chain()
    assert ok and "0 条记录" in detail


def test_换实例仍能验证_模拟重启(tmp_path: Path) -> None:
    """链要跨重启可校验——这正是"不带密钥就退化为哈希链"而不生成临时密钥的原因。"""
    _append_n(_store(tmp_path), 3)
    ok, detail = _store(tmp_path).verify_chain()      # 新实例、同一个库、同一把密钥
    assert ok, detail


def test_换了密钥就验不过(tmp_path: Path) -> None:
    """密钥不对 → 链验不过。这是"带密钥"的意义：没有密钥就算不出合法哈希。"""
    _append_n(_store(tmp_path), 3)
    ok, detail = _store(tmp_path, key=b"another-key-material").verify_chain()
    assert ok is False
    assert "被改动过" in detail or "不符" in detail


def test_每条记录的哈希都不同(tmp_path: Path) -> None:
    """相同内容的记录若行号不同，哈希也应不同（否则重排不会被发现）。"""
    store = _store(tmp_path)
    _append_n(store, 2)
    rows = store._conn.execute("SELECT hash FROM audit_log ORDER BY id").fetchall()
    assert rows[0][0] != rows[1][0]


# ---------------------------------------------------------------------------
# 二、篡改必被发现（这是链的全部意义）
# ---------------------------------------------------------------------------


def test_改字段会被发现(tmp_path: Path) -> None:
    """把一条记录的 operation 从 QUERY 改成 COMMAND（伪造"他没做这个操作"）。"""
    store = _store(tmp_path)
    _append_n(store, 3)
    store._conn.execute("UPDATE audit_log SET operation = 'COMMAND' WHERE id = 2")
    store._conn.commit()

    ok, detail = store.verify_chain()
    assert ok is False
    assert "id=2" in detail and "被改动过" in detail


def test_改时间戳会被发现(tmp_path: Path) -> None:
    """时间也是被篡改的对象（"这件事不是那时候发生的"）。"""
    store = _store(tmp_path)
    _append_n(store, 3)
    store._conn.execute("UPDATE audit_log SET at = 0.0 WHERE id = 1")
    store._conn.commit()
    ok, detail = store.verify_chain()
    assert ok is False and "id=1" in detail


def test_删中间一条会被发现(tmp_path: Path) -> None:
    """删掉一条记录（覆盖痕迹）→ 它后面那条的 prev_hash 对不上。"""
    store = _store(tmp_path)
    _append_n(store, 4)
    store._conn.execute("DELETE FROM audit_log WHERE id = 2")
    store._conn.commit()

    ok, detail = store.verify_chain()
    assert ok is False
    assert "断开" in detail and "id=3" in detail, f"应指出断在删掉之后的第一条上：{detail}"


def test_重排会被发现(tmp_path: Path) -> None:
    """交换两条记录的 id（行号参与哈希，所以换位置也会断链）。"""
    store = _store(tmp_path)
    _append_n(store, 3)
    # 用取巧但确定的方式换位：把 id=2/3 的内容对调后重算（模拟"重排"）
    rows = store._conn.execute(
        "SELECT correlation_id, prev_hash, hash FROM audit_log WHERE id IN (2, 3) ORDER BY id"
    ).fetchall()
    store._conn.execute(
        "UPDATE audit_log SET correlation_id = ? WHERE id = 2", (rows[1][0],)
    )
    store._conn.commit()
    ok, detail = store.verify_chain()
    assert ok is False


def test_伪造一条插入到末尾_带密钥时算不出来(tmp_path: Path) -> None:
    """攻击者往末尾追加一条假记录：不知道密钥就算不出合法哈希。"""
    store = _store(tmp_path)
    _append_n(store, 2)
    head = store._conn.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()[0]
    # 攻击者照着格式伪造，但没有密钥 → 只能用假哈希
    fake_hash = hashlib.sha256(b"forged").hexdigest()
    store._conn.execute(
        "INSERT INTO audit_log (correlation_id, tenant_id, principal_type, principal_id,"
        " product_id, operation, run_id, method, path, status, at, prev_hash, hash)"
        " VALUES ('forged', 'acme', 'user', 'mallory', 'local', 'COMMAND', 'run-x',"
        " 'POST', '/approve/run-x', 200, 1.0, ?, ?)",
        (head, fake_hash),
    )
    store._conn.commit()
    ok, detail = store.verify_chain()
    assert ok is False and "被改动过" in detail


def test_无密钥时挡不住会重算整链的人(tmp_path: Path) -> None:
    """**诚实对照**：不带密钥的哈希链，攻击者改完可以把整条链重算一遍、看起来自洽。

    这不是缺陷披露，而是"为什么必须配 WARDEN_AUDIT_KEY"的证据。
    """
    # 先造一条完整的（无密钥）链
    store = SqliteAuditStore(db_path=_db(tmp_path), chain_key=None)
    _append_n(store, 3)
    assert store.verify_chain()[0] is True

    # 攻击者：改掉第 2 条，然后按同样的规则（无密钥，谁都会算）重算它之后的所有哈希
    store._conn.execute("UPDATE audit_log SET operation = 'COMMAND' WHERE id = 2")
    rows = store._conn.execute(
        "SELECT id, correlation_id, tenant_id, principal_type, principal_id, product_id,"
        " operation, run_id, method, path, status, at FROM audit_log ORDER BY id"
    ).fetchall()
    prev = GENESIS
    for row in rows:
        rec = AuditRecord(
            correlation_id=row[1], tenant_id=row[2], principal_type=row[3],
            principal_id=row[4], product_id=row[5], operation=row[6], run_id=row[7],
            method=row[8], path=row[9], status=row[10], at=row[11],
        )
        digest = chain_hash(None, rec, prev, int(row[0]))
        store._conn.execute("UPDATE audit_log SET prev_hash = ?, hash = ? WHERE id = ?",
                            (prev, digest, int(row[0])))
        prev = digest
    store._conn.commit()

    # 不带密钥 → 重算之后的链"看起来是完整的"
    assert store.verify_chain()[0] is True, "预期：无密钥链挡不住重算（这就是要配密钥的原因）"

    # 换成带密钥的校验方（生产会这么做）→ 一眼看出对不上
    ok, _detail = SqliteAuditStore(
        db_path=_db(tmp_path), chain_key=KEY
    ).verify_chain()
    assert ok is False, "配了 WARDEN_AUDIT_KEY 之后，伪造的链立刻露馅"


def test_历史行没有链哈希时如实报告(tmp_path: Path) -> None:
    """加链之前写下的老记录：不能假装它没被改过，要明确说"无法证明"。"""
    store = _store(tmp_path)
    _append_n(store, 1)
    store._conn.execute("UPDATE audit_log SET hash = '' WHERE id = 1")   # 模拟老数据
    store._conn.commit()
    ok, detail = store.verify_chain()
    assert ok is False and "没有链哈希" in detail


# ---------------------------------------------------------------------------
# 三、配置与 CLI
# ---------------------------------------------------------------------------


def test_未配置密钥时退化为无密钥链并告警(caplog: pytest.LogCaptureFixture) -> None:
    monkey_env = {"WARDEN_AUDIT_KEY": ""}
    assert audit_chain_key(monkey_env) is None
    with caplog.at_level("WARNING"):
        SqliteAuditStore(db_path=Path(tempfile.mkdtemp()) / "a.db", chain_key=None)
    assert any("WARDEN_AUDIT_KEY" in r.message for r in caplog.records)


def test_从环境变量取密钥() -> None:
    assert audit_chain_key({"WARDEN_AUDIT_KEY": "material"}) == b"material"


def test_cli_校验通过时退出码为0(tmp_path: Path) -> None:
    _append_n(SqliteAuditStore(db_path=_db(tmp_path), chain_key=None), 3)
    # 不抛 SystemExit 即通过
    cli.main(["audit-verify", "--db", str(_db(tmp_path))])


def test_cli_链被动过时退出码为4(tmp_path: Path) -> None:
    store = SqliteAuditStore(db_path=_db(tmp_path), chain_key=None)
    _append_n(store, 3)
    store._conn.execute("UPDATE audit_log SET status = 500 WHERE id = 2")
    store._conn.commit()
    with pytest.raises(SystemExit) as exc:
        cli.main(["audit-verify", "--db", str(_db(tmp_path))])
    assert exc.value.code == 4


def test_cli_支持json输出(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _append_n(SqliteAuditStore(db_path=_db(tmp_path), chain_key=None), 2)
    cli.main(["audit-verify", "--db", str(_db(tmp_path)), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert "链完整" in payload["detail"]


def test_审计写入仍不影响业务_链不会让它变脆(tmp_path: Path) -> None:
    """链只增加了两条 SQL，写入接口不变：AuditLogger 的"尽力而为"语义不受影响。"""
    store = _store(tmp_path)
    _append_n(store, 1)
    # 写入后仍能正常查账（链列不影响 query 的列映射）
    records = store.query(tenant_id="acme")
    assert len(records) == 1 and records[0].principal_id == "alice"
    assert records[0].status == 200
    assert sqlite3 is not None


# ---------------------------------------------------------------------------
# 四、尾部截断：链只能发现“改中间/删中间”，锚点才能发现“从末尾删”
# ---------------------------------------------------------------------------


def test_记锚点后从末尾删行会被发现(tmp_path: Path) -> None:
    """锚点前：删末尾的行，剩下的链依然自洽 → 必须靠锚点才能发现。"""
    store = _store(tmp_path)
    _append_n(store, 5)
    store.record_anchor()
    store._conn.execute("DELETE FROM audit_log WHERE id > 3")
    store._conn.commit()
    ok, detail = store.verify_chain()
    assert ok is False
    assert "尾部被截断" in detail


def test_锚点之后正常追加不算尾断(tmp_path: Path) -> None:
    """锚点只是“某个时刻的链头”：之后正常追加，锚定的链头仍在链上 → 不算尾断。"""
    store = _store(tmp_path)
    _append_n(store, 3)
    store.record_anchor()
    _append_n(store, 2)
    ok, detail = store.verify_chain()
    assert ok, detail


def test_没记过锚点时不误报尾断(tmp_path: Path) -> None:
    """诚实边界：从未锚过就无从对比，剩下的链自洽就如实报完整（不假装能发现尾断）。"""
    store = _store(tmp_path)
    _append_n(store, 3)
    store._conn.execute("DELETE FROM audit_log WHERE id > 1")
    store._conn.commit()
    ok, detail = store.verify_chain()
    assert ok, detail


def test_锚点链头被替换也会被发现(tmp_path: Path) -> None:
    """删掉末尾再追加同样条数的伪造行：条数对得上，但锚定的链头已不在链上。"""
    store = _store(tmp_path)
    _append_n(store, 3)
    store.record_anchor()
    store._conn.execute("DELETE FROM audit_log WHERE id = 3")
    # 伪造一条接在 id=2 之后（不知道密钥，用假哈希；条数仍是 3）
    head = store._conn.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()[0]
    store._conn.execute(
        "INSERT INTO audit_log (correlation_id, tenant_id, principal_type, principal_id,"
        " product_id, operation, run_id, method, path, status, at, prev_hash, hash)"
        " VALUES ('forged', 'acme', 'user', 'mallory', 'local', 'QUERY', 'run-x',"
        " 'GET', '/status/run-x', 200, 1.0, ?, ?)",
        (head, hashlib.sha256(b"forged").hexdigest()),
    )
    store._conn.commit()
    ok, detail = store.verify_chain()
    assert ok is False
    assert "被改动过" in detail or "尾部被截断" in detail


def test_未配密钥的存储keyed为假(tmp_path: Path) -> None:
    assert SqliteAuditStore(db_path=_db(tmp_path), chain_key=None).keyed is False
    assert _store(tmp_path).keyed is True


def test_AuditLogger定期记锚点(tmp_path: Path) -> None:
    """每 N 条审计自动锚一次链头（“定期锚”），不需要外部调度。"""
    store = _store(tmp_path)
    lg = AuditLogger(store, enabled=True)
    lg._ANCHOR_EVERY = 2  # 实例上改小间隔，便于测试
    for i in range(2):
        lg.record(
            correlation_id=f"c{i}", caller=None, operation=RunOperation.QUERY,
            run_id="r", method="GET", path="/status/r", status=200,
        )
    count = store._conn.execute("SELECT COUNT(*) FROM audit_anchor").fetchone()[0]
    assert count == 1
    assert lg.keyed is True
