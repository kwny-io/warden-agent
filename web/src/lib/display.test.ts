// lib/display.ts —— 「已落库消息 → 聊天气泡」的唯一转换实现。
//
// 为什么值得单独测：它被**两条**恢复路径共用（初次加载、断线兜底重载），
// 以前这里是两份几乎一样但行为不同的循环，导致同一份历史在“刷新页面”与
// “断线重连”后长得不一样（刷新看不到工具结果、断线一次后突然冒出来）。
// 这里把三条契约钉住：谁是对话、谁是配置；工具气泡的成败怎么判定；气泡 id 从哪来。
import { describe, it, expect } from "vitest";
import { toDisplayMsgs, nextId } from "./display";
import type { DisplayMsg } from "./display";
import type { ChatMessage } from "./types";

/** 取唯一一条气泡：先断言数量，再返回（避免 `out[0].x` 撞上 noUncheckedIndexedAccess）。 */
function only(list: DisplayMsg[]): DisplayMsg {
  expect(list).toHaveLength(1);
  const [first] = list;
  if (!first) throw new Error("期望恰好一条气泡，实际是空的");
  return first;
}

describe("toDisplayMsgs", () => {
  it("system 消息不入聊天区（它是配置，不是对话）", () => {
    const out = toDisplayMsgs([{ role: "system", content: "你是 Warden" } as ChatMessage]);
    expect(out).toEqual([]);
  });

  it("assistant 的 tool_call 变成调用气泡，不把占位正文当回答", () => {
    const out = toDisplayMsgs([
      {
        role: "assistant",
        content: "[调用工具 weather.get]",
        tool_call: { id: "c1", name: "weather.get", arguments: { city: "上海" } },
      } as ChatMessage,
    ]);
    const bubble = only(out);
    expect(bubble.role).toBe("tool");
    expect(bubble.toolKind).toBe("call");
    expect(bubble.text).toBe("🔧 调用工具：weather.get");
  });

  it("落库的 tool 消息变成结果气泡；成败只认后端的失败前缀（契约）", () => {
    // 后端把错误拼进正文（runtime/session.py 的 _execute_with_result），
    // `/messages` 没有单独的 ok 字段——前缀文案一改，这条就必须红。
    const ok = only(toDisplayMsgs([{ role: "tool", content: "上海 晴 26℃" } as ChatMessage]));
    expect(ok).toMatchObject({ role: "tool", toolKind: "result", toolOk: true });

    const bad = only(
      toDisplayMsgs([
        {
          role: "tool",
          content: "[工具 db.query 执行失败，请修正后重试] TimeoutError",
        } as ChatMessage,
      ]),
    );
    expect(bad).toMatchObject({ role: "tool", toolKind: "result", toolOk: false });
    // 正文原样保留（不裁剪、不改写：审计要看的就是原文）
    expect(bad.text).toContain("TimeoutError");
  });

  it("气泡 id 全局唯一且递增（两条恢复路径共用同一个序列，否则 React key 会撞）", () => {
    const a = toDisplayMsgs([
      { role: "user", content: "一" } as ChatMessage,
      { role: "assistant", content: "二" } as ChatMessage,
      { role: "tool", content: "三" } as ChatMessage,
    ]);
    const b = toDisplayMsgs([{ role: "user", content: "四" } as ChatMessage]);
    const ids = [...a, ...b].map((m) => m.id);
    expect(new Set(ids).size).toBe(ids.length);
    expect(nextId()).toBeGreaterThan(Math.max(...ids));
  });

  it("空内容不产生空气泡（畸形历史不白屏）", () => {
    const out = toDisplayMsgs([
      { role: "assistant", content: "" } as ChatMessage,
      { role: "user", content: null } as unknown as ChatMessage,
    ]);
    const bubble = only(out);
    expect(bubble).toMatchObject({ role: "user", text: "" });
  });
});
