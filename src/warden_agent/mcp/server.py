"""MCP server：把本仓的工具**反向**暴露给别的 Agent（第117 批）。

此前仓里只有 `mcp/client.py`——Warden 能**用**别人的 MCP 工具，但不能被别的 Agent **用**。
这条腿缺失的代价很具体：Claude Desktop / Cursor / 其它 Agent 想把 Warden 的
`fs.read`、`shell.run`、`knowledge.search` 当工具用，只能自己再包一层 HTTP，
于是"审批、审计、沙箱边界"这些本仓最强的部分在跨进程使用时**全部失效**——
对方拿到的是一个裸函数，没有任何门禁。

本模块补的就是这条腿，并且**刻意不绕开门禁**：

  · 每一次 `tools/call` 都先过 `PolicyEngine`（DENY 直接拒；ASK 因为 stdio 上没有
    人工通道，**按拒绝处理**并说明原因——不假装"已批准"）；
  · 工具抛异常不升级成 JSON-RPC 协议错误，而是 `isError: true` 的**工具结果**——
    这是 MCP 的约定：工具执行失败是"结果"，协议错误才是"错误"；
  · 只暴露目录里注册过的工具（冻结工具集），未知工具名一律拒绝。

协议实现取最小可用集（stdio + 换行分隔的 JSON-RPC 2.0）：
`initialize` / `notifications/initialized` / `tools/list` / `tools/call` / `ping`。
**没实现**：`resources/*`、`prompts/*`、`sampling`、SSE 传输、断线重连、并发批处理。
这些不做不是因为难，而是因为它们各自需要一套独立的语义（例如 resources 需要 URI 生命周期），
半吊子实现比不实现更容易误导使用者。

用法（stdio，一行一条 JSON）：

    python -m warden_agent.mcp.server            # 默认暴露只读 + 沙箱 shell
    python -m warden_agent.mcp.server --list     # 只打印工具清单后退出（自检用）
"""

from __future__ import annotations

import contextlib
import json
import sys
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any, TextIO

from warden_agent.policy.policy import Decision, PolicyDenied, PolicyEngine
from warden_agent.tool.catalog import ToolArgumentError, ToolCatalog

# MCP 协议版本：与 client.py 里握手用的版本保持一致（同一份协议两端都要认。
# 升版本必须两端一起升，否则"客户端说 2024-11-05、服务端回 2025-03-26"会让严格的客户端断连）。
PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "warden-agent"
SERVER_VERSION = "0.1.0"

# JSON-RPC 2.0 标准错误码（不要自定义数字，客户端按这些码分类处理）
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _error(msg_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": err}


def _result(msg_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": payload}


def _text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    """MCP 的工具返回形状：content 是块列表，isError 表示"工具执行失败"（不是协议错误）。"""
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _as_text(value: Any) -> str:
    """把工具返回值转成文本。dict/list 走 JSON（别用 repr，客户端解析不了 Python 字面量）。"""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(value)


@dataclass
class McpServer:
    """把 `ToolCatalog` 暴露成 MCP 工具面。

    `policy` 是**硬门禁**：不给就装一个空的（全放行）——但那意味着"暴露出去的工具没有审批"，
    所以 CLI 入口默认会显式说明当前策略，而不是让人以为默认安全。
    """

    catalog: ToolCatalog
    policy: PolicyEngine = field(default_factory=PolicyEngine)
    name: str = SERVER_NAME
    version: str = SERVER_VERSION
    # 可选审计钩子：每条请求 (method, params, ok) 都会回调一次（不改变行为，只留痕）
    on_event: Any = None

    # ---- 协议层 ----

    def handle(self, message: Any) -> dict[str, Any] | None:
        """处理一条 JSON-RPC 消息；通知（无 id）返回 None（不回复）。"""
        if not isinstance(message, dict):
            return _error(None, INVALID_REQUEST, "请求必须是 JSON 对象")
        method = message.get("method")
        msg_id = message.get("id")
        is_notification = "id" not in message
        if not isinstance(method, str):
            return None if is_notification else _error(msg_id, INVALID_REQUEST, "缺少 method")
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return None if is_notification else _error(msg_id, INVALID_PARAMS, "params 必须是对象")

        handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "initialize": self._initialize,
            "tools/list": self._tools_list,
            "tools/call": self._tools_call,
            "ping": lambda _p: {},
        }
        handler = handlers.get(method)
        if handler is None:
            # 未知方法：按 MCP 约定，**通知**（例如 notifications/cancelled）静默丢弃，
            # 带 id 的请求才回 METHOD_NOT_FOUND。
            self._emit(method, params, ok=False)
            return None if is_notification else _error(
                msg_id, METHOD_NOT_FOUND, f"未实现的方法: {method}")
        try:
            payload = handler(params)
        except _ProtocolError as exc:
            self._emit(method, params, ok=False)
            return None if is_notification else _error(msg_id, exc.code, str(exc))
        except Exception as exc:  # noqa: BLE001 - 兜底：协议层不能因为工具层崩而断连
            self._emit(method, params, ok=False)
            return None if is_notification else _error(
                msg_id, INTERNAL_ERROR, f"服务端内部错误: {exc}")
        self._emit(method, params, ok=True)
        return None if is_notification else _result(msg_id, payload)

    def _emit(self, method: str, params: dict[str, Any], *, ok: bool) -> None:
        if self.on_event is None:
            return
        # 审计钩子坏了不能影响主流程（但钩子自己该负责记日志——这里不吞掉"请求"）
        with contextlib.suppress(Exception):
            self.on_event(method, params, ok)

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """握手。客户端给的 protocolVersion 我们**不会**照抄回去假装同意。"""
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": self.name, "version": self.version},
        }

    def _tools_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "tools": [
                {
                    "name": spec.name,
                    "description": spec.description,
                    "inputSchema": spec.parameters_schema or {"type": "object", "properties": {}},
                }
                for spec in self.catalog.all()
            ]
        }

    def _tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise _ProtocolError(INVALID_PARAMS, "tools/call 缺少工具名 name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise _ProtocolError(INVALID_PARAMS, "arguments 必须是对象")

        # 门禁先于执行：审批策略不是"建议"，DENY 就是不许跑。
        verdict = self.policy.evaluate(name, arguments)
        if verdict.decision == Decision.DENY:
            return _text_result(f"策略拒绝执行 {name}: {verdict.reason}", is_error=True)
        if verdict.decision == Decision.ASK:
            # stdio 上没有人工通道：**按拒绝处理**，并把"为什么"说清楚。
            # 静默放行等于把 ASK 降级成 ALLOW——那是最危险的失真。
            return _text_result(
                f"工具 {name} 需要人工批准（{verdict.reason}），但 stdio 通道没有审批能力；"
                "已按拒绝处理。要用它：先在 Warden 侧批准，或把这条工具移出 ASK 名单",
                is_error=True,
            )
        try:
            value = self.catalog.execute(name, arguments)
        except KeyError:
            return _text_result(f"没有这个工具: {name}（只暴露已注册工具）", is_error=True)
        except ToolArgumentError as exc:
            return _text_result(f"参数不合法: {exc}", is_error=True)
        except PolicyDenied as exc:
            return _text_result(f"策略拒绝执行 {name}: {exc}", is_error=True)
        except Exception as exc:  # noqa: BLE001 - 工具失败是"结果"，不是协议错误
            return _text_result(f"工具 {name} 执行失败: {type(exc).__name__}: {exc}", is_error=True)
        return _text_result(_as_text(value))


class _ProtocolError(Exception):
    """handler 内部抛出的参数类错误（由 handle() 转成 JSON-RPC error）。"""

    def __init__(self, code: int, message: str) -> None:
        self.code = code
        super().__init__(message)


def serve(server: McpServer, stdin: TextIO, stdout: TextIO) -> None:
    """stdio 主循环：一行一条 JSON。空行忽略；坏 JSON 回 PARSE_ERROR 但**不断连**。"""
    for line in stdin:
        raw = line.strip()
        if not raw:
            continue
        try:
            message = json.loads(raw)
        except json.JSONDecodeError as exc:
            _write(stdout, _error(None, PARSE_ERROR, f"JSON 解析失败: {exc}"))
            continue
        if isinstance(message, list):
            # 批处理不做：MCP 客户端极少用，而"半支持"会让顺序语义变得含糊。
            _write(stdout, _error(None, INVALID_REQUEST, "不支持批处理请求"))
            continue
        reply = server.handle(message)
        if reply is not None:
            _write(stdout, reply)


def _write(stdout: TextIO, payload: dict[str, Any]) -> None:
    stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    stdout.flush()


def iter_messages(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    """把一堆文本行解析成消息（测试与批处理用；坏行直接抛，不静默跳过）。"""
    for line in lines:
        if line.strip():
            yield json.loads(line)


def build_default_catalog() -> ToolCatalog:
    """CLI 默认目录：只读文件 + 沙箱 shell。

    为什么默认是"最小可用"而不是全套：这个进程的 stdin/stdout 被别的 Agent 占用，
    没有人工审批通道，所以默认只暴露**改不了仓库**的工具。
    要暴露写类工具，请在自己的宿主程序里构造 catalog 再用 `McpServer(catalog=...)`，
    并把 policy 一起带上。
    """
    from warden_agent.agent import augment_catalog

    catalog = ToolCatalog()
    augment_catalog(catalog, sandbox=True)
    return catalog


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    server = McpServer(catalog=build_default_catalog())
    if "--list" in args:
        tools = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        print(json.dumps(tools, ensure_ascii=False, indent=2))
        return 0
    serve(server, sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":  # pragma: no cover - 入口
    raise SystemExit(main())
