import type { ChatMessage } from "./types";

// 聊天气泡的数据形状与「已落库消息 → 气泡」的唯一转换实现。
//
// 为什么单独一个模块：这两样东西被**多条**路径共用（初次加载、断线兜底重载），
// 而组件文件里只该导出组件（`react-refresh/only-export-components`；
// 更重要的是：放组件文件里，后面很容易顺手在里面再写一份分支逻辑，就又会分叉）。

export interface DisplayMsg {
  id: number;
  role: "user" | "assistant" | "tool" | "system";
  text: string;
  /**
   * 工具气泡的三种形态：
   * - `call`   模型想调用某工具（🔧 调用工具：x）
   * - `result` 工具真执行了并返回结果（审计/排查最常要看的东西）
   * - `hint`   调用前校验拦下并给了提示，**本次没执行**
   */
  toolKind?: "call" | "result" | "hint";
  /** 工具结果是否成功（失败用琥珀色标出）。仅 `toolKind === "result"` 时有意义。 */
  toolOk?: boolean;
  // 若这是一条“需要审批”的气泡，带上审批信息
  approval?: {
    approval_id: string;
    tool_name: string;
    arguments: Record<string, unknown>;
    reason: string;
  };
}

// 气泡 id 的**唯一**来源：流式新增的气泡与批量恢复的气泡共用同一个自增序列，
// 否则两边各自从 1 开始，React 的 key 会撞（表现是气泡内容错位）。
let seq = 0;
export const nextId = () => ++seq;

/**
 * 已落库消息 → 聊天气泡列表。**两条恢复路径共用这一个函数**。
 *
 * 以前这里有两份几乎一样、但行为不同的循环：初次加载跳过 `tool` 消息，
 * 断线兜底的 `reloadFromServer` 却把 `tool` / `system` 全部渲染出来。
 * 结果是同一份历史在“刷新页面”与“断线重连”后长得不一样——最典型的是：
 * 刷新看不到工具结果，断线兜底一次后工具结果突然冒出来。
 * 所以现在只留一份实现，两边都调它。
 *
 * 约定：`system`（系统提示词）不入聊天区——它是配置，不是对话的一部分。
 */
export function toDisplayMsgs(list: ChatMessage[]): DisplayMsg[] {
  const out: DisplayMsg[] = [];
  for (const m of list) {
    if (m.role === "system") continue;
    if (m.role === "user") {
      out.push({ id: nextId(), role: "user", text: m.content ?? "" });
    } else if (m.role === "assistant" && m.tool_call?.name) {
      out.push({
        id: nextId(),
        role: "tool",
        toolKind: "call",
        text: `🔧 调用工具：${m.tool_call.name}`,
      });
    } else if (m.role === "assistant" && m.content) {
      out.push({ id: nextId(), role: "assistant", text: m.content });
    } else if (m.role === "tool") {
      const text = m.content ?? "";
      // 成败只能看正文前缀：`/messages` 没有单独的 ok 字段（后端把错误串拼进了正文，
      // 见 runtime/session.py 的 `_execute_with_result`）。前缀改了这里就得跟着改。
      const failed = text.startsWith("[工具 ") && text.includes("执行失败");
      out.push({ id: nextId(), role: "tool", toolKind: "result", toolOk: !failed, text });
    }
  }
  return out;
}
