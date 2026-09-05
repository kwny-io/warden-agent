// 后端 REST API 客户端：封装对 FastAPI 的 fetch 调用。
// 开发模式下由 Vite 代理到 8000，生产模式下同源（FastAPI 托管），所以 base 留空。

import type {
  Approval,
  Capabilities,
  ChatMessage,
  ChatResponseOut,
  HealthResult,
  MemoryItem,
} from "./types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
    ...init,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = data.detail || detail;
    } catch {
      /* 保留默认 detail */
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  return (await res.json()) as T;
}

export const api = {
  /** 非流式对话：POST /chat/{run_id} */
  chat(runId: string, text: string): Promise<ChatResponseOut> {
    return request(`/chat/${encodeURIComponent(runId)}`, {
      method: "POST",
      body: JSON.stringify({ text }),
    });
  },

  /** 查 run 状态：GET /status/{run_id} */
  status(runId: string): Promise<{ run_id: string; status: string }> {
    return request(`/status/${encodeURIComponent(runId)}`);
  },

  /** 审批队列：GET /approvals */
  approvals(): Promise<Approval[]> {
    return request("/approvals");
  },

  /** 批准：POST /approve/{run_id} */
  approve(runId: string): Promise<ChatResponseOut> {
    return request(`/approve/${encodeURIComponent(runId)}`, { method: "POST" });
  },

  /** 拒绝：POST /reject/{run_id} */
  reject(runId: string): Promise<ChatResponseOut> {
    return request(`/reject/${encodeURIComponent(runId)}`, { method: "POST" });
  },

  /** 能力：GET /capabilities */
  capabilities(): Promise<Capabilities> {
    return request("/capabilities");
  },

  /** 记忆：GET /memory/{scope} */
  memory(scope: string): Promise<MemoryItem[]> {
    return request(`/memory/${encodeURIComponent(scope)}`);
  },

  /** 健康：GET /health/live 与 /health/ready */
  health(): Promise<HealthResult> {
    return request("/health/live");
  },

  /** 读取一次流式对话（SSE），把事件一一回调给 onEvent */
  async streamChat(
    runId: string,
    text: string,
    onEvent: (ev: import("./types").StreamEvent) => void,
  ): Promise<void> {
    const res = await fetch(`/chat/stream/${encodeURIComponent(runId)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (!res.ok || !res.body) {
      let detail = res.statusText;
      try {
        const data = await res.json();
        detail = data.detail || detail;
      } catch {
        /* ignore */
      }
      onEvent({ type: "error", message: `${res.status}: ${detail}` });
      return;
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx: number;
      // SSE 事件以空行分隔
      while ((idx = buf.indexOf("\n\n")) !== -1) {
        const raw = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const line = raw.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        try {
          onEvent(JSON.parse(line.slice(6)));
        } catch {
          /* 忽略坏帧 */
        }
      }
    }
  },
};

export type { Approval, Capabilities, ChatMessage, HealthResult, MemoryItem };
