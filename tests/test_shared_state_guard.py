"""单机存储 (SQLite) 配上多副本协调开关 ⇒ 必须起不来。

这条约束文档里一直有（`docs/deployment-boundaries.md:101`、`deploy/k8s/README.md:16`），
但代码从不拦——于是它属于最难查的一类配置事故：**文档禁止、代码放行、运行时几乎正常**。

打开 `WARDEN_SHARED_STATE=1` 后，`coordination_for` 会老老实实把幂等/事件流/限流计数
放进 sqlite（因为 SqliteStore 确实有那三个方法），看起来"全都配对了"。但：
  · `SqliteStore.hit_rate_limit` 是读-改-写（只靠进程内锁）⇒ 跨进程**限流计数会丢**，无报错；
  · 写竞争抛 `database is locked` ⇒ 随机 500，不是可解释的失败。

判据要同时钉住三件事：该拒的拒、不该拒的放行（反向对照）、真入口也拦得住。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from warden_agent.store.sqlite import SqliteStore
from warden_agent.web.run_server import (
    SharedStateRefusedError,
    ensure_shared_state_matches_backend,
)


def _sqlite() -> SqliteStore:
    return SqliteStore(Path(tempfile.mkdtemp()) / "guard.db")


def test_sqlite配上多副本开关_拒绝启动() -> None:
    store = _sqlite()
    with pytest.raises(SharedStateRefusedError) as ei:
        ensure_shared_state_matches_backend(store, {"WARDEN_SHARED_STATE": "1"})
    msg = str(ei.value)
    # 报错必须能指路：既说为什么，也给出两条出路。
    assert "WARDEN_PG_HOST" in msg
    assert "限流" in msg
    store.close()


def test_反向对照_sqlite但单副本_放行() -> None:
    """没开共享状态时不能在 sqlite 上误报——那会禁止掉最常用的部署方式。"""
    store = _sqlite()
    ensure_shared_state_matches_backend(store, {})
    ensure_shared_state_matches_backend(store, {"WARDEN_SHARED_STATE": "0"})
    store.close()


def test_反向对照_共享开关取值写法都要认() -> None:
    """`true/on/yes/1` 都算开——同一个变量不许有两种口径（与 env_bool 一致）。"""
    store = _sqlite()
    for value in ("1", "true", "on", "yes", "TRUE"):
        with pytest.raises(SharedStateRefusedError):
            ensure_shared_state_matches_backend(store, {"WARDEN_SHARED_STATE": value})
    store.close()


def test_共享开关_非_sqlite_存储放行() -> None:
    """PostgreSQL 后端（或其他任何存储）就是多副本该用的那个，不许拦。"""

    class _NotSqlite:
        pass

    ensure_shared_state_matches_backend(_NotSqlite(), {"WARDEN_SHARED_STATE": "1"})
