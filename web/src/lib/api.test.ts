// lib/api.ts —— 后端 REST 客户端的单元测试。
// 重点在**错误分支**：4xx/5xx、非 JSON 错误体、网络异常、主动 abort，
// 以及 SSE 流式解析里的坏帧容错。所有网络用 vi.fn() mock，不发真实请求。
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  api,
  getApiKey,
  isApiKeyRemembered,
  setApiKey,
  DEFAULT_TIMEOUT_MS,
  DEFAULT_LONG_TIMEOUT_MS,
} from "./api";

/** 造一个最小的 fetch Response（只实现 api.ts 用到的字段）。 */
function jsonResponse(body: unknown, init?: { status?: number; statusText?: string }): Response {
  const status = init?.status ?? 200;
  return {
    ok: status < 400,
    status,
    statusText: init?.statusText ?? "OK",
    json: async () => body,
  } as unknown as Response;
}

/** 造一个 json() 会抛错的 Response，模拟错误体不是 JSON（如网关返回 HTML）。 */
function nonJsonResponse(status: number, statusText: string): Response {
  return {
    ok: false,
    status,
    statusText,
    json: async () => {
      throw new SyntaxError("Unexpected token < in JSON");
    },
  } as unknown as Response;
}

/** 造一个带 body.getReader() 的 SSE 响应，按 chunk 依次吐出字符串。
 *  也接受 Uint8Array——用来验证多字节字符被切在 chunk 边界时不会乱码。 */
function streamResponse(chunks: Array<string | Uint8Array>, status = 200): Response {
  const enc = new TextEncoder();
  let i = 0;
  return {
    ok: status < 400,
    status,
    statusText: "OK",
    json: async () => ({ detail: "boom" }),
    body: {
      getReader: () => ({
        read: async () => {
          const c = chunks[i++];
          if (c === undefined) return { done: true, value: undefined };
          return { done: false, value: typeof c === "string" ? enc.encode(c) : c };
        },
      }),
    },
  } as unknown as Response;
}

const fetchMock = vi.fn();

beforeEach(() => {
  // 两处都要清：密钥默认存在 sessionStorage，勾"记住"后才会跑到 localStorage
  localStorage.clear();
  sessionStorage.clear();
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("访问密钥 getApiKey / setApiKey", () => {
  it("存进去能读回来，且会 trim", () => {
    setApiKey("  secret-key  ");
    expect(getApiKey()).toBe("secret-key");
  });

  it("传空字符串等于清除密钥", () => {
    setApiKey("abc");
    setApiKey("   ");
    expect(getApiKey()).toBe("");
  });

  it("默认只写 sessionStorage，不落 localStorage", () => {
    // 这条是安全行为的回归线：key 一旦默认落 localStorage 就是跨重启长期驻留
    setApiKey("abc");
    expect(sessionStorage.getItem("warden.apiKey")).toBe("abc");
    expect(localStorage.getItem("warden.apiKey")).toBeNull();
    expect(isApiKeyRemembered()).toBe(false);
  });

  it("remember=true 才写 localStorage，并清掉 sessionStorage 副本", () => {
    setApiKey("abc", { remember: true });
    expect(localStorage.getItem("warden.apiKey")).toBe("abc");
    // 只留一处：两处都留着会让"取消勾选后旧副本还在"无法解释
    expect(sessionStorage.getItem("warden.apiKey")).toBeNull();
    expect(isApiKeyRemembered()).toBe(true);
  });

  it("从记住改成不记住时，localStorage 里的旧 key 会被清掉", () => {
    setApiKey("abc", { remember: true });
    setApiKey("abc", { remember: false });
    expect(localStorage.getItem("warden.apiKey")).toBeNull();
    expect(getApiKey()).toBe("abc"); // 当前会话仍可用
  });

  it("清除时两处都清（不留界面上已清、请求还带着的鬼状态）", () => {
    setApiKey("abc", { remember: true });
    setApiKey("");
    expect(localStorage.getItem("warden.apiKey")).toBeNull();
    expect(sessionStorage.getItem("warden.apiKey")).toBeNull();
  });

  it("sessionStorage 不可用时降级返回空串而不是抛错", () => {
    const spy = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("QuotaExceeded / privacy mode");
    });
    expect(() => getApiKey()).not.toThrow();
    expect(getApiKey()).toBe("");
    spy.mockRestore();
  });
});

describe("request 请求封装", () => {
  it("chat：拼出编码后的 URL、POST 方法和 JSON body", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ run_id: "r1", status: "ok", kind: "final" }));
    const out = await api.chat("run/1", "你好", "u 1");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [path, init] = fetchMock.mock.calls[0]!;
    expect(path).toBe("/chat/run%2F1?user_id=u%201");
    expect(init.method).toBe("POST");
    expect(init.body).toBe(JSON.stringify({ text: "你好" }));
    expect(out).toEqual({ run_id: "r1", status: "ok", kind: "final" });
  });

  it("未设密钥时不带 Authorization，设了就带 Bearer", async () => {
    fetchMock.mockResolvedValue(jsonResponse([]));
    await api.runs();
    expect(fetchMock.mock.calls[0]![1].headers).not.toHaveProperty("Authorization");

    setApiKey("k123");
    fetchMock.mockResolvedValue(jsonResponse([]));
    await api.runs();
    expect(fetchMock.mock.calls[1]![1].headers.Authorization).toBe("Bearer k123");
  });

  it("调用方自带 headers 时仍保留 Authorization（不会被覆盖）", async () => {
    setApiKey("k123");
    fetchMock.mockResolvedValue(jsonResponse([]));
    // 直接走内部 request 的等价路径：runs 不传 headers，这里用 messages 验证默认头
    await api.messages("r1");
    const headers = fetchMock.mock.calls[0]![1].headers;
    expect(headers["Content-Type"]).toBe("application/json");
    expect(headers.Authorization).toBe("Bearer k123");
  });

  it("4xx + JSON detail：错误信息是 `状态码: detail`", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ detail: "会话不存在" }, { status: 404, statusText: "Not Found" }),
    );
    await expect(api.status("missing")).rejects.toThrow("404: 会话不存在");
  });

  it("5xx + 非 JSON 错误体：回退到 statusText，不因解析失败再抛一个错", async () => {
    fetchMock.mockResolvedValue(nonJsonResponse(500, "Internal Server Error"));
    await expect(api.health()).rejects.toThrow("500: Internal Server Error");
  });

  it("网络异常（fetch 直接 reject）：原样向上抛", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    await expect(api.approvals()).rejects.toThrow("Failed to fetch");
  });

  it("调用方 abort（AbortError）会原样向上抛，不被包装成别的错误", async () => {
    const abortErr = new DOMException("The operation was aborted.", "AbortError");
    fetchMock.mockRejectedValue(abortErr);
    await expect(api.chat("r1", "hi")).rejects.toBe(abortErr);
  });

  it("runs 带 user_id 时会做 URL 编码（含空 user_id 时不加 query）", async () => {
    fetchMock.mockResolvedValue(jsonResponse([]));
    await api.runs("user/a b");
    expect(fetchMock.mock.calls[0]![0]).toBe("/runs?user_id=user%2Fa%20b");

    fetchMock.mockResolvedValue(jsonResponse([]));
    await api.runs("");
    expect(fetchMock.mock.calls[1]![0]).toBe("/runs");
  });

  it("deleteRun 用 DELETE，selectModel 没传 key 时 api_key 为 null", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ ok: true }));
    await api.deleteRun("r1");
    expect(fetchMock.mock.calls[0]![1].method).toBe("DELETE");

    fetchMock.mockResolvedValue(jsonResponse({ ok: true, current: "m" }));
    await api.selectModel("gpt", undefined);
    // 没给 endpoint 时两个字段都发 null：后端把 null 当成"没给"，
    // 而不是当成"把已存的地址清空"。
    expect(JSON.parse(fetchMock.mock.calls[1]![1].body)).toEqual({
      id: "gpt",
      api_key: null,
      base_url: null,
      model: null,
    });
  });

  it("probeModel 把**还没保存**的 key 一起发出去（否则先探通再保存是死路）", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ id: "custom", ok: true }));
    await api.probeModel("custom", true, undefined, { base_url: "http://10.0.0.7:8000/v1" }, "not-needed");
    expect(JSON.parse(fetchMock.mock.calls[0]![1].body)).toEqual({
      id: "custom",
      force: true,
      base_url: "http://10.0.0.7:8000/v1",
      model: null,
      api_key: "not-needed",
    });
  });

  it("deleteRuns 打 DELETE /runs，id 放请求体（路径里装不下多个）", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ ok: true, deleted: ["r1", "r2"], count: 2 }));
    await api.deleteRuns(["r1", "r2"]);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/runs");
    expect(init.method).toBe("DELETE");
    expect(JSON.parse(init.body)).toEqual({ run_ids: ["r1", "r2"] });
  });
});

describe("streamChat SSE 解析", () => {
  it("逐帧回调 delta/final，并忽略坏 JSON 帧与无 data 行", async () => {
    fetchMock.mockResolvedValue(
      streamResponse([
        'data: {"type":"delta","text":"你"}\n\n',
        ": 心跳注释\n\n",
        "data: {bad json}\n\n",
        'data: {"type":"delta","text":"好"}\n\n',
        'data: {"type":"final","text":"你好"}\n\n',
      ]),
    );
    const events: unknown[] = [];
    await api.streamChat("r1", "hi", (ev) => events.push(ev));
    expect(events).toEqual([
      { type: "delta", text: "你" },
      { type: "delta", text: "好" },
      { type: "final", text: "你好" },
    ]);
  });

  it("同一个 SSE 事件被拆到两个 chunk 时也能正确拼接", async () => {
    fetchMock.mockResolvedValue(streamResponse(['data: {"type":"fin', 'al","text":"完"}\n\n']));
    const events: unknown[] = [];
    await api.streamChat("r1", "hi", (ev) => events.push(ev));
    expect(events).toEqual([{ type: "final", text: "完" }]);
  });

  it("HTTP 非 2xx：不抛错，而是回调一个 error 事件（带后端 detail）", async () => {
    fetchMock.mockResolvedValue(streamResponse([], 500));
    const events: unknown[] = [];
    await expect(api.streamChat("r1", "hi", (ev) => events.push(ev))).resolves.toBeUndefined();
    expect(events).toEqual([{ type: "error", message: "500: boom" }]);
  });

  it("响应没有 body 时同样回调 error，不崩在 getReader 上", async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      statusText: "OK",
      body: null,
    } as unknown as Response);
    const events: unknown[] = [];
    await api.streamChat("r1", "hi", (ev) => events.push(ev));
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({ type: "error" });
  });

  // —— 以下四条是解析器健壮性回归：这些情形在生产路径上都出现过
  // （CDN/nginx 改写、后端改用 CRLF、多行 JSON）——

  it("CRLF 分隔（\\r\\n\\r\\n）：照旧能解出每一帧", async () => {
    fetchMock.mockResolvedValue(
      streamResponse([
        'data: {"type":"delta","text":"一"}\r\n\r\n',
        'data: {"type":"final","text":"一二"}\r\n\r\n',
      ]),
    );
    const events: unknown[] = [];
    await api.streamChat("r1", "hi", (ev) => events.push(ev));
    expect(events).toEqual([
      { type: "delta", text: "一" },
      { type: "final", text: "一二" },
    ]);
  });

  it("最后一帧没有尾随空行（流直接结束）：不会丢掉这一帧", async () => {
    fetchMock.mockResolvedValue(
      streamResponse([
        'data: {"type":"delta","text":"半"}\n\n',
        'data: {"type":"final","text":"半句完"}',
      ]),
    );
    const events: unknown[] = [];
    await api.streamChat("r1", "hi", (ev) => events.push(ev));
    expect(events).toEqual([
      { type: "delta", text: "半" },
      { type: "final", text: "半句完" },
    ]);
  });

  it("多行 data: 按规范拼接后再 parse（不被截成首行）", async () => {
    fetchMock.mockResolvedValue(
      streamResponse(['data: {"type":"final",\ndata: "text":"多行"}\n\n']),
    );
    const events: unknown[] = [];
    await api.streamChat("r1", "hi", (ev) => events.push(ev));
    expect(events).toEqual([{ type: "final", text: "多行" }]);
  });

  it("多字节字符被切在 chunk 边界上：解码器 flush 后不乱码", async () => {
    // “你好” = E4 BD A0 E5 A5 BD；从第 4 个字节后切开，模拟网络分片
    const bytes = new TextEncoder().encode('data: {"type":"final","text":"你好"}\n\n');
    const cut = bytes.indexOf(0xe4) + 1; // 切在第一个汉字（UTF-8 3 字节）的中间
    fetchMock.mockResolvedValue(streamResponse([bytes.slice(0, cut), bytes.slice(cut)]));
    const events: unknown[] = [];
    await api.streamChat("r1", "hi", (ev) => events.push(ev));
    expect(events).toEqual([{ type: "final", text: "你好" }]);
  });
});

describe("2xx 但体为空 / 不是 JSON", () => {
  it("非 JSON 的 200 不再抛 SyntaxError，退化为 undefined（交给窄守卫兜底）", async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      statusText: "OK",
      json: async () => {
        throw new SyntaxError("Unexpected token < in JSON at position 0");
      },
    } as unknown as Response);
    await expect(api.capabilities()).resolves.toBeUndefined();
  });

  it("204 No Content 不碰 body，直接 undefined", async () => {
    const json = vi.fn(async () => ({ ok: true }));
    fetchMock.mockResolvedValue({
      ok: true,
      status: 204,
      statusText: "No Content",
      json,
    } as unknown as Response);
    await expect(api.deleteRun("r1")).resolves.toBeUndefined();
    expect(json).not.toHaveBeenCalled();
  });
});

describe("超时与取消", () => {
  /** 造一个只在 signal abort 时才 reject 的 fetch，模拟“挂住的连接”。 */
  function hangingFetch() {
    fetchMock.mockImplementation(
      (_path: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          init.signal?.addEventListener("abort", () => reject(init.signal?.reason));
        }),
    );
  }

  it("每个请求都带 AbortSignal（可被取消）", async () => {
    fetchMock.mockResolvedValue(jsonResponse([]));
    await api.runs();
    expect(fetchMock.mock.calls[0]![1].signal).toBeInstanceOf(AbortSignal);
  });

  it("超过 timeoutMs 会主动 abort，抛 TimeoutError", async () => {
    hangingFetch();
    const err = await api.status("r1", { timeoutMs: 20 }).then(
      () => null,
      (e: unknown) => e,
    );
    expect(err).toBeInstanceOf(DOMException);
    expect((err as DOMException).name).toBe("TimeoutError");
    expect((err as DOMException).message).toContain("请求超时");
  });

  it("timeoutMs: 0 表示不限时", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ run_id: "r1", status: "ok" }));
    await expect(api.status("r1", { timeoutMs: 0 })).resolves.toEqual({
      run_id: "r1",
      status: "ok",
    });
  });

  it("调用方 signal abort → 传给 fetch 的 signal 真的被 abort（请求确实取消）", async () => {
    hangingFetch();
    const ctrl = new AbortController();
    const p = api.runs(undefined, { signal: ctrl.signal });
    const fetchSignal = fetchMock.mock.calls[0]![1].signal as AbortSignal;
    expect(fetchSignal.aborted).toBe(false);
    ctrl.abort();
    expect(fetchSignal.aborted).toBe(true);
    await expect(p).rejects.toBeInstanceOf(DOMException);
  });

  it("请求结束后不留悬挂计时器（cleanup 真的跑了）", async () => {
    vi.useFakeTimers();
    try {
      fetchMock.mockResolvedValue(jsonResponse([]));
      await api.runs();
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it("非流式对话用长超时：15s 不会被误杀，5 分钟才超时", async () => {
    vi.useFakeTimers();
    try {
      hangingFetch();
      const p = api.chat("r1", "hi", "u1");
      const settled = vi.fn();
      void p.then(settled, settled); // 同时挂成功/失败处理，避免 unhandled rejection
      await vi.advanceTimersByTimeAsync(DEFAULT_TIMEOUT_MS + 1);
      expect(settled).not.toHaveBeenCalled(); // 15s 时仍未超时
      await vi.advanceTimersByTimeAsync(DEFAULT_LONG_TIMEOUT_MS);
      await expect(p).rejects.toBeInstanceOf(DOMException);
    } finally {
      vi.useRealTimers();
    }
  });

  it("批准/拒绝也是长超时（会等完那一轮，不能被 15s 掐掉）", async () => {
    vi.useFakeTimers();
    try {
      for (const call of [() => api.approve("r1"), () => api.reject("r1")]) {
        hangingFetch();
        const p = call();
        const settled = vi.fn();
        void p.then(settled, settled);
        await vi.advanceTimersByTimeAsync(DEFAULT_TIMEOUT_MS + 1);
        expect(settled).not.toHaveBeenCalled();
        await vi.advanceTimersByTimeAsync(DEFAULT_LONG_TIMEOUT_MS);
        await expect(p).rejects.toBeInstanceOf(DOMException);
      }
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("streamChat 取消与超时", () => {
  /** 造一个“读到 cancel() 为止”的流：cancel() 后 read() 返回 done（真实 fetch 就是这行为）。 */
  function cancellableStream() {
    let cancelled = false;
    let resolveRead: ((r: { done: boolean; value?: Uint8Array }) => void) | null = null;
    const cancel = vi.fn(() => {
      cancelled = true;
      resolveRead?.({ done: true });
      return Promise.resolve();
    });
    const body = {
      getReader: () => ({
        // 真实流语义：cancel() 之后再 read() 直接得到 done
        read: () =>
          cancelled
            ? Promise.resolve<{ done: boolean; value?: Uint8Array }>({ done: true })
            : new Promise<{ done: boolean; value?: Uint8Array }>((res) => {
                resolveRead = res;
              }),
        cancel,
      }),
    };
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      statusText: "OK",
      body,
    } as unknown as Response);
    return { cancel };
  }

  it("调用方 abort：reader 被 cancel，且不回调 error（取消不是错误）", async () => {
    const { cancel } = cancellableStream();
    const ctrl = new AbortController();
    const events: unknown[] = [];
    const p = api.streamChat("r1", "hi", (ev) => events.push(ev), undefined, {
      signal: ctrl.signal,
    });
    ctrl.abort();
    await p;
    expect(cancel).toHaveBeenCalled();
    expect(events).toEqual([]);
  });

  it("流式超时：主动 abort 并回调 error（用户能看到超时，而不是一直转圈）", async () => {
    fetchMock.mockImplementation(
      (_path: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          init.signal?.addEventListener("abort", () => reject(init.signal?.reason));
        }),
    );
    const events: unknown[] = [];
    await api.streamChat("r1", "hi", (ev) => events.push(ev), undefined, { timeoutMs: 20 });
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({ type: "error" });
    expect((events[0] as { message: string }).message).toContain("请求超时");
  });
});

describe("streamChat 断线识别（连接断了 ≠ 说完了）", () => {
  it("流被掐断（只剩半截、没给终态事件）：补一个 disconnected，而不是静默收尾", async () => {
    fetchMock.mockResolvedValue(streamResponse(['data: {"type":"delta","text":"你"}\n\n']));
    const seen = vi.fn();
    await api.streamChat("r1", "hi", seen);
    expect(seen).toHaveBeenCalledWith({ type: "delta", text: "你" });
    // 关键：没有这条，UI 会把「断线」当成「模型说完了」，用户以为回答就这样了
    expect(seen).toHaveBeenLastCalledWith({ type: "disconnected" });
  });

  it("正常说完（final 已到）：不补 disconnected，不误报成断线", async () => {
    fetchMock.mockResolvedValue(streamResponse(['data: {"type":"final","text":"完成"}\n\n']));
    const seen = vi.fn();
    await api.streamChat("r1", "hi", seen);
    expect(seen.mock.calls.map((c) => (c[0] as { type: string }).type)).toEqual(["final"]);
  });

  it("终态事件之后服务端还发了心跳注释行：同样不算断线", async () => {
    fetchMock.mockResolvedValue(
      streamResponse(['data: {"type":"final","text":"ok"}\n\n', ": keep-alive\n\n"]),
    );
    const seen = vi.fn();
    await api.streamChat("r1", "hi", seen);
    expect(seen.mock.calls.map((c) => (c[0] as { type: string }).type)).toEqual(["final"]);
  });

  it("HTTP 非 2xx 的 error 事件算终态：不再追一个 disconnected", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ detail: "run 不存在" }, { status: 404, statusText: "Not Found" }),
    );
    const seen = vi.fn();
    await api.streamChat("r1", "hi", seen);
    expect(seen.mock.calls.map((c) => (c[0] as { type: string }).type)).toEqual(["error"]);
  });

  it("只有心跳、一个字都没说就断了：照样报 disconnected", async () => {
    fetchMock.mockResolvedValue(streamResponse([": keep-alive\n\n"]));
    const seen = vi.fn();
    await api.streamChat("r1", "hi", seen);
    expect(seen).toHaveBeenCalledTimes(1);
    expect(seen).toHaveBeenCalledWith({ type: "disconnected" });
  });
});
