"""存储 codec 一致性回归：SQLite / PostgreSQL 必须共用同一套版本化编解码助手。

背景（跨后端互操作缺口）：SQLite 一直把 tool_call / 待审批 arguments / checkpoint
写成 `v{n}:{json}`（带版本前缀），而 PostgreSQL 直接 `json.dumps`/`json.loads`——
既没有版本前缀，也不认版本。两端换用时数据格式对不上、版本契约静默丢失。

本文件锁定：
  - 共享助手产出的格式与历史 SQLite 逐字节一致（老行必须还能读回）；
  - 未知版本（如 v2）解码抛 KeyError，由调用方兜底；
  - SQLite load_messages 遇到 v2 行只跳过该行、不拖垮整次加载；
  - PostgreSQL 源码确实调用了同一套助手（静态/AST 检查，PG 本地跑不了，
    沿用 tests/test_postgres_contract.py 的既有套路）。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from warden_agent.model.model import Message, ToolCall
from warden_agent.store.codec import (
    DEFAULT_CODEC_REGISTRY,
    decode_versioned,
    encode_versioned,
)
from warden_agent.store.sqlite import SqliteStore

POSTGRES_PY = (
    Path(__file__).resolve().parents[1] / "src" / "warden_agent" / "store" / "postgres.py"
)


# ---------- 共享助手：格式与兼容性 ----------


def test_编码助手产出与历史格式逐字节一致() -> None:
    """老 SQLite 行是 `v1:` + `json.dumps(..., ensure_ascii=False)`，必须逐字节不变。"""
    payload = {"id": "c1", "name": "fs.read", "arguments": {"path": "/x", "n": 1}}
    assert encode_versioned(payload) == "v1:" + json.dumps(payload, ensure_ascii=False)
    assert decode_versioned(encode_versioned(payload)) == payload


def test_编码助手保持非ASCII不转义() -> None:
    assert encode_versioned({"text": "你好"}) == 'v1:{"text": "你好"}'


def test_解码助手兼容无前缀历史数据() -> None:
    """老数据（无 `v:` 前缀）按 v1 JSON 兜底，历史行必须还能读。"""
    assert decode_versioned('{"a": 1}') == {"a": 1}


def test_未知版本解码抛KeyError() -> None:
    with pytest.raises(KeyError):
        decode_versioned("v2:{}")
    with pytest.raises(KeyError):
        DEFAULT_CODEC_REGISTRY.decode(2, "{}")


# ---------- SQLite：坏版本行不拖垮整次加载（P2） ----------


def test_load_messages遇到未知版本行只跳过该行(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "t.db")
    store.append_message("run-1", Message(role="user", content="你好"))
    store.append_message(
        "run-1",
        Message(
            role="assistant",
            content="调工具",
            tool_call=ToolCall(id="c1", name="fs.read", arguments={"path": "/x"}),
        ),
    )
    # 手写一条未来版本（v2）的行：解码器未注册 v2 → 旧代码在这里抛 KeyError、整次 load 崩
    store.conn.execute(
        "INSERT INTO messages (run_id, role, content, tool_call) VALUES (?, ?, ?, ?)",
        ("run-1", "assistant", "未来版本", "v2:{}"),
    )
    store.conn.commit()
    try:
        msgs = store.load_messages("run-1")
        assert len(msgs) == 3                       # 坏行不拖垮其余
        assert msgs[1].tool_call is not None        # 正常行仍解出工具调用
        assert msgs[2].tool_call is None            # 未知版本降级为无工具调用
    finally:
        store.close()


def test_待审批与checkpoint也走版本化助手(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "t.db")
    try:
        store.save_pending_approval("r1", "a1", "fs.delete", {"path": "/x"}, "需批准")
        raw = store.conn.execute(
            "SELECT arguments FROM pending_approvals WHERE run_id = 'r1'"
        ).fetchone()[0]
        assert raw.startswith("v1:")
        assert store.load_pending_approval("r1")[2] == {"path": "/x"}
    finally:
        store.close()


# ---------- PostgreSQL：静态契约（本地跑不了真库） ----------


def _method_name_calls(class_name: str, method_name: str) -> set[str]:
    """返回某方法的 AST 里所有 `Name(...)` 调用名（只看普通函数名）。"""
    tree = ast.parse(POSTGRES_PY.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == method_name:
                    return {
                        n.func.id
                        for n in ast.walk(item)
                        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    }
    raise AssertionError(f"postgres.py 里找不到 {class_name}.{method_name}")


def test_postgres使用共享编解码助手() -> None:
    """PG 的三种 payload 都必须调用同一套助手，而不是自己 raw json.dumps/loads。"""
    methods = (
        "append_message", "load_messages",
        "save_pending_approval", "load_pending_approval",
        "save_checkpoint", "_decode_checkpoint",
    )
    for method in methods:
        calls = _method_name_calls("PostgresStore", method)
        assert calls & {"encode_versioned", "decode_versioned"}, (
            f"PostgresStore.{method} 没有调用共享编解码助手，跨后端格式会再次分叉"
        )


def test_postgres的append_event在显式事务块内() -> None:
    """INSERT + 裁剪必须同生共死（autocommit 下两语句会留下可见的半成品）。"""
    method = next(
        item
        for node in ast.parse(POSTGRES_PY.read_text(encoding="utf-8")).body
        if isinstance(node, ast.ClassDef) and node.name == "PostgresStore"
        for item in node.body
        if isinstance(item, ast.FunctionDef) and item.name == "append_event"
    )
    uses_transaction = any(
        isinstance(node, ast.With)
        and any(
            isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Attribute)
            and item.context_expr.func.attr == "transaction"
            for item in node.items
        )
        for node in ast.walk(method)
    )
    assert uses_transaction, "append_event 的 INSERT+DELETE 必须放进 conn.transaction()"
