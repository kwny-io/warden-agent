// ChatView：核心对话区。支持两种模式：
//  - 流式（默认）：POST /chat/stream/{run_id} 读 SSE，逐字渲染打字机效果
//  - 非流式：POST /chat/{run_id} 等一次性结果
// 同时展示审批请求（needs_approval）内联的批准/拒绝按钮。

import { useEffect, useRef, useState } from "react";
import type { StreamEvent } from "../lib/types";
import { api } from "../lib/api";
import MarkdownText from "./MarkdownText";

export interface DisplayMsg {
  id: number;
  role: "user" | "assistant" | "tool" | "system";
  text: string;
  // 若这是一条"需要审批"的气泡，带上审批信息
  approval?: {
    approval_id: string;
    tool_name: string;
    arguments: Record<string, unknown>;
    reason: string;
  };
}

let seq = 0;
const nextId = () => ++seq;

const SUGGESTIONS = ["查一下上海天气", "帮我算 23 × 47", "记住我喜欢喝美式咖啡", "总结一个项目的上线风险"];

export default function ChatView({
  runId,
  onApprovalAction,
  onToggleRail,
  railOpen,
}: {
  runId: string;
  onApprovalAction: () => void;
  onToggleRail: () => void;
  railOpen: boolean;
}) {
  const [msgs, setMsgs] = useState<DisplayMsg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const activeBubbleRef = useRef<number | null>(null);
  const msgBoxRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // 刷新 / 切 run 后从后端恢复历史对话。
  // system 与 tool 结果消息不入聊天区，保持和流式视图一致的观感。
  useEffect(() => {
    let cancelled = false;
    setMsgs([]);
    setError(null);
    activeBubbleRef.current = null;
    api
      .messages(runId)
      .then((list) => {
        if (cancelled) return;
        const restored: DisplayMsg[] = [];
        for (const m of list) {
          if (m.role === "user") {
            restored.push({ id: nextId(), role: "user", text: m.content ?? "" });
          } else if (m.role === "assistant" && m.tool_call?.name) {
            restored.push({
              id: nextId(),
              role: "tool",
              text: `🔧 调用工具：${m.tool_call.name}`,
            });
          } else if (m.role === "assistant" && m.content) {
            restored.push({ id: nextId(), role: "assistant", text: m.content });
          }
        }
        setMsgs(restored);
        scrollToBottom();
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);

  const scrollToBottom = () => {
    requestAnimationFrame(() => {
      const el = msgBoxRef.current;
      if (el) el.scrollTop = el.scrollHeight;
    });
  };

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
          text: `🔧 调用工具：${ev.name}`,
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
        scrollToBottom();
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

  const doApprove = async (approvalId: string) => {
    try {
      // 后端 /approve/{run_id} 按会话 run 批准（当前 runId 即该会话）
      const r = await api.approve(runId);
      // 批准后：清掉审批气泡，若拿到 final 文本就展示
      setMsgs((prev) =>
        prev.filter((m) => !(m.approval && m.approval.approval_id === approvalId)),
      );
      if (r.text) push({ id: nextId(), role: "assistant", text: r.text });
      onApprovalAction();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const doReject = async (approvalId: string) => {
    try {
      const r = await api.reject(runId);
      setMsgs((prev) =>
        prev.filter((m) => !(m.approval && m.approval.approval_id === approvalId)),
      );
      if (r.text) push({ id: nextId(), role: "assistant", text: r.text });
      onApprovalAction();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const send = async () => {
    const text = input.trim();
    if (!text || busy) return;
    setInput("");
    if (inputRef.current) inputRef.current.style.height = "auto";
    setError(null);
    push({ id: nextId(), role: "user", text });
    setBusy(true);
    try {
      await api.streamChat(runId, text, handleStreamEvent);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      scrollToBottom();
    }
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
      >
        {msgs.length === 0 && (
          <div className="m-auto flex flex-col items-center gap-5 text-center py-10">
            <div className="w-14 h-14 rounded-xl border border-zinc-700 bg-zinc-800/50 flex items-center justify-center text-2xl text-zinc-400">
              ❯_
            </div>
            <div>
              <p className="text-base font-medium text-zinc-200">有什么可以帮你？</p>
              <p className="text-sm text-zinc-500 mt-1">
                高危操作会先暂停，等你批准后再执行。
              </p>
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
                onApprove={doApprove}
                onReject={doReject}
              />
            ))}
          </div>
        )}
      </div>

      {error && (
        <div className="shrink-0 mx-6 mb-1 text-sm text-warden-danger bg-warden-danger/10 border border-warden-danger/40 rounded-xl px-3 py-2">
          ⚠️ {error}
        </div>
      )}

      {/* 输入区：底部悬浮控制台输入端（居中列） */}
      <div className="shrink-0 px-6 pb-4 pt-1">
        <div className="mx-auto w-full max-w-[720px]">
          <div className="rounded-2xl border border-white/[0.08] bg-[#1c1c1f]/90 shadow-[0_8px_28px_rgba(0,0,0,0.4)] p-2.5 flex items-end gap-2 transition-all duration-300 focus-within:border-warden-accent/70 focus-within:shadow-[inset_0_0_24px_rgba(94,129,172,0.08),0_8px_28px_rgba(0,0,0,0.4)]">
            <span className="pl-1.5 pb-2 text-zinc-500 text-sm leading-none select-none">
              ❯
            </span>
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
          </div>
          <p className="text-[11px] text-zinc-600 text-center mt-2">
            Enter 发送 · Shift+Enter 换行
          </p>
        </div>
      </div>
    </section>
  );
}

function MsgBubble({
  msg,
  onApprove,
  onReject,
}: {
  msg: DisplayMsg;
  onApprove: (id: string) => void;
  onReject: (id: string) => void;
}) {
  // 审批气泡优先级最高
  if (msg.approval) {
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
          <button
            onClick={() => onApprove(msg.approval!.approval_id)}
            className="btn-sheen px-4 py-1.5 rounded-md bg-warden-ok/80 text-[#08130f] font-medium text-sm hover:brightness-110 transition"
          >
            ✅ 批准
          </button>
          <button
            onClick={() => onReject(msg.approval!.approval_id)}
            className="btn-sheen px-4 py-1.5 rounded-md bg-warden-danger/80 text-[#160705] font-medium text-sm hover:brightness-110 transition"
          >
            🚫 拒绝
          </button>
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
        <div className="flex items-start gap-1.5">
          <span className="text-warden-accent select-none">❯</span>
          <span className="break-all">{msg.text}</span>
        </div>
      ) : msg.role === "assistant" ? (
        <MarkdownText text={msg.text} />
      ) : (
        msg.text
      )}
    </div>
  );
}
