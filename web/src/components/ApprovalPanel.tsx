// ApprovalPanel：侧边栏的"审批队列"，轮询 GET /approvals 展示所有待审批会话，
// 并支持批准/拒绝（也按 run_id 走 /approve、/reject）。

import { useCallback } from "react";
import type { Approval } from "../lib/types";
import { usePolling } from "../lib/usePolling";
import { api } from "../lib/api";

export default function ApprovalPanel() {
  const { data: approvals, error, refresh } = usePolling<Approval[]>(
    () => api.approvals(),
    2000,
  );

  const act = useCallback(
    async (runId: string, action: "approve" | "reject") => {
      try {
        if (action === "approve") await api.approve(runId);
        else await api.reject(runId);
        refresh();
      } catch (e) {
        alert(e instanceof Error ? e.message : String(e));
      }
    },
    [refresh],
  );

  const list = approvals ?? [];

  return (
    <div className="rounded-xl border border-warden-line bg-warden-panel p-3">
      <h3 className="text-sm font-medium text-warden-fg/80 mb-2">审批队列</h3>
      {error && <p className="text-xs text-warden-danger">⚠️ {error}</p>}
      {list.length === 0 ? (
        <p className="text-xs text-warden-fg/50">（暂无待审批）</p>
      ) : (
        <ul className="flex flex-col gap-2">
          {list.map((a) => (
            <li
              key={a.run_id + a.approval_id}
              className="rounded-lg border border-warden-line bg-warden-bg p-2 text-xs"
            >
              <div className="text-warden-fg/80">
                run=<span className="text-warden-warn">{a.run_id}</span>
              </div>
              <div className="text-warden-fg">
                工具=<span className="text-warden-accent">{a.tool_name}</span>
              </div>
              {a.reason && (
                <div className="text-warden-fg/60 mt-0.5 break-words">原因：{a.reason}</div>
              )}
              <div className="flex gap-2 mt-1.5">
                <button
                  onClick={() => act(a.run_id!, "approve")}
                  className="px-2.5 py-1 rounded bg-warden-ok text-[#06281a] font-medium"
                >
                  批准
                </button>
                <button
                  onClick={() => act(a.run_id!, "reject")}
                  className="px-2.5 py-1 rounded bg-warden-danger text-white font-medium"
                >
                  拒绝
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
