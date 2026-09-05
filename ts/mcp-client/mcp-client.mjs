// MCP 客户端（TypeScript，官方 @modelcontextprotocol/sdk）
//
// 用途：连接一个 MCP stdio server，列出/审查/调用其工具。
// 设计要点：
//   - StdioTransport（通过 ExecutionBroker 托管子进程）
//   - McpToolDiscoveryService / McpToolImportPolicy（工具先本地审查再导入）
//   - McpToolCatalogContribution（导入后进入统一工具管线）
//
// 设计：这个 CLI 是"进程边界"，Python 侧通过 spawn 一个 node 进程来驱动它。
// 每个操作一行 JSON 输出，方便 Python 解析。
//
// 用法：
//   node mcp-client.mjs --server "npx -y @modelcontextprotocol/server-everything" --op list
//   node mcp-client.mjs --server "<cmd>" --op call --tool <toolName> --args '{"x":1}'

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

function parseArgs() {
  const argv = process.argv.slice(2);
  const get = (flag) => {
    const i = argv.indexOf(flag);
    return i >= 0 && i + 1 < argv.length ? argv[i + 1] : undefined;
  };
  return {
    server: get("--server"),
    op: get("--op") || "list",
    tool: get("--tool"),
    args: get("--args"),
    timeoutMs: get("--timeout") ? Number(get("--timeout")) : 30000,
  };
}

function out(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

async function run() {
  const { server, op, tool, args, timeoutMs } = parseArgs();
  if (!server) {
    out({ error: "缺少 --server <MCP server 启动命令>" });
    process.exit(2);
  }

  // 解析启动命令（支持带参数，例如：npx -y some-server --flag x）
  const parts = server.split(" ");
  const command = parts[0];
  const serverArgs = parts.slice(1);

  const client = new Client({ name: "warden-mcp-client", version: "0.1.0" });
  const transport = new StdioClientTransport({
    command,
    args: serverArgs,
  });

  try {
    const timeout = new Promise((_, rej) =>
      setTimeout(() => rej(new Error("MCP 连接/操作超时")), timeoutMs)
    );
    const work = (async () => {
      await client.connect(transport);
      if (op === "list" || op === "review") {
        const { tools } = await client.listTools();
        await client.close();
        return { op, count: tools.length, tools };
      }
      if (op === "call") {
        if (!tool) {
          await client.close();
          return { error: "call 操作需要 --tool" };
        }
        const parsedArgs = args ? JSON.parse(args) : {};
        const result = await client.callTool({ name: tool, arguments: parsedArgs });
        await client.close();
        return { op, tool, result };
      }
      await client.close();
      return { error: `未知操作: ${op}` };
    })();
    const res = await Promise.race([work, timeout]);
    out(res);
  } catch (e) {
    try { await client.close(); } catch (_) {}
    out({ error: String(e && e.message ? e.message : e) });
    process.exit(1);
  }
}

run();
