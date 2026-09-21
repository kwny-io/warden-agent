"""PostgreSQL 实现的**静态契约检查**：不开数据库、不依赖 psycopg，也能拦住
一批"只在 PG 上才炸"的错误。

为什么需要它：
  项目声称"存储可换 PostgreSQL"，但 `tests/test_store_interface.py` 对 PG 是 `skipif` 跳过，
  CI 里也没有 PG 容器 —— 也就是说 **PostgresStore 从未被自动化验证过**。真起一个库来测是最好的
  （见 `16-成熟度评估与话术边界.md` 的推进项 2），但在那之前，下面这几类问题**可以静态判定**，
  而且它们都是"跑起来才发现"的典型：

  1. **连接必须开 `autocommit=True`**：Postgres 里事务内任一语句报错后，整个事务进入 aborted 状态，
     此后所有语句（含读、含健康探针）都会失败，直到有人 rollback。本文件写作时该文件里
     **没有任何 `rollback()`** —— 一条坏写就能把这条连接废掉且不自愈。SQLite 没有这个语义，
     所以这个坑只在 PG 上暴露。
  2. **`ON CONFLICT (列)` 要求该列上有唯一约束**：漏了主键/唯一索引，运行时报
     "there is no unique or exclusion constraint matching the ON CONFLICT specification"。
  3. **多语句写入必须走显式事务块**：否则中途失败会留下"删了一半"的会话。
  4. **占位符必须是 `%s`**（PG 风格），混进 SQLite 的 `?` 会直接语法错。
  5. **接口方法齐全**：缺一个就是运行时 AttributeError（`RunStore` 协议 + 凭证保管库协议）。

**这套检查的边界（别夸大）**：静态检查只能证明"代码写成这样"，**不能替代真跑一遍**——
SQL 语义、类型转换、并发行为仍需真实数据库验证。它拦的是"低级但不跑就发现不了"的那一类。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

POSTGRES_PY = (
    Path(__file__).resolve().parents[1] / "src" / "warden_agent" / "store" / "postgres.py"
)
SOURCE = POSTGRES_PY.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def _class_methods(class_name: str) -> dict[str, ast.FunctionDef]:
    for node in TREE.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                item.name: item
                for item in node.body
                if isinstance(item, ast.FunctionDef)
            }
    raise AssertionError(f"postgres.py 里找不到类 {class_name}")


def _ddl_blocks() -> list[str]:
    """把源码里所有 `cur.execute(\"\"\" ... \"\"\")` 的建表语句原文抽出来。"""
    return re.findall(r'cur\.execute\(\s*"""(.*?)"""', SOURCE, re.DOTALL)


# ---------- 1) autocommit ----------


def test_连接必须开autocommit避免毒丸连接() -> None:
    """不开 autocommit：一次失败写会让整条连接进入 aborted 状态，且此前没有任何 rollback()。"""
    connect_calls = [
        node for node in ast.walk(TREE)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "connect"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "psycopg"
    ]
    assert connect_calls, "没找到 psycopg.connect(...) —— 测试本身可能失效了"
    for call in connect_calls:
        keywords = {kw.arg: kw.value for kw in call.keywords}
        flag = keywords.get("autocommit")
        assert isinstance(flag, ast.Constant) and flag.value is True, (
            "PostgresStore 必须以 autocommit=True 连接。原因：PG 事务内一次报错会让整条连接进入 "
            "aborted 状态，之后所有语句（含读与健康探针）全部失败，直到有人 rollback；"
            "而本文件没有任何 rollback()，等于一条坏写就能把连接废掉且不自愈。"
        )


# ---------- 2) ON CONFLICT 必须有唯一约束 ----------


def test_每个ONCONFLICT目标列都有主键或唯一约束() -> None:
    targets = set(re.findall(r"ON CONFLICT\s*\(([^)]+)\)", SOURCE))
    assert targets, "源码里没扫描到 ON CONFLICT —— 测试本身可能失效了"

    declared: list[set[str]] = []
    for block in _ddl_blocks():
        for line in block.splitlines():
            upper = line.upper()
            if "PRIMARY KEY" not in upper and "UNIQUE" not in upper:
                continue
            inline = re.search(r"PRIMARY KEY\s*\(([^)]+)\)", line, re.IGNORECASE)
            if inline:
                declared.append({c.strip() for c in inline.group(1).split(",")})
            else:
                # 行内写法：`run_id TEXT PRIMARY KEY,`
                declared.append({line.strip().split()[0]})

    for target in sorted(targets):
        want = {column.strip() for column in target.split(",")}
        assert any(want <= have for have in declared), (
            f"ON CONFLICT ({target}) 找不到对应的主键/唯一约束，"
            f"真实 PG 上会报 no unique or exclusion constraint matching。"
            f"建表语句里已声明的约束列组合：{declared}"
        )


# ---------- 3) 多语句写入要有显式事务 ----------


@pytest.mark.parametrize("method_name", ["delete_run", "append_message"])
def test_多语句写入使用显式事务块(method_name: str) -> None:
    """autocommit 模式下没有隐式事务，多语句写入必须自己开 `conn.transaction()`。"""
    method = _class_methods("PostgresStore")[method_name]
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
    assert uses_transaction, (
        f"{method_name} 含多条写语句，必须在 `with self.conn.transaction():` 里执行，"
        "否则中途失败会留下半成品状态（会话删了一半 / 查重与插入之间被插入）"
    )


# ---------- 4) 占位符风格 ----------


def test_没有SQLite风格的问号占位符() -> None:
    """混用 `?` 在 PG 上直接语法错，且只有真跑才发现。"""
    offenders: list[str] = []
    for node in ast.walk(TREE):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        text = node.value
        upper = text.upper()
        if not any(k in upper for k in ("SELECT", "INSERT", "UPDATE")):
            continue
        if re.search(r"=\s*\?", text) or "VALUES (?, " in text:
            offenders.append(text.strip().splitlines()[0][:60])
    assert not offenders, f"发现 SQLite 风格 `?` 占位符（PG 应为 %s）：{offenders}"

    # 反向确认：确实有 %s（避免上面的检查因为"没扫到语句"而假通过）
    assert "%s" in SOURCE


# ---------- 5) 接口完整性 ----------


def test_实现了RunStore全部协议方法() -> None:
    """缺方法 = 运行时 AttributeError，这是"接口可换"的最低要求。"""
    from warden_agent.store.base import RunStore

    required = {
        name for name in dir(RunStore)
        if not name.startswith("_") and callable(getattr(RunStore, name, None))
    }
    missing = sorted(required - set(_class_methods("PostgresStore")))
    assert not missing, f"PostgresStore 缺少 RunStore 协议方法：{missing}"


def test_实现了凭证保管库协议() -> None:
    """凭证密文/租约也要能落 PG（`as_vault` 靠结构化匹配，缺方法会静默退回进程内）。"""
    from warden_agent.credential.vault import CredentialVault

    required = {
        name for name in dir(CredentialVault)
        if not name.startswith("_") and callable(getattr(CredentialVault, name, None))
    }
    missing = sorted(required - set(_class_methods("PostgresStore")))
    assert not missing, f"PostgresStore 缺少凭证保管库方法（凭证将落不了 PG）：{missing}"


def test_凭证租约表建了过期索引() -> None:
    """租约按 (scope, expires_at) 惰性清理；没索引会随租约量线性变慢。"""
    ddl = "\n".join(_ddl_blocks())
    assert "credential_leases" in ddl
    assert "idx_credential_leases_expiry" in SOURCE
