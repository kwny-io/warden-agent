import { useEffect, useRef, useState } from "react";
import { api } from "./lib/api";
import ChatView from "./components/ChatView";
import ApprovalPanel from "./components/ApprovalPanel";
import InfoPanel from "./components/InfoPanel";
import ConversationList from "./components/ConversationList";
import ModelPanel from "./components/ModelPanel";

// 两侧栏宽度：最大 = 三栏默认布局；最小 = 0（收起），拖拽全程连续跟手
const LEFT_DEFAULT = 240;
const RIGHT_DEFAULT = 256;

export default function App() {
  // 当前中控台账号（USER_ID）：整个控制台以它的视角展示对话
  const [userId, setUserId] = useState(
    () => localStorage.getItem("warden.userId") || "demo-user",
  );
  const [userIdInput, setUserIdInput] = useState(userId);
  // 当前 run_id（对话）；用户在顶栏输入 USER_ID + ＋ 创建/切换账号
  const [runId, setRunId] = useState("run-demo");
  const [approvalTick, setApprovalTick] = useState(0);
  // 布局：左对话栏折叠状态；右信息栏三栏常驻，按钮可弹跳收起/展开
  const [leftWidth, setLeftWidth] = useState(240);
  const [rightWidth, setRightWidth] = useState(256);
  const [dragging, setDragging] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<{ side: "left" | "right" } | null>(null);

  // 换账号时确保它已登记（幂等），对话框归属到它名下
  useEffect(() => {
    api.createUser(userId).catch(() => {});
  }, [userId]);

  const applyUser = async () => {
    const id = userIdInput.trim();
    if (!id || id === userId) return;
    try {
      await api.createUser(id); // 幂等：存在即切换，不存在即创建
      localStorage.setItem("warden.userId", id);
      setUserId(id);
      setApprovalTick((t) => t + 1);
    } catch (e) {
      console.error(e);
    }
  };

  const toggleRight = () => setRightWidth((w) => (w === 0 ? RIGHT_DEFAULT : 0));

  const switchRun = (id?: string) => {
    const next = (id ?? "run-demo").trim() || "run-demo";
    setRunId(next);
    setApprovalTick((t) => t + 1);
  };

  // 新对话创建流程：展开左栏并打开创建输入行（ID 由用户输入，不再随机生成）
  const [createOpen, setCreateOpen] = useState(false);
  const openCreate = () => {
    setLeftWidth(LEFT_DEFAULT);
    setCreateOpen(true);
  };

  // 拖拽调宽：按下后监听 window 的 mousemove/mouseup
  const startDrag = (side: "left" | "right") => {
    dragRef.current = { side };
    setDragging(true);
  };

  useEffect(() => {
    if (!dragging) return;
    const onMove = (e: MouseEvent) => {
      const d = dragRef.current;
      const box = containerRef.current?.getBoundingClientRect();
      if (!d || !box) return;
      const raw = d.side === "left" ? e.clientX - box.left : box.right - e.clientX;
      // 0（收起）～ 默认宽（三栏布局）连续可调；贴近边缘 24px 内吸附归零
      const w = raw < 24 ? 0 : Math.min(raw, d.side === "left" ? LEFT_DEFAULT : RIGHT_DEFAULT);
      if (d.side === "left") setLeftWidth(w);
      else setRightWidth(w);
    };
    const onUp = () => {
      dragRef.current = null;
      setDragging(false);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [dragging]);

  const bothCollapsed = leftWidth === 0 && rightWidth === 0;

  return (
    <div className="h-full flex flex-col text-warden-fg">
      {/* 悬浮顶栏 */}
      <div className="px-4 pt-4 shrink-0">
        <header className="mx-auto max-w-7xl rounded-2xl border border-white/[0.06] bg-white/[0.03] backdrop-blur-xl px-4 py-2.5 flex items-center gap-3 shadow-lg shadow-black/30">
          <button
            onClick={() => setLeftWidth((w) => (w === 0 ? LEFT_DEFAULT : 0))}
            title="对话列表"
            className={`btn-sheen w-8 h-8 shrink-0 rounded-lg border transition ${
              leftWidth > 0
                ? "border-warden-accent/70 text-zinc-100"
                : "border-slate-600/50 text-warden-fg/50 hover:text-warden-fg hover:border-slate-500"
            }`}
          >
            ☰
          </button>
          <div className="w-8 h-8 rounded-lg border border-slate-600/50 bg-slate-800/60 flex items-center justify-center text-base">
            🧠
          </div>
          <h1 className="text-base font-semibold tracking-tight">Warden Agent</h1>
          <span className="hidden sm:inline text-[10px] font-normal text-warden-fg/50 border border-slate-600/50 px-2 py-0.5 rounded-full">
            Agent 运行时
          </span>
          <div className="ml-auto flex items-center gap-2 text-sm">
            <span className="text-warden-fg/50 text-xs hidden sm:inline">USER_ID</span>
            <input
              value={userIdInput}
              onChange={(e) => setUserIdInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && applyUser()}
              className="w-40 sm:w-44 bg-black/30 border border-slate-600/50 rounded-full px-3 py-1.5 font-mono text-xs outline-none focus:border-warden-accent/70 transition"
              placeholder="demo-user"
            />
            <button
              onClick={applyUser}
              title={userIdInput === userId ? "当前账号" : "创建 / 切换到该 USER_ID"}
              className={`btn-sheen w-8 h-8 rounded-full border text-base leading-none transition ${
                userIdInput === userId
                  ? "border-warden-accent/70 text-warden-accent bg-warden-accent/10"
                  : "border-slate-600/50 text-warden-fg/70 hover:text-warden-fg hover:border-slate-500"
              }`}
            >
              ＋
            </button>
            <button
              onClick={toggleRight}
              title="信息栏"
              className={`btn-sheen px-3 py-1.5 rounded-full border text-xs transition ${
                rightWidth > 0
                  ? "border-warden-accent/70 text-zinc-100"
                  : "border-slate-600/50 text-warden-fg/50 hover:text-warden-fg hover:border-slate-500"
              }`}
            >
              信息 <span className="inline-block text-[10px]">{rightWidth > 0 ? "»" : "«"}</span>
            </button>
          </div>
        </header>
      </div>

      {/* 主体：左对话栏 ⇄ 中聊天 ⇄ 右信息栏，分隔条可拖拽调宽，挤到底自动收起 */}
      <div
        ref={containerRef}
        className={`flex-1 min-h-0 w-full mx-auto flex px-4 py-4 ${
          dragging ? "select-none" : ""
        } ${bothCollapsed ? "max-w-none" : "max-w-7xl"}`}
      >
        {/* 左：对话栏（wrapper 用 flex，让内层面板撑满与聊天框齐高） */}
        <div
          className={`hidden md:flex shrink-0 min-h-0 overflow-hidden transition-[width] duration-300 ease-out ${
            dragging ? "transition-none" : ""
          }`}
          style={{ width: leftWidth }}
        >
          <ConversationList
            userId={userId}
            activeRunId={runId}
            onSwitch={(id) => switchRun(id)}
            onCollapse={() => {
              setLeftWidth(0);
              setCreateOpen(false);
            }}
            createOpen={createOpen}
            onOpenCreate={openCreate}
            onCreateDone={() => setCreateOpen(false)}
            onDeleted={(deletedId, remaining) => {
              // 删掉的是当前会话 → 切到该账号剩下的第一个，没有就开个新 ID
              if (deletedId === runId) {
                switchRun(remaining[0]?.run_id ?? `run-${Date.now().toString(36)}`);
              }
            }}
          />
        </div>
        {leftWidth > 0 && (
          <div
            onMouseDown={() => startDrag("left")}
            title="拖拽调整宽度"
            className="w-1.5 shrink-0 self-stretch cursor-col-resize rounded-full hover:bg-warden-accent/30 active:bg-warden-accent/60 transition-colors"
          />
        )}
        {leftWidth === 0 && (
          <div
            onClick={() => setLeftWidth(LEFT_DEFAULT)}
            title="展开对话栏"
            className="w-2 shrink-0 self-stretch cursor-pointer rounded-full hover:bg-white/[0.08] transition"
          />
        )}

        {/* 中：聊天 */}
        <main className="flex-1 min-w-0 min-h-0 flex">
          <ChatView
            runId={runId}
            userId={userId}
            onApprovalAction={() => setApprovalTick((t) => t + 1)}
            onToggleRail={() =>
              setLeftWidth((w) => (w === 0 ? LEFT_DEFAULT : 0))
            }
            railOpen={leftWidth > 0}
          />
        </main>

        {/* 右：信息栏 */}
        {rightWidth > 0 && (
          <div
            onMouseDown={() => startDrag("right")}
            title="拖拽调整宽度"
            className="w-1.5 shrink-0 self-stretch cursor-col-resize rounded-full hover:bg-warden-accent/30 active:bg-warden-accent/60 transition-colors"
          />
        )}
        {rightWidth === 0 && (
          <div
            onClick={() => setRightWidth(RIGHT_DEFAULT)}
            title="展开信息栏"
            className="w-2 shrink-0 self-stretch cursor-pointer rounded-full hover:bg-white/[0.08] transition"
          />
        )}
        <aside
          className={`shrink-0 min-h-0 overflow-hidden transition-[width] duration-300 ease-out ${
            dragging ? "transition-none" : ""
          }`}
          style={{ width: rightWidth }}
        >
          <div className="w-full min-w-[236px] h-full flex flex-col gap-3 overflow-y-auto pr-1">
            <ApprovalPanel key={approvalTick} />
            <InfoPanel runId={runId} />
            <ModelPanel />
          </div>
        </aside>
      </div>

      <footer className="pb-2 text-center text-[11px] text-warden-fg/30 shrink-0">
        Warden Agent · 可恢复 / 可治理 / 可部署的 Agent 运行时
      </footer>
    </div>
  );
}
