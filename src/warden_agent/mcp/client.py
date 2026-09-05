"""MCP 客户端（Python 侧）—— 连接外部 MCP 服务器并导入其工具。

  - 底层协议用 TypeScript 官方 SDK（@modelcontextprotocol/sdk）实现（见 ts/mcp-client/），
    通过 stdio transport 连接 MCP server。Python 侧通过 spawn node 进程驱动它。
  - 本模块负责"工具导入链路"：连接 → 列出工具 → **本地审查** → 导入工具管线。

设计要点：
  1. 先本地审查再导入：远端工具不会被无脑塞进 Agent 的工具箱，
     而是先过 McpToolImportReview（默认拒绝危险命名 / 危险参数，可配白名单）。
  2. 导入后是普通 ToolSpec：Agent 通过统一工具管线调用，
     不绕过 Tool pipeline，依然受 PolicyEngine 门禁约束。
  3. 调用通过 node CLI 的 call 操作转发给 MCP server。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from warden_agent.tool.catalog import ToolSpec, function_tool

# 默认审查用的危险关键词：工具名/参数若含这些，默认拒绝导入
_DANGEROUS_NAME = ("shell", "exec", "system", "delete", "rm", "drop", "write")
_DANGEROUS_ARG = ("command", "script", "sql")

_DEFAULT_CLI = str(Path(__file__).resolve().parents[3] / "ts" / "mcp-client" / "mcp-client.mjs")


@dataclass
class McpToolBinding:
    """一个来自 MCP server 的工具。"""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class ImportDecision:
    """一次导入审查的结论。"""

    allowed: bool
    reason: str


@dataclass
class McpImportReport:
    """一次 MCP 会话的导入报告。"""

    server: str
    discovered: int
    imported: int = 0
    rejected: list[str] = field(default_factory=list)
    bindings: list[McpToolBinding] = field(default_factory=list)


class McpImportReview:
    """本地审查：判断一个远端工具该不该导入 Agent 工具管线。"""

    def __init__(
        self,
        allowed_names: set[str] | None = None,
        block_dangerous: bool = True,
    ) -> None:
        self._allowed = allowed_names or set()
        self._block_dangerous = block_dangerous

    def review(self, binding: McpToolBinding) -> ImportDecision:
        # 显式白名单通过
        if binding.name in self._allowed:
            return ImportDecision(True, "白名单")
        if not self._block_dangerous:
            return ImportDecision(True, "未启用危险拦截")
        # 危险命名
        low = binding.name.lower()
        if any(k in low for k in _DANGEROUS_NAME):
            return ImportDecision(False, f"工具名含危险关键词: {binding.name}")
        # 危险参数
        props = binding.input_schema.get("properties", {})
        if any(k.lower() in _DANGEROUS_ARG for k in props):
            return ImportDecision(False, f"工具 {binding.name} 含危险参数")
        return ImportDecision(True, "通过")


class McpClient:
    """驱动 node CLI 的 MCP 客户端。connect → list → import_reviewed -> call。"""

    def __init__(
        self,
        server_command: str,
        *,
        cli: str | None = None,
        review: McpImportReview | None = None,
        node_bin: str = "node",
    ) -> None:
        self.server_command = server_command
        self.cli = str(cli or _DEFAULT_CLI)
        self.review = review or McpImportReview()
        self.node_bin = node_bin

    def _run(self, op: str, tool: str | None = None, args: dict[str, Any] | None = None
             ) -> dict[str, Any]:
        cmd = [
            self.node_bin, self.cli,
            "--server", self.server_command,
            "--op", op,
        ]
        if tool:
            cmd += ["--tool", tool]
        if args is not None:
            cmd += ["--args", json.dumps(args)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            return {"error": "MCP 调用超时"}
        except OSError as e:
            return {"error": f"无法启动 node CLI: {e}"}
        last_line = (proc.stdout or "").strip().splitlines()
        if not last_line:
            return {"error": f"node CLI 无输出: {(proc.stderr or '')[:200]}"}
        try:
            return cast(dict[str, Any], json.loads(last_line[-1]))
        except json.JSONDecodeError:
            return {"error": last_line[-1][:300]}

    def list_tools(self) -> list[McpToolBinding]:
        """列出远端工具（原始，未审查）。"""
        data = self._run("list")
        if "error" in data:
            raise RuntimeError(f"MCP list 失败: {data['error']}")
        bindings = []
        for t in data.get("tools", []):
            bindings.append(McpToolBinding(
                name=t.get("name", ""),
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {}),
            ))
        return bindings

    def import_reviewed(self, catalog: Any) -> McpImportReport:
        """列出 → 逐个本地审查 → 把允许的工具导入 ToolCatalog，返回报告。

        catalog：一个 warden_agent.tool.catalog.ToolCatalog 实例。
        """
        bindings = self.list_tools()
        report = McpImportReport(server=self.server_command, discovered=len(bindings))
        for binding in bindings:
            decision = self.review.review(binding)
            if not decision.allowed:
                report.rejected.append(f"{binding.name}（{decision.reason}）")
                continue
            catalog.register(self._to_spec(binding))
            report.bindings.append(binding)
            report.imported += 1
        return report

    def call(self, tool: str, args: dict[str, Any]) -> Any:
        """通过 MCP server 调用一个工具。"""
        data = self._run("call", tool=tool, args=args)
        if "error" in data:
            raise RuntimeError(f"MCP call {tool} 失败: {data['error']}")
        result = data.get("result", {})
        # 提取文本内容
        contents = result.get("content", [])
        texts = [c.get("text", "") for c in contents if c.get("type") == "text"]
        return "\n".join(texts) if texts else result

    def make_tool(self, binding: McpToolBinding) -> ToolSpec:
        """把一个 MCP 工具转成 Python ToolSpec（不立即注册）。"""
        return self._to_spec(binding)

    def _to_spec(self, binding: McpToolBinding) -> ToolSpec:
        schema = dict(binding.input_schema)
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})

        @function_tool(
            f"mcp.{binding.name}",
            f"[MCP:{binding.name}] {binding.description}",
            schema,
            pure=False,
        )
        def _call(**kwargs: Any) -> Any:
            return self.call(binding.name, kwargs)

        return _call


def node_available() -> bool:
    """检测 node 是否可用（MCP 客户端依赖它）。"""
    return shutil.which("node") is not None
