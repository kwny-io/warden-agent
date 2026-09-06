// InfoPanel：侧边栏的信息面板——run 状态、能力(capabilities)、记忆(memory)、健康检查。

import { useEffect, useState } from "react";
import { api } from "../lib/api";
import type { Capabilities, HealthResult, MemoryItem } from "../lib/types";
import { usePolling } from "../lib/usePolling";

export default function InfoPanel({ runId }: { runId: string }) {
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [toolsOpen, setToolsOpen] = useState(false);

  // 健康检查轮询
  const { data: health } = usePolling<HealthResult>(() => api.health(), 5000);

  // run 状态轮询（3s）：聊完一轮状态卡能自动跟上，不用手点刷新
  const {
    data: runStatusData,
    refresh: refreshRunStatus,
  } = usePolling<{ run_id: string; status: string }>(() => api.status(runId), 3000, runId);
  const runStatus = runStatusData?.status ?? "…";

  // 记忆轮询（5s）：memory.remember 之后卡片能自动跟上
  const { data: memData } = usePolling<MemoryItem[]>(() => api.memory("session"), 5000, runId);
  const memories = memData ?? [];

  // 工具列表默认只展示 6 个，可展开
  const shownTools = caps ? (toolsOpen ? caps.tools : caps.tools.slice(0, 6)) : [];

  // 能力（runId 变化时加载一次）
  useEffect(() => {
    api.capabilities().then(setCaps).catch(() => {});
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
      <div className="wpanel p-3.5">
        <div className="flex items-center justify-between">
          <h3 className="flex items-center gap-1.5 text-sm font-medium text-warden-fg">
            <span className="w-2 h-2 rounded-full bg-warden-accent shadow-[0_0_8px_rgba(86,184,201,0.8)]" />
            Run 状态
          </h3>
          <button
            onClick={() => refreshRunStatus()}
            className="text-xs text-warden-accent hover:underline"
          >
            刷新
          </button>
        </div>
        <div className="mt-1 flex items-center gap-2">
          <span className={`text-sm font-medium ${statusColor}`}>{runStatus || "…"}</span>
        </div>
        <div className="mt-1 text-xs text-warden-fg/70 font-mono break-all">{runId}</div>
      </div>

      {/* 能力 */}
      <div className="wpanel p-3.5">
        <h3 className="flex items-center gap-1.5 text-sm font-medium text-warden-fg mb-2">
          <span className="w-2 h-2 rounded-full bg-warden-cyan shadow-[0_0_8px_rgba(111,211,199,0.8)]" />
          能力
        </h3>
        {!caps ? (
          <p className="text-xs text-warden-fg/70">加载中…</p>
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
            <div className="mb-1.5 flex flex-wrap gap-1">
              {shownTools.map((t) => (
                <span
                  key={t}
                  className="px-1.5 py-0.5 rounded border border-zinc-700 bg-zinc-800/70 text-[10px] font-mono text-zinc-300"
                >
                  {t}
                </span>
              ))}
            </div>
            {caps.tools.length > 6 && (
              <button
                onClick={() => setToolsOpen((v) => !v)}
                className="text-[10px] text-warden-accent hover:underline"
              >
                {toolsOpen ? "收起" : `展开全部 (${caps.tools.length})`}
              </button>
            )}
          </>
        )}
      </div>

      {/* 记忆 */}
      <div className="wpanel p-3.5">
        <h3 className="flex items-center gap-1.5 text-sm font-medium text-warden-fg mb-2">
          <span className="w-2 h-2 rounded-full bg-sky-300 shadow-[0_0_8px_rgba(125,211,252,0.7)]" />
          记忆(session)
        </h3>
        {memories.length === 0 ? (
          <p className="text-xs text-warden-fg/70">（暂无记忆）</p>
        ) : (
          <ul className="flex flex-col gap-1.5">
            {memories.map((m) => (
              <li key={m.key} className="text-xs text-warden-fg/85">
                <span className="text-warden-accent">{m.key}</span>：{m.text}
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* 健康 */}
      <div className="wpanel p-3.5">
        {/* 状态灯就在标题左边：绿=ok，红=异常；悬停可看原始状态 */}
        <h3 className="flex items-center gap-1.5 text-sm font-medium text-warden-fg">
            <span
              title={health?.status ?? "…"}
              className={`inline-block w-2.5 h-2.5 rounded-full ${
                !health
                  ? "bg-warden-fg/30"
                  : health.status === "ok"
                    ? "bg-warden-ok shadow-[0_0_10px_rgba(91,191,179,0.9)]"
                    : "bg-warden-danger shadow-[0_0_10px_rgba(224,122,106,0.9)]"
              }`}
            />
          健康检查
        </h3>
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
