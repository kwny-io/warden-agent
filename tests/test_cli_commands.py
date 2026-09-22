"""CLI 子命令的端到端行为：HTTP 渲染 + 本机库命令（prune/recover/stuck/backup/restore/rotate）。

HTTP 类命令通过替换 `cli._client` 注入假客户端——测的是**命令的渲染与退出码**，
不依赖真实服务。本机类命令对临时 SQLite 库跑，验证"真读真写真清扫"。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from warden_agent import cli
from warden_agent.store.sqlite import SqliteStore
from warden_agent.web.audit import AuditRecord, SqliteAuditStore

# ---------------------------------------------------------------------------
# 假 HTTP 客户端（只实现 CLI 真正用到的方法）
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, status_code: int = 200, data=None, text: str | None = None,
                 lines: list[str] | None = None) -> None:
        self.status_code = status_code
        self._data = data if data is not None else {}
        self.text = text if text is not None else json.dumps(self._data, ensure_ascii=False)
        self._lines = lines or []

    def json(self):
        return self._data

    def iter_lines(self):
        return iter(self._lines)

    # stream(...) 的返回值要能当上下文管理器用
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Client:
    def __init__(self, responses: dict[tuple[str, str], object]) -> None:
        self._r = responses

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def _pick(self, method: str, url: str):
        key = (method, url)
        assert key in self._r, f"假客户端收到未预期请求 {key}"
        value = self._r[key]
        if isinstance(value, Exception):
            raise value
        return value

    def get(self, url: str):
        return self._pick("GET", url)

    def post(self, url: str, json=None):
        return self._pick("POST", url)

    def stream(self, method: str, url: str, json=None):
        return self._pick(method, url)


def _use(monkeypatch, responses):
    monkeypatch.setattr(cli, "_client", lambda: _Client(responses))


# ---------------------------------------------------------------------------
# HTTP 类命令：渲染与退出码
# ---------------------------------------------------------------------------


def test_chat普通回复打印文本(monkeypatch, capsys) -> None:
    _use(monkeypatch, {("POST", "/chat/r1"): _Resp(200, {"kind": "text", "text": "你好"})})
    cli.main(["chat", "r1", "hi"])
    assert "你好" in capsys.readouterr().out


def test_chat需要审批打印工具与下一步(monkeypatch, capsys) -> None:
    _use(monkeypatch, {("POST", "/chat/r1"): _Resp(200, {
        "kind": "needs_approval",
        "approval": {"tool_name": "fs.delete", "arguments": {"path": "x"}, "reason": "高危"},
    })})
    cli.main(["chat", "r1", "删掉"])
    out = capsys.readouterr().out
    assert "需要审批" in out and "fs.delete" in out and "warden approve r1" in out


def test_chat服务返回错误_退出码1(monkeypatch, capsys) -> None:
    _use(monkeypatch, {("POST", "/chat/r1"): _Resp(500, {"detail": "kaboom"})})
    with pytest.raises(SystemExit) as e:
        cli.main(["chat", "r1", "hi"])
    assert e.value.code == 1
    assert "kaboom" in capsys.readouterr().err


def test_approvals空队列与有项目(monkeypatch, capsys) -> None:
    _use(monkeypatch, {("GET", "/approvals"): _Resp(200, [])})
    cli.main(["approvals"])
    assert "审批队列为空" in capsys.readouterr().out

    _use(monkeypatch, {("GET", "/approvals"): _Resp(200, [
        {"run_id": "r7", "tool_name": "fs.delete", "arguments": {}, "reason": "高危"},
    ])})
    cli.main(["approvals"])
    out = capsys.readouterr().out
    assert "r7" in out and "warden approve r7" in out


def test_approve与reject_打印状态(monkeypatch, capsys) -> None:
    _use(monkeypatch, {
        ("POST", "/approve/r1"): _Resp(200, {"status": "done", "text": "ok"}),
        ("POST", "/reject/r1"): _Resp(200, {"status": "rejected"}),
    })
    cli.main(["approve", "r1"])
    assert "approve 完成 -> 状态: done" in capsys.readouterr().out
    cli.main(["reject", "r1"])
    assert "reject 完成 -> 状态: rejected" in capsys.readouterr().out


def test_stream逐行打印(monkeypatch, capsys) -> None:
    _use(monkeypatch, {("POST", "/chat/stream/r1"): _Resp(
        200, lines=['{"delta":"你"}', "", '{"delta":"好"}']
    )})
    cli.main(["stream", "r1", "hi"])
    out = capsys.readouterr().out
    assert '{"delta":"你"}' in out and '{"delta":"好"}' in out


def test_health与caps打印(monkeypatch, capsys) -> None:
    _use(monkeypatch, {
        ("GET", "/health/live"): _Resp(200, text="live-ok"),
        ("GET", "/health/ready"): _Resp(200, text="ready-ok"),
        ("GET", "/capabilities"): _Resp(
            200, {"tools": ["weather.get"], "features": {"memory": True}}
        ),
    })
    cli.main(["health"])
    out = capsys.readouterr().out
    assert "live  200  live-ok" in out and "ready 200  ready-ok" in out
    cli.main(["caps"])
    assert "weather.get" in capsys.readouterr().out


def test_连接失败_退出码1(monkeypatch, capsys) -> None:
    _use(monkeypatch, {("GET", "/health/live"): httpx.ConnectError("refused")})
    with pytest.raises(SystemExit) as e:
        cli.main(["health"])
    assert e.value.code == 1
    assert "无法连接服务" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 本机库命令
# ---------------------------------------------------------------------------


def test_prune文本与json_并真的清掉过期行(tmp_path: Path, capsys) -> None:
    db = tmp_path / "w.db"
    SqliteStore(db).save_idempotent("k1", "payload")

    # TTL=0：刚写入的幂等行也算过期 → 真删
    cli.main(["prune", "--db", str(db), "--ttl-hours", "0", "--json"])
    counts = json.loads(capsys.readouterr().out)
    assert counts["idempotency"] == 1
    assert SqliteStore(db).get_idempotent("k1") is None

    # 再来一次（已空）走文本分支
    cli.main(["prune", "--db", str(db)])
    assert "清扫完成" in capsys.readouterr().out


def test_prune_pg未配置主机_明确报错(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("WARDEN_PG_HOST", raising=False)
    with pytest.raises(SystemExit) as e:
        cli.main(["prune", "--pg"])
    assert e.value.code == 1


def test_recover空库_打印无可恢复(tmp_path: Path, capsys) -> None:
    db = tmp_path / "c.db"
    SqliteStore(db).close()
    cli.main(["recover", "--db", str(db)])
    assert "没有可恢复的 run" in capsys.readouterr().out


def test_recover_json空计划(tmp_path: Path, capsys) -> None:
    db = tmp_path / "c.db"
    SqliteStore(db).close()
    cli.main(["recover", "--db", str(db), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["to_resume"] == [] and payload["terminal"] == []


def test_recover_apply空库_打印无需处理(tmp_path: Path, capsys) -> None:
    db = tmp_path / "c.db"
    SqliteStore(db).close()
    cli.main(["recover", "--db", str(db), "--apply"])
    assert "没有需要处理的 run" in capsys.readouterr().out


def test_stuck空库_无输出即退出码0(tmp_path: Path, capsys) -> None:
    db = tmp_path / "s.db"
    SqliteStore(db).close()
    cli.main(["stuck", "--db", str(db)])  # 空 → 不抛 SystemExit
    assert isinstance(capsys.readouterr().out, str)


def test_backup与restore往返(tmp_path: Path, capsys) -> None:
    src = tmp_path / "src.db"
    SqliteStore(src).save_idempotent("preserve", "v")
    backup_file = tmp_path / "copy.db"

    cli.main(["backup", str(backup_file), "--db", str(src), "--keep", "2"])
    out = capsys.readouterr().out
    assert "备份完成" in out
    assert backup_file.exists()

    target = tmp_path / "restored.db"
    cli.main(["restore", str(backup_file), "--db", str(target), "--force"])
    assert "恢复完成" in capsys.readouterr().out
    assert SqliteStore(target).get_idempotent("preserve") == "v"


def test_rotate_credentials空库_扫描0条(tmp_path: Path, capsys) -> None:
    db = tmp_path / "cred.db"
    SqliteStore(db).close()
    cli.main(["rotate-credentials", "--db", str(db), "--all-scopes"])
    assert "扫描 0 条" in capsys.readouterr().out


def _audit_store(tmp_path: Path) -> Path:
    db = tmp_path / "audit.db"
    store = SqliteAuditStore(db_path=db)
    for i in range(2):
        store.append(AuditRecord(
            correlation_id=f"c{i}", tenant_id="acme", principal_type="user",
            principal_id="alice", product_id="local", operation="QUERY",
            run_id=f"r{i}", method="GET", path=f"/status/r{i}", status=200,
            at=1_700_000_000.0 + i,
        ))
    return db


def test_audit_verify与archive往返(tmp_path: Path, capsys) -> None:
    db = _audit_store(tmp_path)
    cli.main(["audit-verify", "--db", str(db), "--json"])
    assert json.loads(capsys.readouterr().out)["ok"] is True

    archive_dir = tmp_path / "archive"
    cli.main(["audit-archive", "--db", str(db), "--dir", str(archive_dir), "--name", "a.jsonl"])
    assert "归档 2 条" in capsys.readouterr().out

    cli.main(["audit-archive-verify", "--dir", str(archive_dir)])
    assert "✅" in capsys.readouterr().out


def test_scopes_in_store_无表时安全返回空(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "empty.db")
    try:
        assert cli._scopes_in_store(store) == []  # noqa: SLF001
    finally:
        store.close()


def test_db_from_args_优先级(tmp_path: Path, monkeypatch) -> None:
    import argparse

    monkeypatch.setenv("WARDEN_DB_PATH", str(tmp_path / "env.db"))
    assert cli._db_from_args(argparse.Namespace(db="")) == str(tmp_path / "env.db")  # noqa: SLF001
    assert cli._db_from_args(argparse.Namespace(db="explicit.db")) == "explicit.db"  # noqa: SLF001
