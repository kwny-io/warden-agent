// 后端 REST API 客户端：封装对 FastAPI 的 fetch 调用。
// 开发模式下由 Vite 代理到 8000，生产模式下同源（FastAPI 托管），所以 base 留空。

import type {
  Approval,
  ApprovalHistoryItem,
  Capabilities,
  ChatMessage,
  ChatResponseOut,
  HealthResult,
  MemoryItem,
  ModelsInfo,
  RunInfo,
  UserInfo,
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
  chat(runId: string, text: string, userId?: string): Promise<ChatResponseOut> {
    const q = userId ? `?user_id=${encodeURIComponent(userId)}` : "";
    return request(`/chat/${encodeURIComponent(runId)}${q}`, {
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

  /** 对话记录：GET /messages/{run_id}（刷新后恢复聊天区） */
  messages(runId: string): Promise<ChatMessage[]> {
    return request(`/messages/${encodeURIComponent(runId)}`);
  },

  /** 对话列表：GET /runs?user_id=（最近活跃优先；带 user_id 只看该用户的） */
  runs(userId?: string): Promise<RunInfo[]> {
    const q = userId ? `?user_id=${encodeURIComponent(userId)}` : "";
    return request(`/runs${q}`);
  },

  /** 预创建会话：POST /runs/{run_id}?user_id=（幂等，归属当前用户） */
  createRun(runId: string, userId?: string): Promise<{ run_id: string; status: string; user_id?: string }> {
    const q = userId ? `?user_id=${encodeURIComponent(userId)}` : "";
    return request(`/runs/${encodeURIComponent(runId)}${q}`, { method: "POST" });
  },

  /** 中控台用户列表：GET /users */
  users(): Promise<UserInfo[]> {
    return request("/users");
  },

  /** 登记用户：POST /users（幂等） */
  createUser(userId: string): Promise<{ ok: boolean; user_id: string }> {
    return request("/users", {
      method: "POST",
      body: JSON.stringify({ user_id: userId }),
    });
  },

  /** 审批决策历史：GET /approvals/history（最新在前） */
  approvalHistory(): Promise<ApprovalHistoryItem[]> {
    return request("/approvals/history");
  },

  /** 删除会话：DELETE /runs/{run_id}（清掉状态/对话/审批记录） */
  deleteRun(runId: string): Promise<{ ok: boolean }> {
    return request(`/runs/${encodeURIComponent(runId)}`, { method: "DELETE" });
  },

  /** 可用模型列表 + 当前模型：GET /models */
  models(): Promise<ModelsInfo> {
    return request("/models");
  },

  /** 切换 / 导入模型：POST /models/select */
  selectModel(id: string, apiKey?: string): Promise<{ ok: boolean; current: string }> {
    return request("/models/select", {
      method: "POST",
      body: JSON.stringify({ id, api_key: apiKey || null }),
    });
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
    userId?: string,
  ): Promise<void> {
    const q = userId ? `?user_id=${encodeURIComponent(userId)}` : "";
    const res = await fetch(`/chat/stream/${encodeURIComponent(runId)}${q}`, {
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

export type { Approval, ApprovalHistoryItem, Capabilities, ChatMessage, HealthResult, MemoryItem, ModelsInfo, RunInfo };
