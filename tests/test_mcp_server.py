"""MCP server（第117 批）：钉住"暴露出去也不绕开门禁"这条语义。

测的不是"能握手"，而是四条一错就危险的行为：
  1. 工具执行失败是 **isError 结果**，不是 JSON-RPC 协议错误（协议错误会让客户端当成断连/重试用）；
  2. DENY 的工具**根本没被执行**（不是执行了再报错）；
  3. ASK 在 stdio 上没有人工通道 ⇒ **按拒绝处理**（静默放行等于把 ASK 降级成 ALLOW）；
  4. 坏 JSON / 未知方法**不断连**（stdio 是长连接，一次脏数据不该废掉整条通道）。
"""

from __future__ import annotations

import io
import json

from warden_agent.mcp.server import (
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    PROTOCOL_VERSION,
    McpServer,
    serve,
)
from warden_agent.policy.policy import Decision, PolicyEngine, PolicyResult
from warden_agent.tool.catalog import ToolCatalog, function_tool


def _catalog(calls: list[str] | None = None) -> ToolCatalog:
    """两个工具：一个正常返回字符串，一个永远抛异常，一个返回 dict。"""
    catalog = ToolCatalog()
    log = calls if calls is not None else []

    @function_tool("weather.get", "查天气",
                   {"type": "object", "properties": {"city": {"type": "string"}},
                    "required": ["city"]})
    def weather(city: str) -> str:
        log.append(f"weather:{city}")
        return f"{city} 晴"

    @function_tool("boom.run", "总是失败",
                   {"type": "object", "properties": {}})
    def boom() -> str:
        log.append("boom")
        raise RuntimeError("内部炸了")

    @function_tool("stats.get", "返回结构体",
                   {"type": "object", "properties": {}})
    def stats() -> dict[str, int]:
        log.append("stats")
        return {"runs": 3}

    for spec in (weather, boom, stats):
        catalog.register(spec)
    return catalog


def _call(server: McpServer, name: str, arguments: dict | None = None, msg_id: int = 1):
    return server.handle({"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
                          "params": {"name": name, "arguments": arguments or {}}})


def test_握手返回协议版本与工具能力() -> None:
    server = McpServer(catalog=_catalog())
    reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2025-03-26", "capabilities": {}}})
    assert reply is not None
    result = reply["result"]
    assert result["protocolVersion"] == PROTOCOL_VERSION
    # 客户端给的是 2025-03-26，我们回自己的版本——不照抄回去假装同意
    assert result["protocolVersion"] != "2025-03-26"
    assert result["capabilities"]["tools"]["listChanged"] is False
    assert result["serverInfo"]["name"] == "warden-agent"


def test_tools_list_形状符合_MCP_约定() -> None:
    server = McpServer(catalog=_catalog())
    tools = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == {"weather.get", "boom.run", "stats.get"}
    weather = next(t for t in tools if t["name"] == "weather.get")
    assert weather["description"] == "查天气"
    assert weather["inputSchema"]["required"] == ["city"]


def test_调用成功返回文本结果() -> None:
    server = McpServer(catalog=_catalog())
    result = _call(server, "weather.get", {"city": "上海"})["result"]
    assert result["isError"] is False
    assert result["content"] == [{"type": "text", "text": "上海 晴"}]


def test_结构体返回值转成_JSON_而不是_python_repr() -> None:
    server = McpServer(catalog=_catalog())
    text = _call(server, "stats.get")["result"]["content"][0]["text"]
    assert json.loads(text) == {"runs": 3}


def test_未知工具是结果错误不是协议错误() -> None:
    server = McpServer(catalog=_catalog())
    reply = _call(server, "nope.tool")
    assert "error" not in reply, "工具不存在属于工具结果，不该走 JSON-RPC error"
    assert reply["result"]["isError"] is True
    assert "nope.tool" in reply["result"]["content"][0]["text"]


def test_工具抛异常被包成_isError_并保留异常类型() -> None:
    server = McpServer(catalog=_catalog())
    reply = _call(server, "boom.run")
    assert "error" not in reply
    payload = reply["result"]
    assert payload["isError"] is True
    assert "RuntimeError" in payload["content"][0]["text"]


def test_参数不合法是结果错误() -> None:
    server = McpServer(catalog=_catalog())
    payload = _call(server, "weather.get", {})["result"]
    assert payload["isError"] is True
    assert "参数" in payload["content"][0]["text"]


def test_DENY_的工具根本没被执行() -> None:
    calls: list[str] = []
    engine = PolicyEngine()
    engine.add(lambda name, args: PolicyResult(Decision.DENY, "测试性拒绝"))
    server = McpServer(catalog=_catalog(calls), policy=engine)
    payload = _call(server, "weather.get", {"city": "上海"})["result"]
    assert payload["isError"] is True
    assert "拒绝" in payload["content"][0]["text"]
    assert calls == [], "DENY 必须在执行前拦住，不能先跑一遍再报错"


def test_ASK_在_stdio_上按拒绝处理() -> None:
    calls: list[str] = []
    engine = PolicyEngine()
    engine.add(lambda name, args: PolicyResult(Decision.ASK, "需要人工批准"))
    server = McpServer(catalog=_catalog(calls), policy=engine)
    payload = _call(server, "weather.get", {"city": "上海"})["result"]
    assert payload["isError"] is True
    assert "没有审批能力" in payload["content"][0]["text"]
    assert calls == [], "ASK 不能被静默降级成 ALLOW"


def test_通知不回复() -> None:
    server = McpServer(catalog=_catalog())
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    # 未知通知（客户端取消）同样静默
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/cancelled",
                          "params": {"requestId": 1}}) is None


def test_未知方法带_id_回_METHOD_NOT_FOUND() -> None:
    server = McpServer(catalog=_catalog())
    reply = server.handle({"jsonrpc": "2.0", "id": 9, "method": "resources/list"})
    assert reply is not None
    assert reply["error"]["code"] == METHOD_NOT_FOUND


def test_缺工具名是_INVALID_PARAMS() -> None:
    server = McpServer(catalog=_catalog())
    reply = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {}})
    assert reply is not None
    assert reply["error"]["code"] == -32602


def test_坏_JSON_不断连_后续请求照常处理() -> None:
    server = McpServer(catalog=_catalog())
    stdin = io.StringIO(
        '{"jsonrpc": "2.0", "id": 1, "method": "ping"}\n'
        "这不是 JSON\n"
        '{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}\n'
    )
    stdout = io.StringIO()
    serve(server, stdin, stdout)
    lines = [json.loads(x) for x in stdout.getvalue().splitlines() if x.strip()]
    assert lines[0]["result"] == {}
    assert lines[1]["error"]["code"] == PARSE_ERROR
    assert "tools" in lines[2]["result"], "坏行之后通道必须还能用"


def test_批处理请求被明确拒绝() -> None:
    server = McpServer(catalog=_catalog())
    stdout = io.StringIO()
    serve(server, io.StringIO('[{"jsonrpc": "2.0", "id": 1, "method": "ping"}]\n'), stdout)
    reply = json.loads(stdout.getvalue().strip())
    assert reply["error"]["code"] == INVALID_REQUEST


def test_空行被忽略() -> None:
    server = McpServer(catalog=_catalog())
    stdout = io.StringIO()
    serve(server, io.StringIO("\n\n  \n"), stdout)
    assert stdout.getvalue() == ""


def test_审计钩子收到方法名与成败() -> None:
    events: list[tuple[str, bool]] = []
    server = McpServer(catalog=_catalog(), on_event=lambda m, p, ok: events.append((m, ok)))
    _call(server, "weather.get", {"city": "上海"})
    _call(server, "nope")
    assert events == [("tools/call", True), ("tools/call", True)]


def test_审计钩子自己抛异常不影响主流程() -> None:
    def boom(method: str, params: dict, ok: bool) -> None:
        raise RuntimeError("审计坏了")

    server = McpServer(catalog=_catalog(), on_event=boom)
    reply = _call(server, "weather.get", {"city": "上海"})
    assert reply["result"]["isError"] is False
