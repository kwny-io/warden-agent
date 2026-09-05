import { useState } from "react";
import ChatView from "./components/ChatView";
import ApprovalPanel from "./components/ApprovalPanel";
import InfoPanel from "./components/InfoPanel";

export default function App() {
  // 默认 run_id；用户在侧栏或顶部可改
  const [runId, setRunId] = useState("run-demo");
  const [runIdInput, setRunIdInput] = useState(runId);
  const [approvalTick, setApprovalTick] = useState(0);

  return (
    <div className="min-h-screen bg-warden-bg text-warden-fg flex flex-col">
      {/* 顶栏 */}
      <header className="px-6 py-4 border-b border-warden-line flex items-center gap-3">
        <h1 className="text-lg font-semibold">
          🧠 Warden Agent
          <span className="ml-2 text-xs font-normal bg-warden-accent text-white px-2 py-0.5 rounded-full align-middle">
            React + TS + Tailwind
          </span>
        </h1>
        <div className="ml-auto flex items-center gap-2 text-sm">
          <span className="text-warden-fg/60">run_id</span>
          <input
            value={runIdInput}
            onChange={(e) => setRunIdInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                setRunId(runIdInput.trim() || "run-demo");
                setApprovalTick((t) => t + 1);
              }
            }}
            className="w-44 bg-warden-bg border border-warden-line rounded-lg px-2 py-1 font-mono text-xs focus:border-warden-accent outline-none"
            placeholder="run-demo"
          />
          <button
            onClick={() => {
              setRunId(runIdInput.trim() || "run-demo");
              setApprovalTick((t) => t + 1);
            }}
            className="px-3 py-1 rounded-lg bg-warden-panel border border-warden-line text-xs hover:border-warden-accent"
          >
            切换
          </button>
        </div>
      </header>

      {/* 主体：左聊天 + 右侧栏 */}
      <div className="flex gap-4 p-4 flex-1 min-h-0 max-w-7xl w-full mx-auto">
        <main className="flex-1 flex flex-col min-h-0">
          <ChatView runId={runId} onApprovalAction={() => setApprovalTick((t) => t + 1)} />
        </main>
        <aside className="w-72 shrink-0 flex flex-col gap-3 overflow-y-auto max-h-[85vh] pr-1">
          <ApprovalPanel key={approvalTick} />
          <InfoPanel runId={runId} />
        </aside>
      </div>

      <footer className="px-6 py-3 border-t border-warden-line text-xs text-warden-fg/40">
        Warden Agent · 可恢复 / 可治理 / 可部署的 Agent 运行时
      </footer>
    </div>
  );
}
