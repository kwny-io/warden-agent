// ApprovalPanel：侧边栏的"审批队列"，轮询 GET /approvals 展示所有待审批会话，
// 并支持批准/拒绝（也按 run_id 走 /approve、/reject）。

import { useCallback, useState } from "react";
import type { Approval, ApprovalHistoryItem } from "../lib/types";
import { toApprovalHistory, toApprovals } from "../lib/guards";
import { toolLabel } from "../lib/labels";
import { usePolling } from "../lib/usePolling";
import { api } from "../lib/api";

export default function ApprovalPanel({ onDecided }: { onDecided?: () => void } = {}) {
  // 默认显示人话（「应用代码补丁」），技术用户可一键看回原始工具名（`git.apply_patch`）。
  // 为什么要默认收起原始名：审批的意义是「这个人能判断该不该批」，而
  // `git.apply_patch · run-e2e-app` 对非技术用户是一串无法判断的字母。
  // 为什么又要留开关：出了问题时要能对着日志/审计账本核名字，抹掉原文等于切断取证。
  const [showRaw, setShowRaw] = useState(false);
  const { data: approvals, error, refresh } = usePolling<Approval[]>(
    // signal 必须透传：否则切面板/卸载时只丢结果不断连接，后端还在算
    (signal) => api.approvals({ signal }),
    2000,
  );
  // 审批决策历史（已批准/已拒绝），5s 轮询；error 也要接：拉不到历史不等于“从没审批过”
  const { data: history, error: historyError } = usePolling<ApprovalHistoryItem[]>(
    (signal) => api.approvalHistory({ signal }),
    5000,
  );
  const historyList = toApprovalHistory(history);

  const act = useCallback(
    async (runId: string, action: "approve" | "reject") => {
      try {
        // 响应体里带着"恢复后这一轮的最终回答"（后端 /approve 会接着把会话跑完）。
        // 这里**不能丢掉它**：队列批准的入口不在聊天区，用户批完只看到队列空了一格，
        // 聊天区还是那句"需要审批"——看起来像"批了但什么都没发生"。
        // 所以决策成功后通知上层（App 会让聊天区重拉一次会话记录）。
        if (action === "approve") await api.approve(runId);
        else await api.reject(runId);
        refresh();
        onDecided?.();
      } catch (e) {
        alert(e instanceof Error ? e.message : String(e));
      }
    },
    [refresh, onDecided],
  );

  const list = toApprovals(approvals);

  return (
    <div className="wpanel p-3.5">
      <h3 className="flex items-center gap-1.5 text-sm font-medium text-warden-fg mb-2">
        <span className="w-2 h-2 rounded-full bg-warden-warn shadow-[0_0_8px_rgba(210,162,76,0.8)]" />
        审批队列
        {/* 显示/隐藏原始技术标识的开关：默认人话，点一下看回 `git.apply_patch` */}
        <button
          onClick={() => setShowRaw((v) => !v)}
          title={showRaw ? "改为显示中文说明" : "显示原始工具名与会话 ID"}
          aria-label={showRaw ? "隐藏原始标识" : "显示原始标识"}
          className="ml-auto px-1.5 py-0.5 rounded border border-slate-600/60 text-[10px] text-warden-fg/60 hover:border-warden-accent/70 hover:text-warden-fg transition"
        >
          {showRaw ? "通俗" : "原始"}
        </button>
      </h3>
      {error && <p className="text-xs text-warden-danger">⚠️ {error}</p>}
      {list.length === 0 ? (
        // 拉取失败时上方已经有红字：这里不能再说「暂无待审批」（队列里也许有人等着批）
        error ? null : <p className="text-xs text-warden-fg/50">（暂无待审批）</p>
      ) : (
        <ul className="flex flex-col gap-2">
          {list.map((a) => (
            <li
              key={a.run_id + a.approval_id}
              className="rounded-xl border border-white/[0.06] bg-black/20 p-2.5 text-[13px] leading-relaxed"
            >
              {/* 会话 ID 保留：审批必须能对上"这是哪一次对话在等我批"。标题在这个面板里
                  拿不到（要多拉一次 /runs），所以这里如实给 ID，不做半真半假的替换。 */}
              <div className="text-warden-fg/80">
                会话=<span className="text-warden-warn font-mono">{a.run_id}</span>
              </div>

              <div className="text-warden-fg">
                {showRaw ? (
                  <>
                    工具=<span className="text-warden-accent font-mono">{a.tool_name}</span>
                  </>
                ) : (
                  <>
                    操作=<span className="text-warden-accent" title={a.tool_name}>
                      {toolLabel(a.tool_name)}
                    </span>
                  </>
                )}
              </div>
              {a.reason && (
                <div className="text-warden-fg/75 mt-0.5 break-words">原因：{a.reason}</div>
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

      {/* 决策历史：已批准 / 已拒绝的记录（最新在前，最多显示 5 条） */}
      {historyError && (
        <p className="text-xs text-warden-danger">⚠️ 审批历史加载失败：{historyError}</p>
      )}
      {historyList.length > 0 && (
        <div className="mt-2 pt-2 border-t border-white/[0.06]">
          <p className="text-[11px] text-warden-fg/50 mb-1">最近决策</p>
          <ul className="flex flex-col gap-1">
            {historyList.slice(0, 5).map((h, i) => (
              <li key={i} className="text-[11px] text-zinc-400 truncate">
                <span className={h.decision === "approved" ? "text-warden-ok" : "text-warden-danger"}>
                  {h.decision === "approved" ? "✓已批准" : "✗已拒绝"}
                </span>{" "}
                {showRaw ? (
                  <span className="font-mono">{h.tool_name}</span>
                ) : (
                  <span title={h.tool_name}>{toolLabel(h.tool_name)}</span>
                )}{" "}
                · {showRaw ? <span className="font-mono">{h.run_id}</span> : h.run_id}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
