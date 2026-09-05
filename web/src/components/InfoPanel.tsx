// InfoPanel：侧边栏的信息面板——run 状态、能力(capabilities)、记忆(memory)、健康检查。

import { useEffect, useState } from "react";
import { api } from "../lib/api";
import type { Capabilities, HealthResult, MemoryItem } from "../lib/types";
import { usePolling } from "../lib/usePolling";

export default function InfoPanel({ runId }: { runId: string }) {
  const [runStatus, setRunStatus] = useState<string>("…");
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [memories, setMemories] = useState<MemoryItem[]>([]);

  // 健康检查轮询
  const { data: health } = usePolling<HealthResult>(() => api.health(), 5000);

  // run 状态 + 能力 + 记忆（加载一次，run 状态由外部刷新）
  useEffect(() => {
    api.status(runId).then((s) => setRunStatus(s.status)).catch(() => {});
    api.capabilities().then(setCaps).catch(() => {});
    api.memory("session").then(setMemories).catch(() => setMemories([]));
  }, [runId]);

  const statusColor =
    runStatus === "COMPLETED"
      ? "text-warden-ok"
      : runStatus === "FAILED" || runStatus === "CANCELLED"
        ? "text-warden-danger"
        : runStatus === "WAITING_APPROVAL"
          ? "text-warden-warn"
          : "text-warden-fg";

  return (
    <div className="flex flex-col gap-3">
      <div className="rounded-xl border border-warden-line bg-warden-panel p-3">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-medium text-warden-fg/80">Run 状态</h3>
          <button
            onClick={() =>
              api.status(runId).then((s) => setRunStatus(s.status)).catch(() => {})
            }
            className="text-xs text-warden-accent hover:underline"
          >
            刷新
          </button>
        </div>
        <div className="mt-1 flex items-center gap-2">
          <span className={`text-sm font-medium ${statusColor}`}>{runStatus || "…"}</span>
        </div>
        <div className="mt-1 text-xs text-warden-fg/50 font-mono break-all">{runId}</div>
      </div>

      {/* 能力 */}
      <div className="rounded-xl border border-warden-line bg-warden-panel p-3">
        <h3 className="text-sm font-medium text-warden-fg/80 mb-2">能力</h3>
        {!caps ? (
          <p className="text-xs text-warden-fg/50">加载中…</p>
        ) : (
          <>
            <div className="mb-1.5 flex flex-wrap gap-1">
              {caps.features.memory && <Chip>memory</Chip>}
              {caps.features.web && <Chip>web</Chip>}
              {caps.features.skills.map((s) => (
                <Chip key={s}>skill:{s}</Chip>
              ))}
              {caps.features.mcp_server && <Chip>mcp:{caps.features.mcp_server}</Chip>}
            </div>
            <div className="text-xs text-warden-fg/60">
              <span className="text-warden-fg/80">工具({caps.tools.length})：</span>
              {caps.tools.join(", ") || "无"}
            </div>
          </>
        )}
      </div>

      {/* 记忆 */}
      <div className="rounded-xl border border-warden-line bg-warden-panel p-3">
        <h3 className="text-sm font-medium text-warden-fg/80 mb-2">记忆(session)</h3>
        {memories.length === 0 ? (
          <p className="text-xs text-warden-fg/50">（暂无记忆）</p>
        ) : (
          <ul className="flex flex-col gap-1.5">
            {memories.map((m) => (
              <li key={m.key} className="text-xs text-warden-fg/70">
                <span className="text-warden-accent">{m.key}</span>：{m.text}
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* 健康 */}
      <div className="rounded-xl border border-warden-line bg-warden-panel p-3">
        <h3 className="text-sm font-medium text-warden-fg/80 mb-2">健康检查</h3>
        {!health ? (
          <p className="text-xs text-warden-fg/50">—</p>
        ) : (
          <div className="flex items-center gap-2 text-sm">
            <span
              className={`inline-block w-2.5 h-2.5 rounded-full ${
                health.status === "ok" ? "bg-warden-ok" : "bg-warden-danger"
              }`}
            />
            <span
              className={
                health.status === "ok" ? "text-warden-ok" : "text-warden-danger"
              }
            >
              {health.status}
            </span>
          </div>
        )}
      </div>
    </div>
  );
}

function Chip({ children }: { children: React.ReactNode }) {
  return (
    <span className="px-2 py-0.5 rounded-full bg-warden-accent/15 text-warden-accent text-[11px]">
      {children}
    </span>
  );
}
