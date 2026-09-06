// 与后端 FastAPI 交互的类型定义（对齐 web/server.py 里的模型）

// ChatResponseOut：POST /chat/{run_id} 的返回
export interface ChatResponseOut {
  run_id: string;
  status: string;
  kind: "final" | "needs_approval" | "error";
  text?: string | null;
  approval?: Approval | null;
  messages?: ChatMessage[] | null;
}

// 一次待审批的请求
export interface Approval {
  approval_id: string;
  run_id?: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  reason: string;
}

// 一条对话消息（后端 _serialize_messages 结构）
export interface ChatMessage {
  role: string;
  content?: string | null;
  tool_call?: { id?: string; name?: string; arguments?: Record<string, unknown> } | null;
}

// SSE 流式事件 /chat/stream/{run_id}
export type StreamEvent =
  | { type: "start" }
  | { type: "delta"; text: string }
  | { type: "tool"; name: string; arguments: Record<string, unknown> }
  | { type: "final"; text: string }
  | { type: "needs_approval"; approval: Approval }
  | { type: "error"; message: string };

// GET /capabilities
export interface Capabilities {
  tools: string[];
  features: {
    memory: boolean;
    skills: string[];
    web: boolean;
    mcp_server: string | null;
  };
}

// GET /memory/{scope}
export interface MemoryItem {
  scope: string;
  key: string;
  text: string;
  status: string;
}

// 健康检查
export interface HealthResult {
  status: string;
  checks: Array<{ name: string; ok: boolean; detail?: string }>;
}

// GET /runs 对话列表项
export interface RunInfo {
  run_id: string;
  status: string;
  msg_count: number;
  title: string;
  updated_at?: string | null; // 最后活跃时间（老数据可能为空）
}

// GET /models 模型信息
export interface ModelInfo {
  id: string;
  name: string;
  needs_key: boolean;
  configured: boolean;
}

export interface ModelsInfo {
  current: string;
  models: ModelInfo[];
}
