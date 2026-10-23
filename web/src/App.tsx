import { useCallback, useEffect, useRef, useState } from "react";
import { api, getApiKey, isApiKeyRemembered, setApiKey } from "./lib/api";
import { readRunFromUrl, writeRunToUrl } from "./lib/deepLink";
import { toUserInfos } from "./lib/guards";
import ChatView from "./components/ChatView";
import ApprovalPanel from "./components/ApprovalPanel";
import InfoPanel from "./components/InfoPanel";
import ConversationList from "./components/ConversationList";
import ModelPanel from "./components/ModelPanel";
import { PanelBoundary } from "./components/ErrorBoundary";

// 两侧栏宽度：最大 = 三栏默认布局；最小 = 0（收起），拖拽全程连续跟手
const LEFT_DEFAULT = 240;
const RIGHT_DEFAULT = 256;

export default function App() {
  // 当前中控台账号（USER_ID）：整个控制台以它的视角展示对话
  const [userId, setUserId] = useState(() => localStorage.getItem("warden.userId") || "demo-user");
  const [userIdInput, setUserIdInput] = useState(userId);
  // 访问密钥：服务端设了 WARDEN_API_KEY 时业务接口都要带 Bearer，这里填。
  // 它是**访问本服务的凭证**（相当于密码），与「模型面板里填的大模型 API Key」是两回事——
  // 原来标签叫 KEY / API KEY，用户分不清该填哪个，所以改成「访问密钥」。
  // 默认只存 sessionStorage，留空表示服务端未开鉴权。
  const [apiKeyInput, setApiKeyInput] = useState(() => getApiKey());
  // "记住密钥"：初始值取自当前存储位置（localStorage 里有东西就说明上次勾了）
  const [remember, setRemember] = useState(() => isApiKeyRemembered());
  // 当前 run_id（对话）；每次打开页面都从全新会话开始（多账号下避免串号），
  // 历史对话从左栏列表一键找回
  // 会话 ID：优先取 URL 里的深链（`?run=xxx`，可分享/可刷新），否则新开一个。
  // 不把"上次看的会话"存 localStorage：那个值可能已经被服务端清理，刷新后拿一个不存在的
  // run 去拉历史，用户看到的是一个空会话而不是"会话已不在"（比现在这样更糊涂）。
  const [runId, setRunId] = useState(
    () => readRunFromUrl(window.location.search) ?? `run-${Date.now().toString(36)}`,
  );
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

  // USER_ID 历史下拉：从数据库 users 表读取创建过的 ID，点选填入输入框
  const [userListOpen, setUserListOpen] = useState(false);
  const [knownUsers, setKnownUsers] = useState<string[]>([]);
  const refreshUsers = useCallback(() => {
    api
      .users()
      .then((list) => setKnownUsers(toUserInfos(list).map((u) => u.user_id)))
      .catch(() => {});
  }, []);
  useEffect(() => {
    refreshUsers();
  }, [refreshUsers, userId]);

  // 切换账号后：聊天区切到全新会话，避免看到别的账号的对话（首次挂载除外）
  const prevUserId = useRef(userId);
  useEffect(() => {
    if (prevUserId.current !== userId) {
      prevUserId.current = userId;
      setRunId(`run-${Date.now().toString(36)}`);
      setApprovalTick((t) => t + 1);
    }
  }, [userId]);

  const applyUser = async (id?: string) => {
    const next = (id ?? userIdInput).trim();
    if (!next || next === userId) return;
    try {
      await api.createUser(next); // 幂等：存在即切换，不存在即创建
      localStorage.setItem("warden.userId", next);
      // 输入框必须一起回写：从下拉列表点选时调用方传的是 id，输入框里还留着旧
      // 账号名——界面上显示 demo-user、实际却是 alice，再敲一次回车会切回去（
      // 记账/归属跟着错）。显示值必须永远等于真实生效的账号。
      setUserIdInput(next);
      setUserId(next);
      setApprovalTick((t) => t + 1);
    } catch (e) {
      console.error(e);
    }
  };

  const toggleRight = () => setRightWidth((w) => (w === 0 ? RIGHT_DEFAULT : 0));

  const switchRun = (id?: string) => {
    const next = (id ?? `run-${Date.now().toString(36)}`).trim();
    setRunId(next);
    setApprovalTick((t) => t + 1);
  };

  // 会话变化就写回 URL（replaceState：不新增历史条目，否则“后退”会一步步退会话）
  useEffect(() => {
    writeRunToUrl(runId);
  }, [runId]);

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
      // 上限按容器宽度的百分比封顶：大屏够宽，小屏自动收缩不会被裁切；
      // 这样对话框既能拉大（侧栏收窄收起）也能拉小（侧栏变宽）
      const maxW = Math.max(box.width * (d.side === "left" ? 0.38 : 0.45), 180);
      const w = raw < 24 ? 0 : Math.min(raw, maxW);
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
        <header className="relative z-20 mx-auto max-w-7xl rounded-2xl border border-white/[0.06] bg-white/[0.03] backdrop-blur-xl px-4 py-2.5 flex items-center gap-3 shadow-lg shadow-black/30">
          <button
            onClick={() => setLeftWidth((w) => (w === 0 ? LEFT_DEFAULT : 0))}
            title="对话列表"
            aria-label="切换对话列表"
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
              aria-label="USER_ID"
              className="w-40 sm:w-44 bg-black/30 border border-slate-600/50 rounded-full px-3 py-1.5 font-mono text-xs outline-none focus:border-warden-accent/70 transition"
              placeholder="demo-user"
            />
            {/* 访问密钥：服务端设了 WARDEN_API_KEY 时必填；留空即"服务端没开鉴权"。
                用 password 类型遮显；回车或失焦即写入，后续请求自动带 Bearer。
                默认只存 sessionStorage（关标签页即失效）；要跨重启保留得勾"记住"。 */}
            <span className="text-warden-fg/50 text-xs hidden sm:inline">访问密钥</span>
            <input
              type="password"
              value={apiKeyInput}
              onChange={(e) => setApiKeyInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && setApiKey(apiKeyInput, { remember })}
              onBlur={() => setApiKey(apiKeyInput, { remember })}
              aria-label="访问密钥"
              title="服务端设了 WARDEN_API_KEY 时填这里；留空表示服务端未开鉴权。这是访问本服务的凭证，不是大模型的 API Key"
              className="w-28 sm:w-32 bg-black/30 border border-slate-600/50 rounded-full px-3 py-1.5 font-mono text-xs outline-none focus:border-warden-accent/70 transition"
              placeholder="访问密钥"
            />
            {/* "记住密钥"：勾上才落到 localStorage（跨浏览器重启有效），默认不勾。
                明确不默认为勾：这把 key 是控制面全权凭证，而页面要渲染模型输出的 Markdown。 */}
            <label
              title="勾选后密钥写入 localStorage，浏览器重启后仍有效；不勾只在本标签页有效"
              className="flex items-center gap-1 text-[10px] text-warden-fg/50 cursor-pointer select-none"
            >
              <input
                type="checkbox"
                checked={remember}
                aria-label="记住密钥"
                onChange={(e) => {
                  const next = e.target.checked;
                  setRemember(next);
                  // 立即按新选择重写一次：不然"勾上去没反应"要等下次输入才生效
                  setApiKey(apiKeyInput, { remember: next });
                }}
                className="accent-warden-accent"
              />
              记住
            </label>
            {/* 切换按钮：点开展开已创建的 USER_ID 列表，选中即切换（可与信息按钮同排弹性布局） */}
            <div className="relative">
              <button
                onClick={() => {
                  refreshUsers();
                  setUserListOpen((v) => !v);
                }}
                title="切换 USER_ID"
                className={`btn-sheen px-3 py-1.5 rounded-full border text-xs transition ${
                  userListOpen
                    ? "border-warden-accent/70 text-zinc-100"
                    : "border-slate-600/50 text-warden-fg/70 hover:text-warden-fg hover:border-slate-500"
                }`}
              >
                切换 <span className="inline-block text-[10px]">{userListOpen ? "»" : "«"}</span>
              </button>
              {/* ID 列表：点选即切换 */}
              {userListOpen && (
                <div className="absolute right-0 top-full mt-1 w-48 rounded-xl border border-white/[0.07] bg-[#141416]/95 backdrop-blur-xl shadow-xl shadow-black/40 z-50 max-h-56 overflow-y-auto">
                  {knownUsers.length === 0 && (
                    <p className="px-3 py-2 text-xs text-zinc-500">（还没有创建过）</p>
                  )}
                  {knownUsers.map((u) => (
                    <button
                      key={u}
                      onClick={() => {
                        applyUser(u);
                        setUserListOpen(false);
                      }}
                      className={`w-full text-left px-3 py-1.5 text-xs font-mono transition ${
                        u === userId
                          ? "text-warden-accent bg-warden-accent/10"
                          : "text-zinc-300 hover:bg-white/[0.06]"
                      }`}
                    >
                      {u}
                      {u === userId && (
                        <span className="float-right text-[10px] text-warden-ok">当前</span>
                      )}
                    </button>
                  ))}
                </div>
              )}
            </div>
            <button
              onClick={toggleRight}
              title="信息栏"
              aria-label="切换信息栏"
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
        {/* 左：对话栏（wrapper 用 flex + 显式 stretch，保证面板与聊天框齐高；宽度按容器百分比封顶） */}
        <div
          className={`hidden md:flex shrink-0 self-stretch min-h-0 overflow-hidden transition-[width] duration-300 ease-out max-w-[38%] ${
            dragging ? "transition-none" : ""
          }`}
          style={{ width: leftWidth }}
        >
          {/* 折叠到 0 时**不渲染**内容，而不是留一个宽度 0 的容器：
              后者里的按钮仍然可被 Tab 聚焦、被读屏朗读（看不见却能操作），
              且 5s 轮询会接着白烧请求。展开时重挂载换取干净状态。 */}
          {leftWidth > 0 && (
            <PanelBoundary name="对话列表">
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
                onDeleted={(deletedIds, remaining) => {
                  // 删掉的**包含**当前会话 → 切到该账号剩下的第一个，没有就开个新 ID。
                  // 判「包含」而不是「等于」：批量删里当前会话很可能也在其中，
                  // 只判单个的话聊天区会停在一个已经被删掉的会话上（发消息就 404/重建空会话）。
                  if (deletedIds.includes(runId)) {
                    switchRun(remaining[0]?.run_id ?? `run-${Date.now().toString(36)}`);
                  }
                }}
              />
            </PanelBoundary>
          )}
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
          <PanelBoundary name="对话区">
            <ChatView
              runId={runId}
              userId={userId}
              onApprovalAction={() => setApprovalTick((t) => t + 1)}
              refreshTick={approvalTick}
              onToggleRail={() => setLeftWidth((w) => (w === 0 ? LEFT_DEFAULT : 0))}
              railOpen={leftWidth > 0}
            />
          </PanelBoundary>
        </main>

        {/* 右：信息栏（显式 stretch 齐高 + 百分比封顶防裁切） */}
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
          className={`flex shrink-0 self-stretch min-h-0 flex-col overflow-hidden transition-[width] duration-300 ease-out max-w-[45%] ${
            dragging ? "transition-none" : ""
          }`}
          style={{ width: rightWidth }}
        >
          {/* 同左栏：收起（宽度 0）时直接不渲染，避免“看不见但能 Tab 到”的按钮
              与后台白跑的审批/记忆轮询 */}
          {rightWidth > 0 && (
            <div className="w-full flex-1 min-h-0 flex flex-col gap-3 overflow-y-auto pr-1">
              <PanelBoundary name="审批队列">
                <ApprovalPanel
                  key={approvalTick}
                  onDecided={() => setApprovalTick((t) => t + 1)}
                />
              </PanelBoundary>
              <PanelBoundary name="运行信息">
                <InfoPanel runId={runId} />
              </PanelBoundary>
              <PanelBoundary name="模型面板">
                <ModelPanel />
              </PanelBoundary>
            </div>
          )}
        </aside>
      </div>

      <footer className="pb-2 text-center text-[11px] text-warden-fg/30 shrink-0">
        Warden Agent · 可恢复 / 可治理 / 可部署的 Agent 运行时
      </footer>
    </div>
  );
}
