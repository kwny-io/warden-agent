// ConversationList：左侧对话栏——历史对话一览，点条目切换会话。
// 5s 轮询 /runs；带最后活跃时间戳和状态彩点。

import { type MouseEvent as ReactMouseEvent } from "react";
import type { RunInfo } from "../lib/types";
import { usePolling } from "../lib/usePolling";
import { api } from "../lib/api";

const STATUS_DOT: Record<string, string> = {
  COMPLETED: "bg-warden-ok",
  WAITING_APPROVAL: "bg-warden-warn",
  FAILED: "bg-warden-danger",
  CANCELLED: "bg-warden-danger",
  TIMED_OUT: "bg-warden-danger",
  RUNNING: "bg-warden-accent animate-pulse",
};

/** ISO 时间 → 本地 "MM-DD HH:mm"；无时间（老数据）返回空串 */
function fmtTime(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export default function ConversationList({
  activeRunId,
  onSwitch,
  onNew,
  onCollapse,
  onDeleted,
}: {
  activeRunId: string;
  onSwitch: (runId: string) => void;
  onNew: () => void;
  onCollapse: () => void;
  onDeleted: (deletedId: string, remaining: RunInfo[]) => void;
}) {
  const { data, refresh } = usePolling<RunInfo[]>(() => api.runs(), 5000);
  const runs = data ?? [];

  const remove = async (e: ReactMouseEvent, r: RunInfo) => {
    e.stopPropagation(); // 别触发切换会话
    if (!window.confirm(`删除对话「${r.title || r.run_id}」？记录不可恢复。`)) return;
    try {
      await api.deleteRun(r.run_id);
      refresh();
      const remaining = runs.filter((x) => x.run_id !== r.run_id);
      onDeleted(r.run_id, remaining);
    } catch {
      refresh();
    }
  };

  return (
    <div className="w-full flex flex-col gap-3 min-h-0">
      <div className="glass rounded-2xl p-3 flex flex-col gap-2.5 flex-1 min-h-0">
        {/* 面板头部 */}
        <div className="flex items-center gap-1.5 px-0.5 pb-2 border-b border-white/[0.06]">
          <span className="w-2 h-2 rounded-full bg-warden-cyan shadow-[0_0_10px_rgba(111,211,199,0.9)]" />
          <span className="text-sm font-medium text-zinc-100">对话</span>
          {runs.length > 0 && (
            <span className="text-[10px] font-mono text-zinc-500">{runs.length}</span>
          )}
          <button
            onClick={onCollapse}
            title="收起对话栏"
            className="ml-auto w-6 h-6 rounded-md text-warden-fg/40 hover:text-warden-fg hover:bg-white/[0.06] transition"
          >
            ⟨
          </button>
        </div>

      <button
        onClick={onNew}
        className="btn-sheen shrink-0 rounded-lg border border-warden-accent/40 px-3 py-2 text-sm text-warden-fg hover:border-warden-accent/80 transition"
      >
        ＋ 新对话
      </button>

      <div className="flex-1 min-h-0 overflow-y-auto flex flex-col pr-0.5">
        {runs.length === 0 && (
          <p className="text-xs text-warden-fg/35 text-center mt-4">还没有对话</p>
        )}
          {runs.map((r) => {
            const active = r.run_id === activeRunId;
            return (
              <div
                key={r.run_id}
                className={`group relative w-full text-left border-l-2 transition-colors ${
                  active ? "border-warden-accent" : "border-transparent hover:border-slate-600/60"
                }`}
              >
                <button
                  onClick={() => onSwitch(r.run_id)}
                  title={`${r.title}（${r.run_id}）`}
                  className="w-full text-left px-2.5 py-1.5 block"
                >
                  <div className="flex items-center gap-1.5">
                    <span
                      className={`w-1.5 h-1.5 shrink-0 rounded-full ${
                        STATUS_DOT[r.status] ?? "bg-warden-fg/30"
                      }`}
                    />
                    <span
                      className={`text-xs truncate flex-1 ${
                        active ? "text-warden-fg" : "text-warden-fg/70"
                      }`}
                    >
                      {r.title || r.run_id}
                    </span>
                  </div>
                  <div className="mt-0.5 pl-3 text-[10px] font-mono text-warden-fg/30 truncate">
                    {fmtTime(r.updated_at) || "—"} · {r.msg_count} 条 · {r.run_id}
                  </div>
                  {/* 分割线：两边浅中间深的渐变 */}
                  <div className="mt-1.5 h-px bg-gradient-to-r from-transparent via-zinc-400/60 to-transparent" />
                </button>
                <button
                  onClick={(e) => remove(e, r)}
                  title="删除对话"
                  className="absolute top-1.5 right-1 w-5 h-5 rounded text-[11px] leading-none text-zinc-600 opacity-0 group-hover:opacity-100 hover:text-warden-danger hover:bg-white/[0.06] transition"
                >
                  🗑
                </button>
              </div>
            );
          })}
      </div>
      </div>
    </div>
  );
}
