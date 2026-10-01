// lib/guards.ts —— 后端响应的运行时窄守卫。
// 这是"后端返回畸形/空数据时组件不崩"的第一道防线，所以用最刁钻的输入打它：
// 非数组 / null 元素 / 缺字段 / 类型不符，断言的是"收窄成什么"，不是"有没有被调用"。
import { describe, it, expect } from "vitest";
import {
  bool,
  isRecord,
  num,
  recordArray,
  str,
  strOrNull,
  stringArray,
  toApprovalHistory,
  toApprovals,
  toCapabilities,
  toChatMessages,
  toMemoryItems,
  toModelsInfo,
  toRunInfos,
  toUserInfos,
} from "./guards";

describe("基础收窄", () => {
  it("isRecord 排除 null / 数组 / 原始值", () => {
    expect(isRecord({})).toBe(true);
    expect(isRecord(null)).toBe(false);
    expect(isRecord([])).toBe(false);
    expect(isRecord("x")).toBe(false);
    expect(isRecord(0)).toBe(false);
  });

  it("str / bool / num / strOrNull 对类型不符一律回退", () => {
    expect(str(3)).toBe("");
    expect(str(undefined, "d")).toBe("d");
    expect(str("v")).toBe("v");
    expect(bool(1)).toBe(false);
    expect(bool("true")).toBe(false);
    expect(bool(true)).toBe(true);
    expect(num("3")).toBe(0);
    expect(num(Number.NaN)).toBe(0);
    expect(num(Infinity, -1)).toBe(-1);
    expect(strOrNull(undefined)).toBeNull();
    expect(strOrNull("a")).toBe("a");
  });

  it("stringArray 只留字符串项", () => {
    expect(stringArray(["a", null, 3, {}, "b"])).toEqual(["a", "b"]);
    expect(stringArray("nope")).toEqual([]);
    expect(stringArray(null)).toEqual([]);
  });

  it("recordArray 非数组 → []，数组里的非对象项被丢掉", () => {
    expect(recordArray(null)).toEqual([]);
    expect(recordArray("x")).toEqual([]);
    expect(recordArray([{ a: 1 }, null, 7, [], { b: 2 }])).toEqual([{ a: 1 }, { b: 2 }]);
  });
});

describe("toChatMessages", () => {
  it("非数组 → []（后端把 messages 返回成对象也不炸）", () => {
    expect(toChatMessages({ role: "user" })).toEqual([]);
    expect(toChatMessages(null)).toEqual([]);
    expect(toChatMessages(undefined)).toEqual([]);
  });

  it("丢掉 null / 数字等非对象元素，字段缺失给缺省值", () => {
    const out = toChatMessages([null, 123, "x", {}, { role: 7, content: 8 }, { role: "user" }]);
    expect(out).toHaveLength(3);
    expect(out[1]).toEqual({ role: "", content: null, tool_call: null });
    expect(out[2]).toEqual({ role: "user", content: null, tool_call: null });
  });

  it("tool_call 非对象 → null；arguments 非对象 → undefined", () => {
    expect(toChatMessages([{ role: "assistant", tool_call: "shell" }])[0]!.tool_call).toBeNull();
    const tc = toChatMessages([
      { role: "assistant", tool_call: { name: "shell.exec", arguments: "ls" } },
    ])[0]!.tool_call;
    expect(tc).toEqual({ id: undefined, name: "shell.exec", arguments: undefined });
  });
});

describe("toCapabilities", () => {
  it("整体不是对象 → null（调用方据此报『响应格式异常』，而不是假装空能力）", () => {
    expect(toCapabilities([])).toBeNull();
    expect(toCapabilities("oops")).toBeNull();
    expect(toCapabilities(null)).toBeNull();
  });

  it("tools 不是数组 → []（旧代码会在这里 .slice 崩掉）", () => {
    expect(toCapabilities({ tools: "shell", features: {} })?.tools).toEqual([]);
    expect(toCapabilities({ tools: null })?.tools).toEqual([]);
  });

  it("tools 里混入非字符串项只留字符串", () => {
    expect(toCapabilities({ tools: ["a", null, 1] })?.tools).toEqual(["a"]);
  });

  it("features 缺失 → 全默认；memory 只有严格 true 才算开", () => {
    // “到底是不是真的”类标记的默认值全是**三态 null**（= 后端没说），而不是 false：
    // 把没说读成“不是”会到处报假警，读成“是”会把演示档当真实能力。
    const defaults = {
      memory: false,
      skills: [],
      web: false,
      mcp_server: null,
      memory_persistent: null,
      web_search_mock: null,
      web_fetch_mock: null,
      web_degraded_reason: "",
      model_kind: null,
      model_kind_now: null,
      demo_instance: false,
      knowledge_embedder: null,
    };
    expect(toCapabilities({})?.features).toEqual(defaults);
    expect(toCapabilities({ features: { memory: "yes", mcp_server: 42 } })?.features).toEqual(
      defaults,
    );
  });

  it("演示档标记如实透传（含降级原因与模型 kind）", () => {
    const caps = toCapabilities({
      tools: [],
      features: {
        web: true,
        web_search_mock: true,
        web_fetch_mock: false,
        web_degraded_reason: "WARDEN_SEARCH_PROVIDER=tavily 但未配 WARDEN_SEARCH_API_KEY",
        memory_persistent: false,
        model_kind_now: "fake",
        knowledge_embedder: "hashing",
      },
    });
    expect(caps?.features.web_search_mock).toBe(true);
    expect(caps?.features.web_fetch_mock).toBe(false);
    expect(caps?.features.web_degraded_reason).toContain("WARDEN_SEARCH_API_KEY");
    expect(caps?.features.memory_persistent).toBe(false);
    expect(caps?.features.model_kind_now).toBe("fake");
    expect(caps?.features.knowledge_embedder).toBe("hashing");
  });

  it("三态字段给了非布尔值（后端改版 / 字符串 true）→ null，不猜", () => {
    const caps = toCapabilities({
      tools: [],
      features: { web_search_mock: "true", memory_persistent: 1 },
    });
    expect(caps?.features.web_search_mock).toBeNull();
    expect(caps?.features.memory_persistent).toBeNull();
  });
});

describe("其余接口的收窄", () => {
  it("toRunInfos：msg_count 类型不符归 0，updated_at 非字符串归 null", () => {
    expect(toRunInfos("nope")).toEqual([]);
    expect(toRunInfos([{ run_id: "r1", msg_count: "3", updated_at: 5 }])).toEqual([
      { run_id: "r1", status: "", msg_count: 0, title: "", updated_at: null },
    ]);
  });

  it("toMemoryItems：缺 text 归空串", () => {
    expect(toMemoryItems([{ key: "k" }])).toEqual([
      { scope: "", key: "k", text: "", status: "" },
    ]);
  });

  it("toApprovals：arguments 非对象归 {}，缺 run_id 的条目仍保留（不静默吞数据）", () => {
    expect(toApprovals([{ approval_id: "a", tool_name: "t", arguments: [] }])).toEqual([
      { approval_id: "a", run_id: "", tool_name: "t", arguments: {}, reason: "" },
    ]);
  });

  it("toApprovalHistory / toModelsInfo / toUserInfos 的边界", () => {
    expect(toApprovalHistory([{ decision: "approved" }])[0]!.decision).toBe("approved");
    expect(toModelsInfo(null)).toBeNull();
    expect(toModelsInfo({ models: "x" })).toEqual({ current: "", models: [] });
    expect(toUserInfos([{ user_id: "u1" }, {}, { user_id: 9 }])).toEqual([
      { user_id: "u1", created_at: "" },
    ]);
  });
});
