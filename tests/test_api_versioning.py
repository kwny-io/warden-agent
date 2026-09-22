"""API 版本协商与弃用政策的**可执行部分**。

政策写在 `docs/api-versioning.md`；这里只守"机器能判定"的那几条：
  1. 所有响应都带 `X-Warden-Api-Version`（客户端据此知道服务端实际契约版本）；
  2. 客户端声明的主版本受支持 → 正常处理；不受支持 / 形态不对 → **400**，不"尽力处理"；
  3. 不带该头 → 按当前版本处理（最常见的调用方式不能被搞坏）。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import httpx
import pytest
from tests.conftest import ScriptedModel, weather_tool

from warden_agent.model.model import ChatResponse
from warden_agent.policy.policy import PolicyEngine
from warden_agent.store.sqlite import SqliteStore
from warden_agent.web.server import API_SUPPORTED_MAJORS, API_VERSION, _split_version, build_app


def _client(replies: int = 1) -> httpx.AsyncClient:
    app = build_app(
        model=ScriptedModel(
            [ChatResponse(content="好", finish_reason="stop") for _ in range(replies)]
        ),
        catalog=weather_tool(),
        policy=PolicyEngine(),
        store=SqliteStore(Path(tempfile.mkdtemp()) / "t.db"),
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# ---------- 版本号解析 ----------


def test_版本号解析() -> None:
    assert _split_version("1.0") == ("1", "0")
    assert _split_version(" 2.13 ") == ("2", "13")
    for bad in ("1", "1.2.3", "v1.0", "1.x", "", "abc"):
        assert _split_version(bad) is None, bad


# ---------- 协商行为 ----------


@pytest.mark.asyncio
async def test_不带版本头_按当前版本处理() -> None:
    async with _client() as c:
        r = await c.get("/health/live")
        assert r.status_code == 200
        assert r.headers["X-Warden-Api-Version"] == API_VERSION


@pytest.mark.asyncio
async def test_声明受支持的主版本_正常处理() -> None:
    # 脚本模型每被调用一次消耗一条回复，所以给 3 条（下面三个声明各驱动一次）。
    async with _client(replies=3) as c:
        for i, declared in enumerate((API_VERSION, "1.0", "1.99")):
            r = await c.post(f"/chat/run-v{i}", json={"text": "hi"},
                             headers={"X-Warden-Api-Version": declared})
            assert r.status_code == 200, declared  # 次要版本差异不拒绝


@pytest.mark.asyncio
async def test_声明不支持的主版本_明确报400() -> None:
    """关键：**不装作没事继续处理**——那会让调用方用旧假设读语义已变的响应。"""
    async with _client() as c:
        r = await c.post("/chat/run-v2", json={"text": "hi"},
                         headers={"X-Warden-Api-Version": "2.0"})
        assert r.status_code == 400
        body = r.json()
        assert "UNSUPPORTED_API_VERSION" in str(body)
        assert "2.0" in str(body)


@pytest.mark.asyncio
async def test_版本号形态不对_也报400() -> None:
    async with _client() as c:
        for bad in ("v1", "1", "1.2.3", "abc"):
            r = await c.get("/health/live", headers={"X-Warden-Api-Version": bad})
            assert r.status_code == 400, bad


def test_当前版本的主版本在被支持列表里() -> None:
    """自检：别把 API_VERSION 改成 2.0 却忘了把 "2" 加进支持列表。

    否则所有声明 2.0 的客户端都会被拒——那是自己把自己锁在门外。
    """
    parsed = _split_version(API_VERSION)
    assert parsed is not None
    assert parsed[0] in API_SUPPORTED_MAJORS
