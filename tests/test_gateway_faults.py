"""网关「错误路径」自身的健壮性 —— 错误处理器不能再引发第二个错误。

## 背景（真实缺陷，被审计连带发现）

`_gateway` 的兜底 `except Exception` 里要释放幂等占位，用的是 `idem_reserved`：

    except Exception as exc:
        ...
        if idem_reserved and idem_key:
            idem_store.release(idem_key)

但 `idem_reserved = False` 这条赋值**写在 try 内部很靠后的位置**（在认证、授权、
限流、body 上限判定之后）。于是只要异常发生在认证/授权阶段：

    AttributeError → except 块执行到 `if idem_reserved` → UnboundLocalError

即：**本应返回「干净的 500 problem+json」的兜底处理器自己崩了**，客户端拿到的是
未捕获异常（栈直接冒到 ASGI 层）。后果不止是错误响应不体面：
- `status_code = 500` / `_problem(...)` 都没执行 → `finally` 里按旧值记账，
  审计与指标上这次故障可能是"200"，可观测面直接说谎；
- 幂等占位没人释放。

修复：把 `idem_reserved = False` 提到 `try` **之前**（Python 的 `and` 短路保证
`idem_key` / `idem_store` 在后半段不会被求值，所以初始化一个变量足够）。

本文件同时守住「admin 跨归属审批」那条路径——它和这个缺陷是一起暴露的：
那里曾有 `permission_only.authorize(...)`，而 `permission_authorizer()` 返回的是
裸回调（`AuthorizeFn`），没有 `.authorize()` 方法，于是那条路径必然 AttributeError，
正好落进上面那个双错陷阱（见 tests/test_approval_separation.py 的 happy path）。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.conftest import ScriptedModel, weather_tool

from warden_agent.core.metrics import metrics
from warden_agent.policy.policy import PolicyEngine
from warden_agent.store.sqlite import SqliteStore
from warden_agent.web.auth import TrustedCaller
from warden_agent.web.server import build_app

CALLERS = {"k-alice": TrustedCaller("tenant-a", "user", "alice")}
A_ALICE = {"Authorization": "Bearer k-alice"}


class _BoomLoadRun(SqliteStore):
    """读 Run 时炸掉——用来在**认证/授权阶段**触发异常（而非端点内部）。

    授权器 `owner_authorizer` 的 `load_owner` 回调就是 `_gateway._owner_of`，
    它第一件事就是 `store.load_run(run_id)`。所以这条读路径是"异常发生在
    try 内很早的位置"的最小可靠触发器。
    """

    def load_run(self, run_id: str) -> Any:  # type: ignore[override]
        raise RuntimeError("load_run boom")


def _store(name: str) -> _BoomLoadRun:
    """建一个「读 Run 即炸」的库——异常因此落在认证/授权阶段（idem_reserved 未赋值时）。"""
    return _BoomLoadRun(Path(tempfile.mkdtemp()) / name)


def _app(store: SqliteStore):
    return build_app(
        model=ScriptedModel([]),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=store,
        api_keys=CALLERS,
    )


@pytest.mark.asyncio
async def test_认证阶段异常仍返回干净的500() -> None:
    """异常发生在授权阶段（idem_reserved 尚未赋值）时，必须是 500 而不是 UnboundLocalError。"""
    store = _store("boom.db")
    app = _app(store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # raise_app_exceptions 默认 True：若兜底块自己抛 UnboundLocalError，
        # 这里就会直接炸出异常，测试失败——这正是我们要锁住的回归。
        r = await client.get("/status/run-1", headers=A_ALICE)

    assert r.status_code == 500
    body = r.json()
    assert body["errorCode"] == "INTERNAL_ERROR"
    # 不泄漏内部细节：不把 "load_run boom" 这种内部异常文本回给调用方
    assert "boom" not in r.text
    # correlation_id 必须同时在响应头与响应体里（用户报障时运维靠它对上日志）
    assert r.headers["X-Correlation-Id"] == body["correlationId"]
    store.close()


@pytest.mark.asyncio
async def test_兜底500不会被记成200() -> None:
    """可观测面不能对故障说谎：这次请求必须落进 5xx 计数器，而不是 200。"""
    inner = _store("boom2.db")
    app = _app(inner)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/status/run-2", headers=A_ALICE)).status_code == 500

    rendered = metrics().render()
    # 异常发生在路由匹配之前 → path 标签回落固定串 "other"（有界，基数安全）
    assert 'warden_http_errors_total{method="GET",path="other"}' in rendered
    inner.close()


@pytest.mark.asyncio
async def test_未认证请求不触发归属校验从而不炸() -> None:
    """同一份坏 store：没带凭证时根本不进授权分支（caller is None），应当正常返回 401。"""
    inner = _store("boom3.db")
    app = _app(inner)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/status/run-3")).status_code == 401
    inner.close()
