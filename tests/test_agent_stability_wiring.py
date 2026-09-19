"""验证工具稳定性层真的接进了产品路径（而不是"只有实现 + 测试、没人用"）。

背景：这块此前是**实现了但没接线** —— `exec_tool` 与 `AgentSession` 都接受 `stability`，
但 `build_agent` / `build_app` 从没构造或传过它，全仓只有一句注释提到它。
所以"每次工具调用必经的管卡"这个说法当时是不成立的。

这组测试钉住四件事：
  1. `build_stability_executor` 把多种写法归一（None / False / True / Config / Executor）；
  2. 开了之后，**纯工具**的瞬时失败会被自动重试（SDK 路径）；
  3. **非纯工具**的逻辑错误**不**重试 —— 否则等于把有副作用的操作重放（比如删两次）；
  4. **产品路径（HTTP）** 真的走到了稳定性层 —— 这条最关键。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from tests.conftest import ScriptedModel

from warden_agent.agent import build_agent
from warden_agent.model.model import ChatResponse, ToolCall
from warden_agent.policy.policy import PolicyEngine
from warden_agent.store.sqlite import SqliteStore
from warden_agent.tool.catalog import ToolCatalog, ToolSpec, function_tool
from warden_agent.tool.stability import (
    DEFAULT_STABILITY_CONFIG,
    StabilityConfig,
    StableToolExecutor,
    build_stability_executor,
)
from warden_agent.web.server import build_app


def _flaky(*, pure: bool, exc: BaseException, seen: list[int]) -> ToolSpec:
    """首次调用必失败的工具。`seen` 记录实际被调用了几次。"""

    @function_tool(
        "flaky.run",
        "首次调用必失败的工具（仅测试用）",
        {"type": "object", "properties": {}, "required": []},
        pure=pure,
    )
    def flaky() -> str:
        seen.append(1)
        if len(seen) == 1:
            raise exc
        return "工具返回 ok"

    return flaky


def _script() -> list[ChatResponse]:
    """一次工具调用 + 一句最终回答。

    两轮就够：工具失败时错误会在同一轮被喂回，下一轮模型直接给结论。
    """
    return [
        ChatResponse(
            content=None, finish_reason="tool_calls",
            tool_calls=[ToolCall(id="1", name="flaky.run", arguments={})],
        ),
        ChatResponse(content="完成了", finish_reason="stop"),
    ]


# ---------- 解析器 ----------


def test_解析器把多种写法归一() -> None:
    assert build_stability_executor(None) is None
    assert build_stability_executor(False) is None

    enabled = build_stability_executor(True)
    assert enabled is not None
    assert enabled.config is DEFAULT_STABILITY_CONFIG  # 用的是那套保守默认值

    cfg = StabilityConfig(timeout_seconds=1.0)
    resolved = build_stability_executor(cfg)
    assert resolved is not None
    assert resolved.config is cfg

    executor = StableToolExecutor(cfg)
    assert build_stability_executor(executor) is executor  # 测试替身原样透传


# ---------- SDK 路径 ----------


def test_默认不传stability_行为与以前完全一致() -> None:
    seen: list[int] = []
    agent = build_agent(
        provider=ScriptedModel(_script()),
        tools=[_flaky(pure=True, exc=OSError("瞬时故障"), seen=seen)],
    )
    agent.chat("跑一下")
    assert len(seen) == 1, "默认（不传 stability）不应开启重试"


def test_开启后纯工具瞬时失败会被重试() -> None:
    seen: list[int] = []
    agent = build_agent(
        provider=ScriptedModel(_script()),
        tools=[_flaky(pure=True, exc=OSError("瞬时网络故障"), seen=seen)],
        stability=True,
    )
    agent.chat("跑一下")
    assert len(seen) == 2, "OSError 属瞬时错误且工具为纯 → 应重试一次"


def test_非纯工具的逻辑错误不重试_避免重放副作用() -> None:
    """这条是稳定性层的安全底线：`fs.delete` 这类操作绝不能被重放。"""
    seen: list[int] = []
    agent = build_agent(
        provider=ScriptedModel(_script()),
        tools=[_flaky(pure=False, exc=ValueError("参数不合法"), seen=seen)],
        stability=True,
    )
    agent.chat("跑一下")
    assert len(seen) == 1, "非纯工具 + 逻辑错误 → 不重试"


# ---------- 产品路径（HTTP） ----------


@pytest.mark.asyncio
async def test_产品路径_HTTP_也走到了稳定性层(tmp_path: Path) -> None:
    """最关键的一条：证明"接线"接的是产品路径，而不只是 SDK 门面。"""
    seen: list[int] = []
    catalog = ToolCatalog()
    catalog.register(_flaky(pure=True, exc=OSError("瞬时故障"), seen=seen))

    app = build_app(
        model=ScriptedModel(_script()),
        catalog=catalog,
        policy=PolicyEngine(),
        store=SqliteStore(str(tmp_path / "stability.db")),
        stability=True,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/chat/run-stab", json={"text": "跑一下"})

    assert r.status_code == 200, r.text
    assert len(seen) == 2, "产品路径上，纯工具的瞬时失败也应被重试"
