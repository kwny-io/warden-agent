// 后端 REST API 客户端：封装对 FastAPI 的 fetch 调用。
// 开发模式下由 Vite 代理到 8000，生产模式下同源（FastAPI 托管），所以 base 留空。

import type {
  Approval,
  ApprovalHistoryItem,
  Capabilities,
  ChatMessage,
  ChatResponseOut,
  HealthResult,
  MemoryCandidate,
  MemoryItem,
  ModelProbe,
  ModelsInfo,
  RunInfo,
  StreamEvent,
  UserInfo,
} from "./types";

// ---- 访问密钥（WARDEN_API_KEY）----
//
// 服务端设了 WARDEN_API_KEY 之后，所有业务接口都要带 `Authorization: Bearer <key>`。
// 没设 key 时保持"不带 Authorization"——无鉴权模式下后端也不看这个头。
//
// **默认只存 sessionStorage（标签页作用域）**：这把 key 等于控制面全权凭证，
// 而页面要渲染模型生成的 Markdown —— 一次 XSS 就能 `localStorage.getItem(...)` 把它读走。
// 落在 localStorage 里的更大问题是"跨浏览器重启长期驻留"：读了不走的钥匙永远在那。
// 因此只有用户**显式勾选"记住密钥"**时才写 localStorage（此时同步清掉 sessionStorage 副本，
// 避免"取消勾选后旧副本还在"的假象）。这对 UX 是个可见变化：不勾选，关标签页后要重输。
const API_KEY_STORAGE = "warden.apiKey";

/** 读密钥：优先 sessionStorage（当前标签页），退回 localStorage（用户显式选择过"记住"）。 */
export function getApiKey(): string {
  try {
    return sessionStorage.getItem(API_KEY_STORAGE) ?? localStorage.getItem(API_KEY_STORAGE) ?? "";
  } catch {
    return ""; // 隐私模式等场景下 Web Storage 可能直接抛异常
  }
}

/** 用户是否选了"记住密钥"（UI 用它初始化勾选框）。 */
export function isApiKeyRemembered(): boolean {
  try {
    return localStorage.getItem(API_KEY_STORAGE) !== null;
  } catch {
    return false;
  }
}

/**
 * 写密钥。
 *
 * @param key 访问密钥；空字符串 = 清除（两处都清，不然会出现"界面上已清、请求里还带着"的鬼状态）
 * @param opts.remember true 才写 localStorage；默认 false 只写 sessionStorage
 */
export function setApiKey(key: string, opts: { remember?: boolean } = {}): void {
  const v = key.trim();
  try {
    if (!v) {
      sessionStorage.removeItem(API_KEY_STORAGE);
      localStorage.removeItem(API_KEY_STORAGE);
      return;
    }
    if (opts.remember) {
      localStorage.setItem(API_KEY_STORAGE, v);
      // 两处都留着会让"取消勾选后到底存在哪"无法推理，所以只留一处
      sessionStorage.removeItem(API_KEY_STORAGE);
    } else {
      sessionStorage.setItem(API_KEY_STORAGE, v);
      localStorage.removeItem(API_KEY_STORAGE);
    }
  } catch {
    /* 存不下就只能本次会话内存生效：请求会带空 key → 服务端 401，故障可见而不是静默 */
  }
}

/** 统一请求头：有访问密钥就带上。 */
function authHeaders(extra?: HeadersInit): HeadersInit {
  const key = getApiKey();
  return {
    "Content-Type": "application/json",
    ...(key ? { Authorization: `Bearer ${key}` } : {}),
    ...(extra || {}),
  };
}

// ---- 超时与取消 ----
//
// 所有请求都带一个 AbortSignal：默认超时（普通请求 15s、流式 5min），
// 调用方也可以把自己的 signal 传进来（组件卸载 / 切换 run 时取消在途请求）。
// 两者合并：任一触发都会 abort 底层 fetch，finally 里清掉计时器与监听。

/** 普通请求默认超时（毫秒）。 */
export const DEFAULT_TIMEOUT_MS = 15_000;
/**
 * 长请求默认超时（毫秒）：非流式对话 / 批准 / 拒绝 / 流式对话会等一整轮 agent 跑完，
 * 用 15s 会误杀（之前没有超时，改超时时必须把这几条排除在外）。
 */
export const DEFAULT_LONG_TIMEOUT_MS = 300_000;

export interface RequestOpts {
  /** 调用方自己的取消信号（组件卸载 / 切换会话时 abort）。与超时信号合并。 */
  signal?: AbortSignal | null;
  /** 本次请求的超时毫秒数；0 表示不限时。缺省用各接口的默认值。 */
  timeoutMs?: number;
}

/** 是不是"超时"导致的取消（用来区分"超时"和"调用方主动取消"）。 */
export function isTimeout(err: unknown): boolean {
  return err instanceof DOMException && err.name === "TimeoutError";
}

/**
 * 把「超时」与「调用方 signal」合并成一个 signal。
 * 返回的 cleanup 必须在请求结束后调用（清计时器、摘监听），否则会漏定时器。
 */
function withTimeout(
  timeoutMs: number,
  external?: AbortSignal | null,
): { signal: AbortSignal; cleanup: () => void } {
  const ctrl = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  if (timeoutMs > 0) {
    timer = setTimeout(
      () => ctrl.abort(new DOMException(`请求超时（${timeoutMs}ms）`, "TimeoutError")),
      timeoutMs,
    );
  }
  const onExternalAbort = () => ctrl.abort(external?.reason);
  if (external) {
    if (external.aborted) ctrl.abort(external.reason);
    else external.addEventListener("abort", onExternalAbort, { once: true });
  }
  return {
    signal: ctrl.signal,
    cleanup: () => {
      if (timer !== undefined) clearTimeout(timer);
      external?.removeEventListener("abort", onExternalAbort);
    },
  };
}

/**
 * 安全读 body：204 / 空体 / 非 JSON 的 2xx 都不再抛 SyntaxError，而是返回 undefined，
 * 交给调用方的窄守卫（lib/guards.ts）兜底成空值。
 */
async function readJson(res: Response): Promise<unknown> {
  if (res.status === 204) return undefined;
  try {
    return await res.json();
  } catch {
    return undefined;
  }
}

/** 从错误响应里取 detail（非 JSON 体回退到 statusText）。 */
async function errorDetail(res: Response): Promise<string> {
  let detail = res.statusText;
  try {
    const data = (await res.json()) as { detail?: unknown } | null;
    if (data && typeof data.detail === "string") detail = data.detail;
  } catch {
    /* 保留默认 detail */
  }
  return detail;
}

/** 终态事件：收到它，这一轮就"说完了"。收到之前流断掉 = 连接中断。 */
function isTerminalEvent(ev: unknown): boolean {
  if (!ev || typeof ev !== "object") return false;
  const t = (ev as { type?: unknown }).type;
  return t === "final" || t === "needs_approval" || t === "error";
}

/**
 * 把一个 SSE 响应体读成一串事件（streamChat / watchEvents 共用同一套帧解析）。
 *
 * 这几个"看起来多余"的细节都是踩过的坑：帧分隔符必须是「空行」且允许 CRLF（只认
 * \n\n 时，前面加一层 nginx/CDN 把行尾改写成 \r\n 就会静默失效：不报错也没内容）；
 * 一帧可以有多行 data:，按规范要拼起来（只取第一条会把多行 JSON 截断）；流结束时
 * 可能还剩一帧没有尾随空行（不清掉就整帧丢失）；中文多字节字符会被截在 chunk 边界上
 * （靠流式解码补齐）。服务端心跳是 `: keep-alive` 注释行，没有 data: 前缀，自然跳过。
 *
 * 返回「有没有读到终态事件」：false 且不是调用方主动取消 = 流在说到一半时断了。
 */
async function readSseBody(
  res: Response,
  signal: AbortSignal,
  onEvent: (ev: unknown) => void,
  opts: { stopOnTerminal?: boolean; isTerminal?: (ev: unknown) => boolean } = {},
): Promise<boolean> {
  const reader = res.body?.getReader();
  if (!reader) return false;
  const isTerminal = opts.isTerminal ?? isTerminalEvent;
  // 取消时立刻关掉底层流：reader.read() 会随之结束，避免连接挂着继续读。
  // 某些 mock / 老 polyfill 没有 cancel，包起来忽略。
  const cancelReader = () => {
    try {
      const p = reader.cancel();
      if (p) void p.catch(() => {});
    } catch {
      /* 没有 cancel 实现的流：忽略 */
    }
  };
  if (signal.aborted) cancelReader();
  else signal.addEventListener("abort", cancelReader, { once: true });

  let sawTerminal = false;
  const decoder = new TextDecoder();
  let buf = "";
  const handleFrame = (raw: string) => {
    const parts = raw
      .split(/\r?\n/)
      .filter((l) => l.startsWith("data:"))
      .map((l) => l.slice(5).replace(/^ /, ""));
    if (parts.length === 0) return;
    let ev: unknown;
    try {
      ev = JSON.parse(parts.join("\n"));
    } catch {
      return; /* 忽略坏帧 */
    }
    if (isTerminal(ev)) sawTerminal = true;
    onEvent(ev);
  };
  const FRAME_SEP = /\r?\n\r?\n/;
  const flushFrames = (final: boolean) => {
    for (;;) {
      const m = FRAME_SEP.exec(buf);
      if (!m) break;
      const raw = buf.slice(0, m.index);
      buf = buf.slice(m.index + m[0].length);
      handleFrame(raw);
    }
    // 流结束时可能还剩一帧（没有尾随空行）：不清掉就整帧丢失
    if (final && buf.trim()) {
      handleFrame(buf);
      buf = "";
    }
  };
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) {
        buf += decoder.decode(); // 冲掉解码器里半个多字节字符（中文截断在 chunk 边界上）
        flushFrames(true);
        return sawTerminal;
      }
      buf += decoder.decode(value, { stream: true });
      flushFrames(false);
      // 续读场景：拿到终态就收工，不必等服务端把长连接关掉
      if (opts.stopOnTerminal && sawTerminal) return true;
    }
  } finally {
    signal.removeEventListener("abort", cancelReader);
    cancelReader();
  }
}

async function request<T>(
  path: string,
  init?: RequestInit,
  opts?: RequestOpts,
  defaultTimeoutMs = DEFAULT_TIMEOUT_MS,
): Promise<T> {
  const { signal, cleanup } = withTimeout(opts?.timeoutMs ?? defaultTimeoutMs, opts?.signal);
  try {
    const res = await fetch(path, {
      ...init,
      // headers 放在 ...init 之后：否则调用方传了 headers 就会把 Authorization 覆盖掉
      headers: authHeaders(init?.headers),
      signal,
    });
    if (!res.ok) throw new Error(`${res.status}: ${await errorDetail(res)}`);
    return (await readJson(res)) as T;
  } finally {
    cleanup();
  }
}

export const api = {
  /** 非流式对话：POST /chat/{run_id}（等整轮 agent，用长超时） */
  chat(runId: string, text: string, userId?: string, opts?: RequestOpts): Promise<ChatResponseOut> {
    const q = userId ? `?user_id=${encodeURIComponent(userId)}` : "";
    return request(
      `/chat/${encodeURIComponent(runId)}${q}`,
      { method: "POST", body: JSON.stringify({ text }) },
      opts,
      DEFAULT_LONG_TIMEOUT_MS,
    );
  },

  /** 查 run 状态：GET /status/{run_id} */
  status(runId: string, opts?: RequestOpts): Promise<{ run_id: string; status: string }> {
    return request(`/status/${encodeURIComponent(runId)}`, undefined, opts);
  },

  /** 审批队列：GET /approvals */
  approvals(opts?: RequestOpts): Promise<Approval[]> {
    return request("/approvals", undefined, opts);
  },

  /** 批准：POST /approve/{run_id}（会接着跑完那一轮，用长超时） */
  approve(runId: string, opts?: RequestOpts): Promise<ChatResponseOut> {
    return request(
      `/approve/${encodeURIComponent(runId)}`,
      { method: "POST" },
      opts,
      DEFAULT_LONG_TIMEOUT_MS,
    );
  },

  /** 拒绝：POST /reject/{run_id}（同批准，可能拿到 final 文本） */
  reject(runId: string, opts?: RequestOpts): Promise<ChatResponseOut> {
    return request(
      `/reject/${encodeURIComponent(runId)}`,
      { method: "POST" },
      opts,
      DEFAULT_LONG_TIMEOUT_MS,
    );
  },

  /** 能力：GET /capabilities */
  capabilities(opts?: RequestOpts): Promise<Capabilities> {
    return request("/capabilities", undefined, opts);
  },

  /** 对话记录：GET /messages/{run_id}（刷新后恢复聊天区） */
  messages(runId: string, opts?: RequestOpts): Promise<ChatMessage[]> {
    return request(`/messages/${encodeURIComponent(runId)}`, undefined, opts);
  },

  /** 对话列表：GET /runs?user_id=（最近活跃优先；带 user_id 只看该用户的） */
  runs(userId?: string, opts?: RequestOpts): Promise<RunInfo[]> {
    const q = userId ? `?user_id=${encodeURIComponent(userId)}` : "";
    return request(`/runs${q}`, undefined, opts);
  },

  /** 预创建会话：POST /runs/{run_id}?user_id=（幂等，归属当前用户） */
  createRun(
    runId: string,
    userId?: string,
    opts?: RequestOpts,
  ): Promise<{ run_id: string; status: string; user_id?: string }> {
    const q = userId ? `?user_id=${encodeURIComponent(userId)}` : "";
    return request(`/runs/${encodeURIComponent(runId)}${q}`, { method: "POST" }, opts);
  },

  /** 中控台用户列表：GET /users */
  users(opts?: RequestOpts): Promise<UserInfo[]> {
    return request("/users", undefined, opts);
  },

  /** 登记用户：POST /users（幂等） */
  createUser(userId: string, opts?: RequestOpts): Promise<{ ok: boolean; user_id: string }> {
    return request("/users", { method: "POST", body: JSON.stringify({ user_id: userId }) }, opts);
  },

  /** 审批决策历史：GET /approvals/history（最新在前） */
  approvalHistory(opts?: RequestOpts): Promise<ApprovalHistoryItem[]> {
    return request("/approvals/history", undefined, opts);
  },

  /** 删除会话：DELETE /runs/{run_id}（清掉状态/对话/审批记录） */
  deleteRun(runId: string, opts?: RequestOpts): Promise<{ ok: boolean }> {
    return request(`/runs/${encodeURIComponent(runId)}`, { method: "DELETE" }, opts);
  },

  /** 可用模型列表 + 当前模型：GET /models */
  models(opts?: RequestOpts): Promise<ModelsInfo> {
    return request("/models", undefined, opts);
  },

  /** 切换 / 导入模型：POST /models/select */
  selectModel(
    id: string,
    apiKey?: string,
    opts?: RequestOpts,
  ): Promise<{ ok: boolean; current: string }> {
    return request(
      "/models/select",
      { method: "POST", body: JSON.stringify({ id, api_key: apiKey || null }) },
      opts,
    );
  },

  /** 主动探一次 provider 通不通：POST /models/probe（只调 GET /models，不花 token） */
  probeModel(id: string, force = false, opts?: RequestOpts): Promise<ModelProbe> {
    return request(
      "/models/probe",
      { method: "POST", body: JSON.stringify({ id, force }) },
      opts,
    );
  },

  /** 记忆：GET /memory/{scope}（只回已生效的 ACTIVE 项） */
  memory(scope: string, opts?: RequestOpts): Promise<MemoryItem[]> {
    return request(`/memory/${encodeURIComponent(scope)}`, undefined, opts);
  },

  /** 待确认候选：GET /memory/{scope}/candidates。
   *
   * 为什么前端要拉这个：`memory.remember` 写的是“先进入待确认候选”，若候选在界面上
   * 看不见，那个“确认环节”就只是文案（内容写了但不生效，用户也不知道有东西在等）。 */
  memoryCandidates(scope: string, opts?: RequestOpts): Promise<MemoryCandidate[]> {
    return request(`/memory/${encodeURIComponent(scope)}/candidates`, undefined, opts);
  },

  /** 确认候选 → 生效：POST /memory/{scope}/candidates/{uid}/confirm */
  confirmMemoryCandidate(scope: string, uid: string, opts?: RequestOpts): Promise<unknown> {
    return request(
      `/memory/${encodeURIComponent(scope)}/candidates/${encodeURIComponent(uid)}/confirm`,
      { method: "POST" },
      opts,
    );
  },

  /** 拒绝候选（服务端软删，留审计痕迹）：POST /memory/{scope}/candidates/{uid}/reject */
  rejectMemoryCandidate(scope: string, uid: string, opts?: RequestOpts): Promise<unknown> {
    return request(
      `/memory/${encodeURIComponent(scope)}/candidates/${encodeURIComponent(uid)}/reject`,
      { method: "POST" },
      opts,
    );
  },

  /** 健康：GET /health/live 与 /health/ready */
  health(opts?: RequestOpts): Promise<HealthResult> {
    return request("/health/live", undefined, opts);
  },

  /** 读取一次流式对话（SSE），把事件一一回调给 onEvent */
  async streamChat(
    runId: string,
    text: string,
    onEvent: (ev: StreamEvent) => void,
    userId?: string,
    opts?: RequestOpts,
  ): Promise<void> {
    const q = userId ? `?user_id=${encodeURIComponent(userId)}` : "";
    const { signal, cleanup } = withTimeout(
      opts?.timeoutMs ?? DEFAULT_LONG_TIMEOUT_MS,
      opts?.signal,
    );
    // 见过任何一个事件？用来区分「后端在跑但连接断了」与「压根没连上」：
    // 前者要提示「回复仍在后台继续」并自动续读，后者该老老实实报错。
    let sawAny = false;
    try {
      const res = await fetch(`/chat/stream/${encodeURIComponent(runId)}${q}`, {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({ text }),
        signal,
      });
      if (!res.ok || !res.body) {
        onEvent({ type: "error", message: `${res.status}: ${await errorDetail(res)}` });
        return;
      }
      const sawTerminal = await readSseBody(res, signal, (ev) => {
        sawAny = true;
        onEvent(ev as StreamEvent);
      });
      // 流「结束」了却从没见过终态事件 = 连接被掐断（代理 idle 超时 / 切网 / 客户端被
      // 回收）。这时后端那一轮往往还在跑：报出去让 UI 提示「回复仍在后台继续」+ 自动
      // 续读；不报的话就是静默收尾——用户会以为模型就是这么敷衍地回答完了。
      // 主动取消（点停止 / 切会话）不算断线，静默结束。
      if (!sawTerminal && !signal.aborted) onEvent({ type: "disconnected" });
    } catch (e) {
      // 调用方主动取消（组件卸载/切会话）不算错误：静默结束，别往聊天区塞红条。
      // 超时则要报出来，否则用户只看到"没反应"。
      if (signal.aborted && !isTimeout(signal.reason)) return;
      const msg = e instanceof Error ? e.message : String(e);
      // 已经收到过事件、随后读流才抛（连接被重置 / chunked 编码不完整）：同属断线，
      // 后端那轮很可能还在跑——按断线处理，比只丢一条红字有用。
      if (!signal.aborted && sawAny) {
        onEvent({ type: "disconnected" });
        return;
      }
      onEvent({ type: "error", message: msg });
    } finally {
      cleanup();
    }
  },

  /**
   * 断线续读：订阅 GET /events/{run_id}，把事件总线上的终态事件回调出来。
   *
   * 为什么需要它：客户端断线（刷新页面 / 切网 / 中间层掐连接）时，后端那一轮仍在
   * 跑完，并把最终回答**同时**发一份到事件总线。前端连回这个端点就能把同一份结果
   * 捡回来——不这么做的话，用户"刷一下就重来"等于把这一轮回复和已经花掉的 token
   * 全丢了。收到终态事件即自行结束（有界，不挂长连接）。
   */
  async watchEvents(
    runId: string,
    onEvent: (ev: StreamEvent) => void,
    opts?: RequestOpts,
  ): Promise<void> {
    const { signal, cleanup } = withTimeout(
      opts?.timeoutMs ?? DEFAULT_LONG_TIMEOUT_MS,
      opts?.signal,
    );
    try {
      const res = await fetch(`/events/${encodeURIComponent(runId)}`, {
        method: "GET",
        headers: authHeaders(),
        signal,
      });
      if (!res.ok || !res.body) return;
      // 总线事件的形状是 {event: "final", text}（没有 type 字段）：在这里映射成流式
      // 事件，UI 侧就只需要处理一种事件类型（见 lib/types.ts 的 StreamEvent）。
      const busKind = (ev: unknown): string =>
        ev && typeof ev === "object" ? String((ev as { event?: unknown }).event ?? "") : "";
      const TERMINAL = ["final", "needs_approval", "error"];
      await readSseBody(
        res,
        signal,
        (ev) => {
          const r = ev as { text?: unknown; message?: unknown; approval?: unknown };
          const kind = busKind(ev);
          if (kind === "final") onEvent({ type: "final", text: String(r.text ?? "") });
          else if (kind === "needs_approval")
            onEvent({ type: "needs_approval", approval: r.approval as Approval });
          else if (kind === "error")
            onEvent({ type: "error", message: String(r.message ?? "连接中断") });
        },
        { stopOnTerminal: true, isTerminal: (ev) => TERMINAL.includes(busKind(ev)) },
      );
    } catch {
      /* 续读失败：静默交给调用方的兜底路径（重新加载会话） */
    } finally {
      cleanup();
    }
  },
};

export type {
  Approval,
  ApprovalHistoryItem,
  Capabilities,
  ChatMessage,
  HealthResult,
  MemoryCandidate,
  MemoryItem,
  ModelsInfo,
  RunInfo,
};
