"""e2e 多副本用例用的 PostgreSQL 小工具：探活 / 建库 / 删库。

**为什么单独一个脚本**：多副本用例在 Node/Playwright 侧（`web/e2e/multi-replica.spec.ts`），
而"给多副本准备一个干净的空库"这件事只有 Python 侧能干净地做（项目本来就依赖 psycopg）。
与其在 spec 里塞一段 `python -c`，不如做成一条可独立运行、可单独验证的命令：

    python scripts/e2e_pg_admin.py probe
    python scripts/e2e_pg_admin.py create warden_e2e_1730000000
    python scripts/e2e_pg_admin.py drop   warden_e2e_1730000000

**为什么必须"每次运行新建一个空库"**：多副本共享的是同一个库，而库里有**跨请求累积**的状态，
最典型的是限流计数（键是来源 IP / 调用者，窗口 60 秒）与幂等表：

  · 上一次运行留下的限流计数，会让下一次运行**第一个**请求就是 429，红在一个看不懂的地方；
  · 上一次运行留下的幂等记录，会让"同 key 应该重新执行"的对照用例假绿。

连接参数沿用 pytest 的 PG 约定（见 `tests/test_postgres_integration.py` 顶部注释），
所以本机容器与 CI 的 service container 用的是同一套环境变量：

    WARDEN_TEST_PG_HOST / WARDEN_TEST_PG_PORT / WARDEN_TEST_PG_DB
    WARDEN_TEST_PG_USER / WARDEN_TEST_PG_PASSWORD

退出码：0 = 成功；**2 = 没有可用的 PostgreSQL**（e2e 据此整体 skip，与 pytest 的 skipif 同构）；
1 = 其他错误（参数不合法、权限不足等）。把"没有库"与"库出错"分开，是因为它们对使用者的
含义完全不同：前者是环境没准备好，后者是环境有问题——都用同一个退出码会把后者藏起来。
"""

from __future__ import annotations

import os
import re
import sys

# 库名会被拼进 DDL（DDL 不接受参数占位符），所以**先严格校验再拼**：
# 只允许"字母开头 + 小写字母/数字/下划线 + 不超过 63 字节"（PostgreSQL 标识符上限）。
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

# 维护库：建库/删库要连一个已存在的库，惯例用 postgres
_MAINTENANCE_DB = "postgres"


def _params() -> dict[str, object]:
    """连接参数：默认对齐本机容器（localhost:5432，用户 postgres，空密码）。"""
    return {
        "host": os.environ.get("WARDEN_TEST_PG_HOST", "localhost"),
        "port": int(os.environ.get("WARDEN_TEST_PG_PORT", "5432")),
        "user": os.environ.get("WARDEN_TEST_PG_USER", "postgres"),
        "password": os.environ.get("WARDEN_TEST_PG_PASSWORD", ""),
    }


def _connect(dbname: str):
    """连上指定库。`autocommit=True`：CREATE/DROP DATABASE 不能在事务块里执行。"""
    import psycopg

    return psycopg.connect(dbname=dbname, autocommit=True, connect_timeout=3, **_params())  # type: ignore[arg-type]


def _probe() -> int:
    """只验证"能不能连上配置的那个库"（与 pytest 的 `_pg_available` 同一个判据）。"""
    dbname = os.environ.get("WARDEN_TEST_PG_DB", "warden")
    try:
        conn = _connect(dbname)
    except Exception as e:  # noqa: BLE001 - 任何失败都算"没有可用的库"
        print(f"连不上 {dbname}：{e}", file=sys.stderr)
        return 2
    conn.close()
    print("ok")
    return 0


def _create(name: str) -> int:
    if not _NAME_RE.match(name):
        print(
            f"库名不合法：{name!r}（只允许小写字母开头、字母数字下划线、不超过 63 字符）",
            file=sys.stderr,
        )
        return 1
    conn = _connect(_MAINTENANCE_DB)
    try:
        # 先 DROP 再 CREATE：万一同名库还在（上一次跑崩了、或时间戳撞了），
        # 也要保证**建出来的库一定是空的**——"可能不空"会让用例的结论不可信。
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        conn.close()
    print(name)
    return 0


def _drop(name: str) -> int:
    if not _NAME_RE.match(name):
        print(f"库名不合法：{name!r}", file=sys.stderr)
        return 1
    conn = _connect(_MAINTENANCE_DB)
    try:
        # WITH (FORCE)（PostgreSQL 13+）：副本进程可能还没完全释放连接，
        # 普通 DROP 会以 "database is being accessed by other users" 失败——
        # 那会让"跑完清理"变成永远留一个库在 CI 的 PG 里。
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        conn.close()
    print("ok")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        print("用法：e2e_pg_admin.py probe | create <name> | drop <name>", file=sys.stderr)
        return 1
    action = argv[1]
    try:
        if action == "probe":
            return _probe()
        if action in ("create", "drop"):
            if len(argv) < 3:
                print(f"{action} 需要一个库名参数", file=sys.stderr)
                return 1
            return _create(argv[2]) if action == "create" else _drop(argv[2])
    except Exception as e:  # noqa: BLE001 - 统一转成退出码 1，错误原文照打（别吞掉原因）
        print(f"{action} 失败：{e}", file=sys.stderr)
        return 1
    print(f"未知动作：{action}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
