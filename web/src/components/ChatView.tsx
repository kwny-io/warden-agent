// ChatView：核心对话区。支持两种模式：
//  - 流式（默认）：POST /chat/stream/{run_id} 读 SSE，逐字渲染打字机效果
//  - 非流式：POST /chat/{run_id} 等一次性结果
// 同时展示审批请求（needs_approval）内联的批准/拒绝按钮。

import { useRef, useState } from "react";
import type { StreamEvent } from "../lib/types";
import { api } from "../lib/api";

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

export default function ChatView({
  runId,
  onApprovalAction,
}: {
  runId: string;
  onApprovalAction: () => void;
}) {
  const [msgs, setMsgs] = useState<DisplayMsg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const activeBubbleRef = useRef<number | null>(null);
  const msgBoxRef = useRef<HTMLDivElement>(null);

  const scrollToBottom = () => {
    requestAnimationFrame(() => {
      const el = msgBoxRef.current;
      if (el) el.scrollTop = el.scrollHeight;
    });
  };

  const patchById = (id: number, patch: Partial<DisplayMsg>) =>
    setMsgs((prev) => prev.map((m) => (m.id === id ? { ...m, ...patch } : m)));

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
        activeBubbleRef.current = nid;
        push({
          id: nid,
          role: "tool",
          text: `🔧 调用工具：${ev.name} ${JSON.stringify(ev.arguments)}`,
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

  return (
    <section className="flex flex-col gap-3 flex-1 min-h-0">
      {/* 消息区 */}
      <div
        ref={msgBoxRef}
        className="flex-1 min-h-0 overflow-y-auto flex flex-col gap-2 rounded-xl border border-warden-line bg-warden-panel p-3"
        style={{ maxHeight: "60vh" }}
      >
        {msgs.length === 0 && (
          <p className="text-warden-fg/50 text-sm m-auto">
            说点什么，比如「查一下上海天气」或「帮我算 23 × 47」
          </p>
        )}
        {msgs.map((m) => (
          <MsgBubble
            key={m.id}
            msg={m}
            onApprove={doApprove}
            onReject={doReject}
          />
        ))}
      </div>

      {error && (
        <div className="text-sm text-warden-danger bg-warden-danger/10 border border-warden-danger/40 rounded-lg px-3 py-2">
          ⚠️ {error}
        </div>
      )}

      {/* 输入栏 */}
      <div className="flex gap-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && send()}
          placeholder="输入消息…（Enter 发送）"
          disabled={busy}
          className="flex-1 bg-warden-bg border border-warden-line text-warden-fg rounded-lg px-3 py-2 outline-none focus:border-warden-accent disabled:opacity-60"
        />
        <button
          onClick={send}
          disabled={busy || !input.trim()}
          className="px-5 py-2 rounded-lg bg-warden-accent text-white font-medium disabled:opacity-50 hover:bg-warden-accent/80"
        >
          {busy ? "思考中…" : "发送"}
        </button>
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
      <div className="self-center w-full max-w-md rounded-xl border border-warden-warn bg-[#3a2f17] p-3 text-sm">
        <div className="font-medium text-warden-warn mb-1">
          ⚠️ 需要审批：<span className="text-warden-fg">{msg.approval.tool_name}</span>
        </div>
        <div className="text-warden-fg/80 font-mono text-xs my-1 break-all">
          参数：{JSON.stringify(msg.approval.arguments)}
        </div>
        <div className="text-warden-fg/70 text-xs mb-2">原因：{msg.approval.reason}</div>
        <div className="flex gap-2">
          <button
            onClick={() => onApprove(msg.approval!.approval_id)}
            className="px-4 py-1.5 rounded-md bg-warden-ok text-[#06281a] font-medium text-sm hover:brightness-95"
          >
            ✅ 批准
          </button>
          <button
            onClick={() => onReject(msg.approval!.approval_id)}
            className="px-4 py-1.5 rounded-md bg-warden-danger text-white font-medium text-sm hover:brightness-95"
          >
            🚫 拒绝
          </button>
        </div>
      </div>
    );
  }

  const style =
    msg.role === "user"
      ? "self-end bg-warden-accent text-white"
      : msg.role === "tool"
        ? "self-center bg-[#24324d] text-[#b8c6e0] text-xs"
        : msg.role === "system"
          ? "self-center bg-[#2a2140] text-[#b9a8e0] text-xs"
          : "self-start bg-[#2a2f45] text-warden-fg";

  return (
    <div className={`max-w-[80%] px-3 py-2 rounded-xl text-sm leading-relaxed whitespace-pre-wrap ${style}`}>
      {msg.role === "user" ? `你：${msg.text}` : msg.text}
    </div>
  );
}
