"""子进程环境姿态的**类守卫**：每个派生点都必须显式选一种环境。

为什么需要这一条（真实缺口，不是假想）：
  `execution/child_env.py` 定义了两种姿态 ——
    ① `minimal_child_env()`：最小白名单（模型能影响的子进程）
    ② `env_without_warden()`：继承但摘掉 `WARDEN_*`（操作者装配的外部组件）
  `tests/test_child_env.py` 也把这两个函数本身逐条验穿了。**但它测的是"机制"，
  没有任何用例检查"派生点有没有真的用这两个机制"**。于是这三处长期存在：

    · `runtime/backup.py`  `env = dict(os.environ)`  → `pg_dump` 拿到全部 `WARDEN_*` 密钥
    · `runtime/backup.py`  `pg_restore --list` 不传 `env=` → 整包继承宿主环境
    · `execution/sandbox.py` `unshare` 探测不传 `env=` → 同上

  在不传 `env=` 的语义下，`subprocess` 会把父进程环境**整包**给子进程；所以判据不能
  只测那两个函数，必须对**每一个派生点**成立。本文件就是那条判据。

判据说明：
  · 对"读法"不敏感：扫 AST 找派生调用，而不是 grep 某几种写法；
  · 允许 `env=` 经**一次局部变量转手**（`env = env_without_warden()` 后 `env=env`），
    否则守卫会把人逼成"为了过守卫把函数调用内联"，那是反向激励；
  · 末尾三条**反向对照**证明这条守卫不是永真：不传 `env=`、`dict(os.environ)`、
    `os.system` 三种写法必须被它抓出来。
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "warden_agent"

# 被认可的环境姿态函数（`execution/child_env.py` 里那两个）。
APPROVED_POSTURES: frozenset[str] = frozenset({"minimal_child_env", "env_without_warden"})

# 派生子进程的调用名（`subprocess.*` 与 `os.*` 两族）。
SPAWN_ATTRS: frozenset[str] = frozenset({
    "run", "Popen", "check_output", "check_call", "call",
    "system", "popen",
    "spawn", "spawnv", "spawnvp", "spawnve", "execv", "execvp", "execve",
})

# 扫描范围的下限（**防止"守卫悄悄失明还报绿"**）：今天有 6 个派生点，
# 这里只要求"至少找到这些模块里的点"，不锁死具体数量。
MUST_BE_SCANNED: frozenset[str] = frozenset({
    "execution/broker.py",
    "execution/sandbox.py",
    "git/revision.py",
    "mcp/client.py",
    "runtime/backup.py",
})


def _dotted(node: ast.AST) -> str | None:
    """把调用目标还原成 `subprocess.run` / `os.system` 这样的点号名。"""
    parts: list[str] = []
    cur: ast.AST | None = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return None


def _approved_posture(node: ast.AST) -> str | None:
    """这个表达式是不是"选择了一种被认可的环境姿态"。"""
    if not isinstance(node, ast.Call):
        return None
    name = _dotted(node.func)
    if name is None:
        return None
    short = name.split(".")[-1]
    return short if short in APPROVED_POSTURES else None


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    mapping: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            mapping[child] = parent
    return mapping


def _enclosing_scope(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST | None:
    """最近的函数作用域（没有就是模块级）。"""
    cur = parents.get(node)
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur
        cur = parents.get(cur)
    return None


def _assigned_values(scope: ast.AST | None, name: str) -> list[ast.expr]:
    """在作用域里找出 `name = ...` 的右值（用于把 `env=env` 还原回源头）。"""
    if scope is None:
        return []
    values: list[ast.expr] = []
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.expr):
            if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                values.append(node.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and isinstance(node.value, ast.expr)
        ):
            values.append(node.value)
    return values


def _env_kwang(call: ast.Call) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == "env":
            return kw.value
    return None


def _judge(call: ast.Call, scope: ast.AST | None) -> tuple[bool, str]:
    """判定一次派生：返回 (是否合规, 说明)。"""
    value = _env_kwang(call)
    if value is None:
        return False, "没传 `env=` —— `subprocess` 会把宿主环境整包交给子进程"
    posture = _approved_posture(value)
    if posture is not None:
        return True, f"env={posture}()"
    if isinstance(value, ast.Name):
        sources = _assigned_values(scope, value.id)
        if len(sources) == 1:
            inner = _approved_posture(sources[0])
            if inner is not None:
                return True, f"env={value.id} ← {inner}()"
            return False, f"env={value.id} ← `{ast.unparse(sources[0])[:70]}`（不是被认可的姿态）"
        if not sources:
            return False, f"env={value.id}（作用域里找不到它的来源）"
        return False, f"env={value.id}（作用域里有 {len(sources)} 处赋值，判不清）"
    return False, f"env=`{ast.unparse(value)[:70]}`（不是被认可的姿态）"


def audit_source(source: str, rel: str = "<内存>") -> list[str]:
    """审计一段源码，返回违规描述列表（空列表 = 全合规）。"""
    tree = ast.parse(source)
    parents = _parents(tree)
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _dotted(node.func)
        if name is None:
            continue
        module, _, attr = name.rpartition(".")
        if module not in {"subprocess", "os"} or attr not in SPAWN_ATTRS:
            continue
        ok, detail = _judge(node, _enclosing_scope(node, parents))
        if not ok:
            violations.append(f"{rel}:{node.lineno} {name} —— {detail}")
    return violations


def audit_tree() -> tuple[list[str], set[str]]:
    """审计整个 `src/warden_agent`，返回 (违规列表, 扫到的模块集合)。"""
    violations: list[str] = []
    scanned: set[str] = set()
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        source = path.read_text(encoding="utf-8")
        found = audit_source(source, rel)
        if "subprocess" in source or "os.system" in source or "os.popen" in source:
            scanned.add(rel)
        violations.extend(found)
    return violations, scanned


# ---------------------------------------------------------------- 主判据


def test_每个子进程派生点都用声明的环境姿态() -> None:
    """**这条就是缺口本身**：派生点必须把 `env=` 指到两种姿态之一。

    失败时按下面的信息改：在调用处加 `env=minimal_child_env()`（模型能影响的子进程）
    或 `env=env_without_warden()`（操作者装配的外部组件，如 pg_dump / pg_restore），
    并在旁边写清楚为什么选这一种。
    """
    violations, _ = audit_tree()
    assert not violations, (
        "以下子进程派生点没有选择声明的环境姿态（会把宿主环境整包交给子进程）：\n"
        + "\n".join(f"  - {v}" for v in violations)
        + "\n修法：加 `env=minimal_child_env()` 或 `env=env_without_warden()`，"
        "并写清楚为什么选这一种（见 execution/child_env.py 顶部两种姿态的说明）。"
    )


def test_扫描范围没有悄悄失明() -> None:
    """反向对照：万一 SRC 路径写错/目录改名，上面那条会**空跑还报绿**。

    所以这里要求"确实扫到了那几个含派生点的模块"——守卫失明必须表现为失败。
    """
    _, scanned = audit_tree()
    missing = sorted(MUST_BE_SCANNED - scanned)
    assert not missing, f"这些模块本该被扫到却没有：{missing}（SRC={SRC}）"


# ------------------------------------------------- 反向对照（证明判据不是永真）


def test_抓出不传env的派生() -> None:
    source = "import subprocess\nsubprocess.run(['echo', 'hi'])\n"
    assert audit_source(source), "不传 env= 的派生点必须被抓出来"


def test_抓出整包继承宿主环境的派生() -> None:
    source = (
        "import os\n"
        "import subprocess\n"
        "def go(cmd):\n"
        "    env = dict(os.environ)\n"
        "    subprocess.run(cmd, env=env)\n"
    )
    violations = audit_source(source)
    assert violations and "dict(os.environ)" in violations[0], violations


def test_抓出os模块的派生() -> None:
    source = "import os\nos.system('echo hi')\n"
    assert audit_source(source), "os.system / os.popen 这一族也必须覆盖"


def test_接受经局部变量转手的合规姿态() -> None:
    """反向对照的另一半：合规写法不能被误报（否则守卫会逼人把调用内联）。"""
    source = (
        "import subprocess\n"
        "from warden_agent.execution.child_env import env_without_warden\n"
        "def go(cmd):\n"
        "    env = env_without_warden()\n"
        "    subprocess.run(cmd, env=env)\n"
    )
    assert audit_source(source) == [], audit_source(source)


def test_接受直接传姿态函数的写法() -> None:
    source = (
        "import subprocess\n"
        "from warden_agent.execution.child_env import minimal_child_env\n"
        "subprocess.run(['echo'], env=minimal_child_env())\n"
    )
    assert audit_source(source) == [], audit_source(source)


# --------------------------------------------- 真调用点：跑起来时环境里到底有什么
#
# AST 判据看的是"用了哪个函数"，下面两条看的是"真跑起来时交给子进程的环境里有什么"。
# 两者互相独立 —— 一条说谎，另一条能抓住。


def test_pg_dump与pg_restore都拿不到WARDEN密钥且保留PG配置(monkeypatch, tmp_path) -> None:
    """真调用点验一遍：`backup_postgres` 两个派生点都不得把 `WARDEN_*` 交出去。

    同时反向锁住姿态选择：`PGSSLMODE` 这类**操作者的连接配置**必须留下
    （这正是用 `env_without_warden()` 而不是最小白名单的理由 —— 后者会把
    `PGSSLMODE`/`PGSSLROOTCERT`/`PGPASSFILE` 一并丢掉，静默改变连哪个库/用哪张证书）。
    """
    from warden_agent.runtime import backup as backup_mod

    monkeypatch.setenv("WARDEN_API_KEY", "svc-secret")
    monkeypatch.setenv("WARDEN_CREDENTIAL_KEY", "master-key")
    monkeypatch.setenv("PGSSLMODE", "require")

    seen: list[dict[str, str]] = []

    class _Proc:
        returncode = 0
        stdout = b""
        stderr = b""

    def fake_run(cmd: list[str], **kwargs: object) -> _Proc:
        seen.append(dict(kwargs.get("env") or {}))  # type: ignore[arg-type]
        if "-f" in cmd:  # pg_dump 的产物路径：让后续的完整性校验有东西可看
            Path(cmd[cmd.index("-f") + 1]).write_bytes(b"fake-dump")
        return _Proc()

    monkeypatch.setattr(backup_mod.shutil, "which", lambda _name: "C:/fake/pg_dump")
    monkeypatch.setattr(backup_mod.subprocess, "run", fake_run)

    result = backup_mod.backup_postgres(
        {"dbname": "warden", "password": "pw"}, tmp_path / "x.dump"
    )

    assert len(seen) >= 2, f"pg_dump 与 pg_restore 两次派生都该被看见：{seen}"
    for env in seen:
        assert "WARDEN_API_KEY" not in env, env
        assert "WARDEN_CREDENTIAL_KEY" not in env, env
        assert env.get("PGSSLMODE") == "require", "操作者的 PG* 配置必须留给 pg_dump"
    assert seen[0].get("PGPASSWORD") == "pw", "口令走环境变量（不能进命令行）"
    assert result["bytes"] == len(b"fake-dump")


def test_沙箱探测子进程拿不到WARDEN密钥(monkeypatch) -> None:
    """`_probe_namespace` 真跑一个 `unshare` 探测：它也不需要 `WARDEN_*`。"""
    from warden_agent.execution import sandbox

    monkeypatch.setenv("WARDEN_API_KEY", "svc-secret")
    captured: dict[str, dict[str, str]] = {}

    class _Proc:
        returncode = 0

    def fake_run(cmd: list[str], **kwargs: object) -> _Proc:
        captured["env"] = dict(kwargs.get("env") or {})  # type: ignore[arg-type]
        return _Proc()

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)

    assert sandbox._probe_namespace(["unshare", "-rn"]) is True
    env = captured["env"]
    assert "WARDEN_API_KEY" not in env, env
    assert any(k.upper() == "PATH" for k in env), f"探测也要能找得到可执行文件：{env}"
