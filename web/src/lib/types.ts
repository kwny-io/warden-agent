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
//
// `disconnected` 不是服务端发的事件，而是**客户端**在流「没说完就结束」时补的：
// 连接被掐断（代理 idle 超时 / 切网 / 客户端被回收）时后端那一轮往往还在跑，
// 所以 UI 要提示「回复仍在后台继续」并去 /events/{run_id} 续读，而不是静默收尾。
export type StreamEvent =
  | { type: "start" }
  | { type: "delta"; text: string }
  | { type: "tool"; name: string; arguments: Record<string, unknown> }
  // 工具执行结果（后端在 _execute 落库后原样推出来）。`ok=false` 表示工具失败，
  // `text` 是喂回模型的那段文本（失败时带 "执行失败，请修正后重试" 前缀）。
  | { type: "tool_result"; name: string; ok: boolean; text: string }
  // 调用前校验给的提示：**这次没有执行**，把提示喂回模型让它换招。
  | { type: "tool_hint"; name: string; text: string }
  | { type: "final"; text: string }
  | { type: "needs_approval"; approval: Approval }
  | { type: "disconnected" }
  | { type: "error"; message: string };

// GET /capabilities
export interface Capabilities {
  tools: string[];
  /**
   * 能力清单。
   *
   * 为什么这里有一堆“到底是不是真的”的标记：后端可能是**离线演示**档（FakeModel、
   * mock 搜索），而演示档的返回值与真能力长得一模一样。控制台不把这件事摆出来，
   * 用户就会把演示当成系统能力——“能力错觉”比能力缺失危险得多。
   * 约定：`null` = 后端没上报（不确定），前端**不据此下任何结论**；`false` = 明确说了不是。
   */
  features: {
    memory: boolean;
    skills: string[];
    web: boolean;
    mcp_server: string | null;
    /** 记忆是否落盘（null = 未上报）；false = 进程内，重启即丢 */
    memory_persistent?: boolean | null;
    /** web.search / web.fetch 是不是离线演示 provider */
    web_search_mock?: boolean | null;
    web_fetch_mock?: boolean | null;
    /** 为什么退回了离线 mock（配置不全 / provider 名写错）——给运维看 */
    web_degraded_reason?: string;
    /** 模型是真实推理还是演示脚本（FakeModel） */
    model_kind?: string | null;
    model_kind_now?: string | null;
    demo_instance?: boolean;
    /** 知识库用的是哪种嵌入器（词频 vs 语义） */
    knowledge_embedder?: string | null;
  };
}

// GET /memory/{scope}
export interface MemoryItem {
  scope: string;
  key: string;
  text: string;
  status: string;
}

// GET /memory/{scope}/candidates —— 待确认记忆候选（两阶段记忆的第一阶段）
export interface MemoryCandidate {
  /** 候选 id：确认/拒绝都靠它，缺了就点不动（不能拿 key 当 id 用，同 key 可能有多个候选） */
  uid: string;
  scope: string;
  key: string;
  text: string;
  status: string;
  /** 非空 = 确认后会替掉这条已有记忆（界面必须说清楚，否则就是静默覆盖别人的旧值） */
  replaces_text: string;
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
export interface ModelProbe {
  id: string;
  ok: boolean;
  /** 探不通的**可操作分档**（换 Key / 去充值 / 改地址 / 等一等） */
  code?: string;
  /** 给用户看的动作句（服务端给，前端不再自己编） */
  hint?: string | null;
  reason: string | null;
  latency_ms?: number;
  checked_at?: string;
  cached?: boolean;
}

export interface ModelInfo {
  id: string;
  name: string;
  needs_key: boolean;
  /**
   * 除 key 之外还要填端点（地址 + 模型名）。目前只有「自建 / 私有部署」是 true。
   *
   * 为什么要让界面知道这件事：不告诉它，用户点「切换」才收到 400，
   * 而界面上没有任何可填的地方——那是个死胡同。
   */
  needs_endpoint?: boolean;
  /** 有 key（不等于能用：provider 可能宕机/网络不通/key 被吐回） */
  configured: boolean;
  /** 已登记的自建端点地址（不是秘密，回显才能让用户改错字；**key 永不回显**） */
  base_url?: string | null;
  /** 已登记的自建模型名（如 qwen2.5-7b-instruct） */
  model?: string | null;
  /** 上次主动探测的结论；从没探过就是 null（不许编一个“可用”出来） */
  last_probe?: ModelProbe | null;
}

/** 自建接入的端点两件套（POST /models/select 与 /models/probe 共用）。 */
export interface ModelEndpoint {
  base_url?: string | null;
  model?: string | null;
}

export interface ModelsInfo {
  current: string;
  models: ModelInfo[];
}

// GET /users 中控台用户
export interface UserInfo {
  user_id: string;
  created_at: string;
}

// GET /approvals/history 审批决策历史项
export interface ApprovalHistoryItem {
  run_id: string;
  approval_id: string;
  tool_name: string;
  decision: string; // approved / rejected
  created_at: string;
}
