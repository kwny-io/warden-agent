// ChatView：核心对话区。支持两种模式：
//  - 流式（默认）：POST /chat/stream/{run_id} 读 SSE，逐字渲染打字机效果
//  - 非流式：POST /chat/{run_id} 等一次性结果
// 同时展示审批请求（needs_approval）内联的批准/拒绝按钮。

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { StreamEvent } from "../lib/types";
import { api } from "../lib/api";
import { toChatMessages } from "../lib/guards";
import { nextId, toDisplayMsgs, type DisplayMsg } from "../lib/display";
import MarkdownText from "./MarkdownText";

/** 工具结果默认折叠到这么多行 / 字符（展开后看全，不隐藏）。 */
const COLLAPSED_LINES = 6;
const COLLAPSED_CHARS = 600;

/** 判定"已经在底部"的容差（px）：亚像素与滚动条宽度会带来 1~2px 误差。 */
const NEAR_BOTTOM_PX = 8;

const SUGGESTIONS = [
  "查一下上海天气",
  "帮我算 23 × 47",
  "记住我喜欢喝美式咖啡",
  "总结一个项目的上线风险",
];

// 类型从 lib/display 再导出：气泡形状与转换逻辑放在一起，这里只做转发。
export type { DisplayMsg };

export default function ChatView({
  runId,
  userId,
  onApprovalAction,
  onToggleRail,
  railOpen,
  refreshTick = 0,
}: {
  runId: string;
  userId: string; // 当前中控台账号：新会话归属它
  onApprovalAction: () => void;
  onToggleRail: () => void;
  railOpen: boolean;
  // 自增计数：“别处发生了审批决策”的信号（见下面那个 effect）。
  refreshTick?: number;
}) {
  const [msgs, setMsgs] = useState<DisplayMsg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 连接中断（流没说完就断了）：null = 没中断；"resuming" = 正在续读；
  // "lost" = 没取回。断线不等于那一轮白跑了，所以要提示 + 自动续读，不能静默收尾。
  const [disconnected, setDisconnected] = useState<"resuming" | "lost" | null>(null);
  // 正在提交的审批 id：防止同一个（或两个不同）审批被重复提交，也给按钮一个禁用态
  const [actingId, setActingId] = useState<string | null>(null);
  const activeBubbleRef = useRef<number | null>(null);
  const msgBoxRef = useRef<HTMLDivElement>(null);
  // 「还在跟随最新一条」：默认跟随。用户把视口往上滚开就停止跟随（别人正在看历史，
  // 别把他拽下去）；他再发一条消息、或自己滚回底部，就重新跟随。
  // 用 ref 不用 state：滚动事件里读它不该触发渲染（每帧一次的重渲染只会让滚动更卡）。
  const followRef = useRef(true);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  // 在途流式请求的取消句柄：卸载时 abort，底层 reader 随之 cancel
  const abortRef = useRef<AbortController | null>(null);
  // 在途流的序号：「当前这一条」才有权改 busy / 清句柄。停止后立刻再发一条时，
  // 旧流的 finally 会晚到，不加这个门就会把新流的状态一起抹掉。
  const streamSeq = useRef(0);
  // 卸载守卫：慢请求 / 流式回调都可能晚到，卸载后一律不再 setState
  const aliveRef = useRef(true);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
      abortRef.current?.abort();
    };
  }, []);

  // 审批决策可能来自**侧栏审批队列**而不是聊天气泡里的按钮：那条路径上 ChatView 毫不知情，
  // 但它已经让服务端把这一轮接着跑完了（/approve 会写出最终回答）。
  // App 用一个自增的 refreshTick 告诉我们"有决策发生了"——这时从服务端把会话重拉一遍。
  // 不这么做的话，用户从队列批完只看到队列少了一格，聊天区还停在那句"需要审批"，
  // 看上去就像"我批了，但什么都没发生"。
  useEffect(() => {
    if (refreshTick === 0) return; // 初次挂载：runId 那个 effect 已经拉过了
    if (abortRef.current) return; // 在途流式请求会把结果自己写进来，不要抢（用 ref 判断，不是状态）
    void reloadFromServer();
    // 只认 refreshTick 的变化：runId / 函数身份变化不该触发重拉
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshTick]);

  const scrollToBottom = useCallback(() => {
    requestAnimationFrame(() => {
      const el = msgBoxRef.current;
      if (!el) return;
      if (!followRef.current) return; // 用户已经滚上去看历史了，不要抢他的视口
      el.scrollTop = el.scrollHeight;
    });
  }, []);

  // 滚动位置 → 跟随开关。
  // 为什么要有它：没有这个门的时候，每次 delta 都会把视口拽回底部——用户在助手
  // 长回答的中途想往上翻两句是做不到的（刚滚上去就被拽下来）。
  useEffect(() => {
    const el = msgBoxRef.current;
    if (!el) return;
    const onScroll = () => {
      followRef.current =
        el.scrollTop + el.clientHeight >= el.scrollHeight - NEAR_BOTTOM_PX;
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, []);

  // 消息变了就把视口钉到最新一条——**在提交之后**钉。
  //
  // 为什么不能用"在 `.then()` 里 requestAnimationFrame 钉一次"那种写法（原来就是）：
  // `setMsgs(history)` 之后 React 什么时候把 DOM 提交上去，和 rAF 什么时候跑，**没有**
  // 先后保证（React 用 MessageChannel/scheduler 调度，rAF 是另一条路径）。rAF 先跑时
  // 读到的 `scrollHeight` 还是**旧内容**的高度（刚挂载时几乎是 0），于是那条历史被渲染
  // 出来之后，视口留在顶部——用户看到的是"聊到三十轮的会话一打开停在最上面"。
  // 本机几乎总是 React 先提交，所以这个竞态只在慢机器/CI 上现形：
  // 实测 `/runs/36960948354` 的 e2e 就是这么挂的（`isPinnedToBottom` 15s 一直是 false）。
  // useLayoutEffect 在 DOM 提交后、浏览器绘制前同步执行——读到的必然是**这一份**内容的
  // 高度，不依赖调度器谁先跑。
  useLayoutEffect(() => {
    if (!followRef.current) return;
    const el = msgBoxRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [msgs]);

  // 刷新 / 切 run 后从后端恢复历史对话。
  // system 与 tool 结果消息不入聊天区，保持和流式视图一致的观感。
  // 先过 toChatMessages 收窄：`as ChatMessage[]` 只是编译期断言，
  // 后端给 null 元素 / 非对象项时直接 for-of 会 TypeError。
  useEffect(() => {
    const ctrl = new AbortController();
    let cancelled = false;
    // 切会话时必须把**在途的流式请求**一起断掉。ChatView 不随 runId 重挂载，
    // 只有历史请求（下面那个 ctrl）被 abort 了：旧 run 留下来的流会继续把
    // delta/final 写进新会话的消息列表（跨会话串号，治理台最不能接受的事），
    // 且 busy 一直是 true，新会话输入框 disabled，用户根本发不出话。
    streamSeq.current++;
    abortRef.current?.abort();
    abortRef.current = null;
    setBusy(false);
    setMsgs([]);
    setError(null);
    activeBubbleRef.current = null;
    // 换会话 = 新的一段对话：重新打开跟随。不重置的话，在上一个会话里往上翻过历史的人
    // 切到新会话时会带着"已停止跟随"过去，新会话打开就停在顶部——与 CI 那条失败
    // 同一个可见症状（"我新开的会话怎么停在最上面"）。
    followRef.current = true;
    api
      .messages(runId, { signal: ctrl.signal })
      .then((raw) => {
        if (cancelled) return;
        // toChatMessages 先做收窄（服务端可能返回垃圾），toDisplayMsgs 与断线兜底同一份逻辑
        setMsgs(toDisplayMsgs(toChatMessages(raw)));
        // 这里**不再**自己钉一次：上屏后的钉底由下面的 useLayoutEffect 统一负责
        // （它在 DOM 提交之后、绘制之前执行）。两处各钉一次迟早会分叉，
        // 而且"在 .then 里 rAF 钉一次"会把顺序押给调度器——正是 CI 挂掉的原因。
      })
      .catch(() => {});
    return () => {
      cancelled = true;
      ctrl.abort(); // 切 run / 卸载时取消在途请求
    };
  }, [runId, scrollToBottom]);

  const patchById = (
    id: number,
    patch: Partial<DisplayMsg> | ((prev: DisplayMsg) => Partial<DisplayMsg>),
  ) =>
    setMsgs((prev) =>
      prev.map((m) =>
        m.id === id ? { ...m, ...(typeof patch === "function" ? patch(m) : patch) } : m,
      ),
    );

  const push = (msg: DisplayMsg) => {
    setMsgs((prev) => [...prev, msg]);
    scrollToBottom();
  };

  const handleStreamEvent = (ev: StreamEvent) => {
    if (!aliveRef.current) return; // 组件已卸载：别再往 state 里写
    switch (ev.type) {
      case "delta": {
        // 把增量追加到当前 assistant 气泡尾部（打字机）
        const id = activeBubbleRef.current;
        if (id === null) {
          const nid = nextId();
          activeBubbleRef.current = nid;
          push({ id: nid, role: "assistant", text: ev.text });
        } else {
          patchById(id, (prev) => ({ text: prev.text + ev.text }));
        }
        scrollToBottom();
        break;
      }
      case "tool": {
        const nid = nextId();
        // 工具调用是独立的居中小气泡：完结当前文字气泡，
        // 让后续 final 开新气泡，而不是把最终回答灌进工具气泡里。
        activeBubbleRef.current = null;
        push({
          id: nid,
          role: "tool",
          toolKind: "call",
          text: `🔧 调用工具：${ev.name}`,
        });
        scrollToBottom();
        break;
      }
      case "tool_result": {
        // 工具真执行完了：结果原样上屏（长文本在 MsgBubble 里默认折叠）。
        // 以前这条信息只存在库里和模型的上下文里：用户只看到“调用工具”，
        // 工具到底输出了什么、成功还是失败，界面上一点都看不到。
        activeBubbleRef.current = null;
        push({
          id: nextId(),
          role: "tool",
          toolKind: "result",
          toolOk: ev.ok,
          text: ev.text,
        });
        scrollToBottom();
        break;
      }
      case "tool_hint": {
        // 调用前校验拦下：**这次没执行**，把提示喂回模型让它换招。
        // 必须上屏：否则用户会以为工具已经跑过了（静默丢弃 = 信息失真）。
        activeBubbleRef.current = null;
        push({
          id: nextId(),
          role: "tool",
          toolKind: "hint",
          text: `⏸ 未执行（先修正调用）：${ev.text}`,
        });
        scrollToBottom();
        break;
      }
      case "final": {
        const id = activeBubbleRef.current;
        if (id !== null) {
          patchById(id, { text: ev.text });
          activeBubbleRef.current = null;
        } else {
          push({ id: nextId(), role: "assistant", text: ev.text });
        }
        scrollToBottom();
        break;
      }
      case "needs_approval": {
        const nid = nextId();
        activeBubbleRef.current = null;
        push({
          id: nid,
          role: "tool",
          text: "",
          approval: ev.approval,
        });
        // 审批是**必须被看见**的事件：这条不清掉，run 就停在等审批上；用户看不到卡片
        // 就会以为"模型挂了"。所以这里直接**恢复跟随**，让上面那个 useLayoutEffect
        // （提交后执行）去钉底——不再另起一条 rAF 钉一次：rAF 可能跑在提交之前，
        // 钉的是"还没有这张卡片"时的高度，同一个时序坑换个地方又出现一次。
        followRef.current = true;
        break;
      }
      case "disconnected": {
        // 流断了，但后端那一轮很可能还在跑：保留已渲染的部分，提示 + 续读（见
        // resumeAfterDisconnect）。不要往聊天区塞红条——这通常不是用户的错。
        setDisconnected("resuming");
        activeBubbleRef.current = null;
        break;
      }
      case "error": {
        setError(ev.message);
        activeBubbleRef.current = null;
        break;
      }
      case "start":
      default:
        break;
    }
  };

  /**
   * 断线续读：连接断了不等于那一轮白跑了——后端仍在跑完，并把最终回答**同时**发到
   * 事件总线（见 src/warden_agent/web/server.py 的 chat_stream）。连回
   * /events/{run_id} 就能把同一份结果捡回来：不重跑模型、不重复扣 token、不丢掉
   * 用户已经等了几十秒的那一次回答。取不到就退化成「重新加载会话」。
   */
  async function resumeAfterDisconnect(mySeq: number) {
    const stillMine = () => aliveRef.current && streamSeq.current === mySeq;
    const ctrl = new AbortController();
    abortRef.current = ctrl; // 让「停止」按钮也能提前掐掉续读（它同样是一条长连接）
    try {
      let got = false;
      await api.watchEvents(
        runId,
        (ev) => {
          if (!stillMine()) return;
          got = true;
          handleStreamEvent(ev);
        },
        { signal: ctrl.signal, timeoutMs: 120_000 },
      );
      if (stillMine()) setDisconnected(got ? null : "lost");
    } catch {
      if (stillMine()) setDisconnected("lost");
    } finally {
      if (abortRef.current === ctrl) abortRef.current = null;
      if (stillMine()) setBusy(false);
    }
  }

  /** 兜底：从服务端拉一次已保存的会话记录（断线且续读失败时用）。
   *
   * 与初次加载走**同一个** toDisplayMsgs——两条路径一旦各写一份，就会慢慢长成
   * 两种渲染（历史上就真的长成了：一边显示工具结果、一边不显示）。
   */
  async function reloadFromServer() {
    try {
      const list = await api.messages(runId);
      setMsgs(toDisplayMsgs(toChatMessages(list)));
      setDisconnected(null);
      setError(null);
      scrollToBottom();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }
  const handleApprovalError = (e: unknown) => {
    const msg = e instanceof Error ? e.message : String(e);
    if (msg.includes("没有等待审批")) {
      setMsgs((prev) => prev.filter((m) => !m.approval));
      onApprovalAction();
    } else {
      setError(msg);
    }
  };

  const doApprove = async (approvalId: string) => {
    setActingId(approvalId);
    try {
      // 后端 /approve/{run_id} 按会话 run 批准（当前 runId 即该会话）
      const r = await api.approve(runId);
      // 批准后：清掉审批气泡，若拿到 final 文本就展示
      setMsgs((prev) => prev.filter((m) => !(m.approval && m.approval.approval_id === approvalId)));
      if (r.text) push({ id: nextId(), role: "assistant", text: r.text });
      onApprovalAction();
    } catch (e) {
      handleApprovalError(e);
    } finally {
      if (aliveRef.current) setActingId(null);
    }
  };

  const doReject = async (approvalId: string) => {
    setActingId(approvalId);
    try {
      const r = await api.reject(runId);
      setMsgs((prev) => prev.filter((m) => !(m.approval && m.approval.approval_id === approvalId)));
      if (r.text) push({ id: nextId(), role: "assistant", text: r.text });
      onApprovalAction();
    } catch (e) {
      handleApprovalError(e);
    } finally {
      if (aliveRef.current) setActingId(null);
    }
  };

  const send = async () => {
    const text = input.trim();
    if (!text || busy) return;
    setInput("");
    if (inputRef.current) inputRef.current.style.height = "auto";
    setError(null);
    setDisconnected(null);
    // 用户主动发消息 = 明确的"我要看最新"：重新打开跟随开关（他刚才可能正翻历史）。
    // 不做这一步的话，往上翻过历史的人发完消息只能看到自己的气泡落在视口外，
    // 页面看起来像"没反应"——那正是长会话用例里第 3 段要抓的东西。
    followRef.current = true;
    push({ id: nextId(), role: "user", text });
    setBusy(true);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    const mySeq = ++streamSeq.current;
    // 会话归属守卫：事件只在「我发起的这一条流」仍然有效时才处理。
    // 切会话/点停止/又发了一条都会让 seq 变化，旧流的迟到事件一律丢弃。
    // 断线标记：流「正常结束」却没给终态事件时由 api 层报出（见 lib/api.ts）。
    let brokeOff = false;
    const onEvent = (ev: StreamEvent) => {
      if (!aliveRef.current || streamSeq.current !== mySeq) return;
      if (ev.type === "disconnected") brokeOff = true;
      handleStreamEvent(ev);
    };
    try {
      await api.streamChat(runId, text, onEvent, userId, { signal: ctrl.signal });
    } catch (e) {
      if (aliveRef.current && streamSeq.current === mySeq) {
        setError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      // 只有「当前这一条」的收尾才能改状态：
      //  · 旧流晚到的 finally 若无条件 `abortRef.current = null`，会把新流的 controller
      //    搞丢（停止按钮对第二条消息直接失灵）；无条件 `setBusy(false)` 又会让正在
      //    跑的第二条流看起来空闲，用户能再发第三条，两条回答交错写进同一个气泡。
      if (streamSeq.current === mySeq) {
        abortRef.current = null;
        if (aliveRef.current) {
          setBusy(false);
          scrollToBottom();
        }
      }
      // 断线自动续读：只认「我这一条」的断线，且用户没有在这中间点停止 / 切会话
      // （seq 变了就不续）。放 finally 里保证不管上面哪条路径都会试一次。
      if (brokeOff && aliveRef.current && streamSeq.current === mySeq) {
        void resumeAfterDisconnect(mySeq);
      }
    }
  };

  // 用户主动打断：中断在途 SSE（后端按客户端断开收尾），立刻恢复输入可用。
  // SSE 一旦断开，后端不再往回送 delta，已经渲染的部分保留在聊天区里。
  const stop = () => {
    streamSeq.current++; // 作废当前流：它迟到收尾不再改状态
    const ctrl = abortRef.current;
    abortRef.current = null;
    ctrl?.abort();
    setBusy(false);
    scrollToBottom();
  };

  // 多行输入框自适应高度（上限 160px，再多出滚动）
  const autoGrow = (el: HTMLTextAreaElement) => {
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  };

  return (
    <section className="flex flex-col flex-1 min-h-0 overflow-hidden rounded-2xl border border-white/[0.07] bg-[#141416]/90 backdrop-blur-md">
      {/* 面板头部：对话列表开关 + 状态标签 */}
      <header className="shrink-0 px-4 py-2.5 border-b border-white/[0.06] flex items-center gap-2">
        <button
          onClick={onToggleRail}
          title={railOpen ? "收起对话列表" : "展开对话列表"}
          aria-label={railOpen ? "收起对话列表" : "展开对话列表"}
          className={`w-6 h-6 rounded-md text-sm leading-none transition ${
            railOpen
              ? "text-warden-accent hover:bg-white/[0.06]"
              : "text-zinc-500 hover:text-zinc-300 hover:bg-white/[0.06]"
          }`}
        >
          {railOpen ? "⟨" : "☰"}
        </button>
        <span className="text-xs font-semibold tracking-[0.2em] text-zinc-200">状态</span>
        <span className="ml-auto text-[10px] font-mono tracking-widest text-zinc-600">
          WARDEN CONSOLE
        </span>
      </header>

      {/* 消息区：居中对话列（ZCode 式排版） */}
      <div
        ref={msgBoxRef}
        className="flex-1 min-h-0 overflow-y-auto px-6 py-5 flex flex-col"
        // 流式新增的内容是“静默变更”，读屏软件默认感知不到；polite 让它读完当前句
        // 再报新增，不打断用户正在听的内容。
        aria-live="polite"
        aria-relevant="additions text"
      >
        {msgs.length === 0 && (
          <div className="m-auto flex flex-col items-center gap-5 text-center py-10">
            <div className="w-14 h-14 rounded-xl border border-zinc-700 bg-zinc-800/50 flex items-center justify-center text-2xl text-zinc-400">
              ❯_
            </div>
            <div>
              <p className="text-base font-medium text-zinc-200">有什么可以帮你？</p>
              <p className="text-sm text-zinc-500 mt-1">高危操作会先暂停，等你批准后再执行。</p>
            </div>
            <div className="flex flex-wrap justify-center gap-2 max-w-md">
              {SUGGESTIONS.map((s) => (
                <button
                  key={s}
                  onClick={() => {
                    setInput(s);
                    inputRef.current?.focus();
                  }}
                  className="btn-sheen px-3.5 py-1.5 rounded-md text-xs text-zinc-400 border border-zinc-700 hover:border-zinc-500 hover:text-zinc-200 transition"
                >
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}
        {msgs.length > 0 && (
          <div className="mx-auto w-full max-w-[720px] flex flex-col gap-4">
            {msgs.map((m) => (
              <MsgBubble
                key={m.id}
                msg={m}
                busy={m.approval !== undefined && actingId === m.approval.approval_id}
                onApprove={doApprove}
                onReject={doReject}
              />
            ))}
          </div>
        )}
      </div>

      {disconnected && (
        <div
          role="status"
          className="shrink-0 mx-6 mb-1 text-sm text-amber-300 bg-amber-400/10 border border-amber-400/40 rounded-xl px-3 py-2 flex items-center gap-3"
        >
          {disconnected === "resuming" ? (
            <span>🔌 连接中断，正在从服务端续读本轮回复…</span>
          ) : (
            <>
              <span>🔌 连接中断，本轮回复未能取回。</span>
              <button
                type="button"
                className="shrink-0 underline decoration-dotted hover:text-amber-200"
                onClick={() => void reloadFromServer()}
              >
                重新加载会话
              </button>
            </>
          )}
        </div>
      )}

      {error && (
        <div
          role="alert"
          className="shrink-0 mx-6 mb-1 text-sm text-warden-danger bg-warden-danger/10 border border-warden-danger/40 rounded-xl px-3 py-2"
        >
          ⚠️ {error}
        </div>
      )}

      {/* 输入区：底部悬浮控制台输入端（居中列） */}
      <div className="shrink-0 px-6 pb-4 pt-1">
        <div className="mx-auto w-full max-w-[720px]">
          <div className="rounded-2xl border border-white/[0.08] bg-[#1c1c1f]/90 shadow-[0_8px_28px_rgba(0,0,0,0.4)] p-2.5 flex items-end gap-2 transition-all duration-300 focus-within:border-warden-accent/70 focus-within:shadow-[inset_0_0_24px_rgba(94,129,172,0.08),0_8px_28px_rgba(0,0,0,0.4)]">
            <span className="pl-1.5 pb-2 text-zinc-500 text-sm leading-none select-none">❯</span>
            <textarea
              ref={inputRef}
              value={input}
              onChange={(e) => {
                autoGrow(e.target);
                setInput(e.target.value);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  send();
                }
              }}
              aria-label="给 Warden Agent 发消息"
              placeholder="给 Warden Agent 发消息…"
              disabled={busy}
              rows={1}
              className="flex-1 resize-none bg-transparent px-2 py-1.5 outline-none text-zinc-100 placeholder:text-zinc-600 disabled:opacity-60"
            />
            <button
              onClick={send}
              disabled={busy || !input.trim()}
              aria-label="发送"
              title="发送（Enter）"
              className="btn-sheen w-9 h-9 shrink-0 rounded-full border border-zinc-600 text-zinc-300 hover:border-zinc-400 hover:text-white disabled:opacity-40 transition flex items-center justify-center"
            >
              {busy ? (
                <span className="inline-block w-3.5 h-3.5 border-2 border-zinc-600 border-t-zinc-200 rounded-full animate-spin" />
              ) : (
                <svg viewBox="0 0 24 24" className="w-4 h-4 translate-x-[1px]" fill="currentColor">
                  <path d="M3.4 20.4l17.4-7.5a1 1 0 000-1.8L3.4 3.6a.9.9 0 00-1.3 1L4 11l9 1-9 1-1.9 6.4a.9.9 0 001.3 1z" />
                </svg>
              )}
            </button>
            {busy && (
              <button
                onClick={stop}
                aria-label="停止生成"
                title="停止生成（会断开与后端的流式连接，已生成的内容保留）"
                className="btn-sheen w-9 h-9 shrink-0 rounded-full border border-warden-danger/60 text-warden-danger hover:bg-warden-danger/10 transition flex items-center justify-center"
              >
                <span className="inline-block w-3 h-3 bg-current rounded-[2px]" />
              </button>
            )}
          </div>
          <p className="text-[11px] text-zinc-600 text-center mt-2">
            Enter 发送 · Shift+Enter 换行
          </p>
        </div>
      </div>
    </section>
  );
}

/**
 * 工具结果气泡：多行输出默认折叠。
 *
 * 「默认折叠」不是审美选择：工具输出动辄几百行（拉日志、列目录、diff），
 * 直接展开会让长会话的 DOM 节点数与滚动重排失控；折叠后审计时点「展开」仍能看全——
 * **不隐藏，只收起**。`ok === false` 用琥珀色标出，和成功的灰区分开。
 */
function ToolResultBubble({ text, ok }: { text: string; ok: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const lines = text.split("\n");
  const tooLong = lines.length > COLLAPSED_LINES || text.length > COLLAPSED_CHARS;
  const clipped =
    !expanded && tooLong
      ? lines.slice(0, COLLAPSED_LINES).join("\n").slice(0, COLLAPSED_CHARS)
      : text;
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center gap-1.5 text-[11px]">
        <span className={ok ? "text-zinc-500" : "text-amber-400"}>{ok ? "⚙" : "⚠"}</span>
        <span className={ok ? "text-zinc-500" : "text-amber-300"}>
          {ok ? "工具结果" : "工具执行失败"}
        </span>
      </div>
      <pre className="whitespace-pre-wrap break-all font-mono text-xs text-zinc-300">
        {clipped}
      </pre>
      {tooLong && (
        <button
          type="button"
          className="self-start text-[11px] text-zinc-400 underline decoration-dotted hover:text-zinc-200"
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? "收起" : `展开全部（共 ${lines.length} 行）`}
        </button>
      )}
    </div>
  );
}

function MsgBubble({
  msg,
  busy,
  onApprove,
  onReject,
}: {
  msg: DisplayMsg;
  /** 该气泡的审批正在提交（禁用按钮，避免重复提交） */
  busy: boolean;
  onApprove: (id: string) => void;
  onReject: (id: string) => void;
}) {
  // 审批是治理动作，误点即执行高危工具：先把按钮切成“确认”再真发，并禁止重复提交。
  const [confirming, setConfirming] = useState<null | "approve" | "reject">(null);
  useEffect(() => {
    if (!confirming) return;
    // 4s 没确认就退回初始态，避免把“已点过一次”一直挂在界面上
    const t = setTimeout(() => setConfirming(null), 4000);
    return () => clearTimeout(t);
  }, [confirming]);

  // 审批气泡优先级最高
  if (msg.approval) {
    const approvalId = msg.approval.approval_id;
    return (
      <div className="self-start w-full rounded-xl border border-warden-warn/40 bg-[#1d1b12] p-3.5 text-sm animate-[fadeInUp_.25s_ease_both]">
        <div className="font-medium text-warden-warn mb-1">
          ⚠️ 需要审批：<span className="text-zinc-100">{msg.approval.tool_name}</span>
        </div>
        <div className="text-zinc-400 font-mono text-xs my-1 break-all">
          参数：{JSON.stringify(msg.approval.arguments)}
        </div>
        <div className="text-zinc-500 text-xs mb-2.5">原因：{msg.approval.reason}</div>
        <div className="flex gap-2">
          {confirming ? (
            <>
              <button
                onClick={() => {
                  const which = confirming;
                  setConfirming(null);
                  if (which === "approve") onApprove(approvalId);
                  else onReject(approvalId);
                }}
                disabled={busy}
                aria-busy={busy}
                className={`btn-sheen px-4 py-1.5 rounded-md font-medium text-sm transition disabled:opacity-50 ${
                  confirming === "approve"
                    ? "bg-warden-ok/80 text-[#08130f] hover:brightness-110"
                    : "bg-warden-danger/80 text-[#160705] hover:brightness-110"
                }`}
              >
                {busy ? "处理中…" : confirming === "approve" ? "确认批准？" : "确认拒绝？"}
              </button>
              <button
                onClick={() => setConfirming(null)}
                disabled={busy}
                className="btn-sheen px-3 py-1.5 rounded-md border border-zinc-600 text-zinc-300 text-sm hover:border-zinc-400 transition disabled:opacity-50"
              >
                取消
              </button>
            </>
          ) : (
            <>
              <button
                onClick={() => setConfirming("approve")}
                disabled={busy}
                aria-busy={busy}
                className="btn-sheen px-4 py-1.5 rounded-md bg-warden-ok/80 text-[#08130f] font-medium text-sm hover:brightness-110 transition disabled:opacity-50"
              >
                ✅ 批准
              </button>
              <button
                onClick={() => setConfirming("reject")}
                disabled={busy}
                aria-busy={busy}
                className="btn-sheen px-4 py-1.5 rounded-md bg-warden-danger/80 text-[#160705] font-medium text-sm hover:brightness-110 transition disabled:opacity-50"
              >
                🚫 拒绝
              </button>
            </>
          )}
        </div>
      </div>
    );
  }

  const style =
    msg.role === "user"
      ? "self-end rounded-2xl rounded-br-sm bg-[#27272a] px-4 py-2.5 text-zinc-100"
      : msg.role === "tool"
        ? "self-start w-full rounded-lg bg-[#0f0f11] border border-white/[0.06] px-3 py-2 font-mono text-xs text-zinc-400"
        : msg.role === "system"
          ? "self-center rounded-md bg-zinc-800/70 text-zinc-300 text-xs"
          : "self-start max-w-[85%] ml-2 rounded-2xl rounded-bl-sm bg-[#1a1a1f] border border-white/[0.07] px-5 py-3 text-zinc-200";

  return (
    <div
      className={`relative whitespace-pre-wrap leading-relaxed animate-[fadeInUp_.25s_ease_both] ${style}`}
    >
      {/* 指示箭头：一眼区分消息是谁发的 */}
      {msg.role === "user" && (
        <span className="absolute -right-[5px] bottom-3 w-2.5 h-2.5 rotate-45 bg-[#27272a] rounded-[2px]" />
      )}
      {msg.role === "assistant" && (
        <span className="absolute -left-[5px] bottom-3 w-2.5 h-2.5 rotate-45 bg-[#1a1a1f] rounded-[2px] border-b border-l border-white/[0.07]" />
      )}
      {msg.role === "tool" ? (
        msg.toolKind === "result" ? (
          <ToolResultBubble text={msg.text} ok={msg.toolOk !== false} />
        ) : (
          <div className="flex items-start gap-1.5">
            <span className="text-warden-accent select-none">❯</span>
            <span className="break-all">{msg.text}</span>
          </div>
        )
      ) : msg.role === "assistant" ? (
        <MarkdownText text={msg.text} />
      ) : (
        msg.text
      )}
    </div>
  );
}
