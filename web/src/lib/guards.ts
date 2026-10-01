// 后端响应的运行时窄守卫。
//
// 为什么需要：`api.*` 的返回类型是 TS 的**编译期**断言（`as T`），后端一旦返回
// 少字段 / 类型不符 / 元素为 null（网关改包、老数据、500 兜底体）就会在渲染时炸出
// TypeError（例如 `caps.tools.slice()`、`for (const m of list)`）。
// 这里用不引入任何依赖的手写守卫，在**取用处**把 unknown 收窄成 lib/types.ts 里的类型，
// 畸形数据一律降级为"空 + 缺省值"，而不是崩组件。
//
// 约定：类型定义始终以 ./types 为实现来源，本文件只做收窄，不新增业务结构。

import type {
  Approval,
  ApprovalHistoryItem,
  Capabilities,
  ChatMessage,
  MemoryCandidate,
  MemoryItem,
  ModelInfo,
  ModelsInfo,
  RunInfo,
  UserInfo,
} from "./types";

/** 普通对象（排除 null 和数组）——收窄的入口判断。 */
export function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** 取字符串字段；缺失或类型不符回退到 fallback。 */
export function str(v: unknown, fallback = ""): string {
  return typeof v === "string" ? v : fallback;
}

/** 取布尔字段；只有真正的 true 才算 true。 */
export function bool(v: unknown): boolean {
  return v === true;
}

/** 取数字字段；NaN/非有限数也算不合法。 */
export function num(v: unknown, fallback = 0): number {
  return typeof v === "number" && Number.isFinite(v) ? v : fallback;
}

/** 取可空字符串：不是字符串就是 null。 */
export function strOrNull(v: unknown): string | null {
  return typeof v === "string" ? v : null;
}

/**
 * 取**三态**布尔：`true` / `false` / `null`（未上报）。
 *
 * 为什么不能用 `bool()`（缺字段当 false）：像“记忆是不是落盘”“搜索是不是演示档”
 * 这类标记，把“没说”读成“不是”会让控制台到处报假警；读成“是”更糟——会把演示档
 * 当真实能力。三态才能做到：不确定就不下结论。
 */
export function boolOrNull(v: unknown): boolean | null {
  return typeof v === "boolean" ? v : null;
}

/** 只保留数组里的字符串项，丢掉 null / 数字 / 对象。 */
export function stringArray(v: unknown): string[] {
  if (!Array.isArray(v)) return [];
  return v.filter((x): x is string => typeof x === "string");
}

/** 把 unknown 收窄成对象数组：非数组 → []，数组里的非对象项 → 丢掉。 */
export function recordArray(v: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(v)) return [];
  return v.filter(isRecord);
}

// ---- 各接口的具体收窄 ----

/** GET /messages/{run_id} → ChatMessage[]（丢掉非对象项，字段缺失给缺省值）。 */
export function toChatMessages(v: unknown): ChatMessage[] {
  return recordArray(v).map((m) => {
    const tc = isRecord(m.tool_call) ? m.tool_call : null;
    let toolCall: ChatMessage["tool_call"] = null;
    if (tc) {
      toolCall = {
        id: strOrNull(tc.id) ?? undefined,
        name: strOrNull(tc.name) ?? undefined,
        arguments: isRecord(tc.arguments) ? tc.arguments : undefined,
      };
    }
    return {
      role: str(m.role),
      content: strOrNull(m.content),
      tool_call: toolCall,
    };
  });
}

/** GET /capabilities → Capabilities；整体不是对象时返回 null（调用方据此报"格式异常"）。 */
export function toCapabilities(v: unknown): Capabilities | null {
  if (!isRecord(v)) return null;
  const f = isRecord(v.features) ? v.features : {};
  return {
    tools: stringArray(v.tools),
    features: {
      memory: bool(f.memory),
      skills: stringArray(f.skills),
      web: bool(f.web),
      mcp_server: strOrNull(f.mcp_server),
      // “到底是不是真的”类标记一律三态：没说就是 null，前端不据此下结论
      memory_persistent: boolOrNull(f.memory_persistent),
      web_search_mock: boolOrNull(f.web_search_mock),
      web_fetch_mock: boolOrNull(f.web_fetch_mock),
      web_degraded_reason: str(f.web_degraded_reason),
      model_kind: strOrNull(f.model_kind),
      model_kind_now: strOrNull(f.model_kind_now),
      demo_instance: bool(f.demo_instance),
      knowledge_embedder: strOrNull(f.knowledge_embedder),
    },
  };
}

/** GET /runs → RunInfo[]。 */
export function toRunInfos(v: unknown): RunInfo[] {
  return recordArray(v).map((r) => ({
    run_id: str(r.run_id),
    status: str(r.status),
    msg_count: num(r.msg_count),
    title: str(r.title),
    updated_at: strOrNull(r.updated_at),
  }));
}

/** GET /memory/{scope} → MemoryItem[]（缺 text 也保留，渲染成空串）。 */
export function toMemoryItems(v: unknown): MemoryItem[] {
  return recordArray(v).map((m) => ({
    scope: str(m.scope),
    key: str(m.key),
    text: str(m.text),
    status: str(m.status),
  }));
}

/** GET /memory/{scope}/candidates → MemoryCandidate[]（缺 text/uid 也保留，由界面决定能不能点）。 */
export function toMemoryCandidates(v: unknown): MemoryCandidate[] {
  return recordArray(v).map((c) => ({
    uid: str(c.uid),
    scope: str(c.scope),
    key: str(c.key),
    text: str(c.text),
    status: str(c.status),
    replaces_text: str(c.replaces_text),
  }));
}

/** GET /approvals → Approval[]（缺 run_id 的也保留：面板要展示，不能静默吞掉）。 */
export function toApprovals(v: unknown): Approval[] {
  return recordArray(v).map((a) => ({
    approval_id: str(a.approval_id),
    run_id: str(a.run_id),
    tool_name: str(a.tool_name),
    arguments: isRecord(a.arguments) ? a.arguments : {},
    reason: str(a.reason),
  }));
}

/** GET /approvals/history → ApprovalHistoryItem[]。 */
export function toApprovalHistory(v: unknown): ApprovalHistoryItem[] {
  return recordArray(v).map((h) => ({
    run_id: str(h.run_id),
    approval_id: str(h.approval_id),
    tool_name: str(h.tool_name),
    decision: str(h.decision),
    created_at: str(h.created_at),
  }));
}

/** GET /models → ModelsInfo；整体不是对象时返回 null。 */
export function toModelsInfo(v: unknown): ModelsInfo | null {
  if (!isRecord(v)) return null;
  const models: ModelInfo[] = recordArray(v.models).map((m) => ({
    id: str(m.id),
    name: str(m.name),
    needs_key: bool(m.needs_key),
    configured: bool(m.configured),
  }));
  return { current: str(v.current), models };
}

/** GET /users → UserInfo[]（丢掉没有 user_id 的项）。 */
export function toUserInfos(v: unknown): UserInfo[] {
  const out: UserInfo[] = [];
  for (const u of recordArray(v)) {
    const id = str(u.user_id);
    if (id) out.push({ user_id: id, created_at: str(u.created_at) });
  }
  return out;
}
