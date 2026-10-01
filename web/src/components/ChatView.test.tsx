// ChatView.tsx —— 核心对话区。
// 这是全站最关键、交互最复杂的组件：发送、SSE 打字机、审批气泡的批准/拒绝、
// 历史恢复、以及畸形历史数据不白屏。全部通过 mock api.* 驱动，不发真实请求。
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ChatView, { type DisplayMsg } from "./ChatView";
import { api } from "../lib/api";
import type { StreamEvent } from "../lib/types";

type StreamFn = (ev: StreamEvent) => void;

function setup(overrides: Partial<Parameters<typeof ChatView>[0]> = {}) {
  const props = {
    runId: "run-1",
    userId: "u1",
    onApprovalAction: vi.fn(),
    onToggleRail: vi.fn(),
    railOpen: true,
    ...overrides,
  };
  const utils = render(<ChatView {...props} />);
  return { ...props, ...utils };
}

/** 让 streamChat 按给定事件序列同步回调。 */
function emit(events: StreamEvent[]) {
  return vi.spyOn(api, "streamChat").mockImplementation(async (_runId, _text, onEvent) => {
    for (const ev of events) (onEvent as StreamFn)(ev);
  });
}

beforeEach(() => {
  vi.spyOn(api, "messages").mockResolvedValue([]);
  vi.spyOn(api, "streamChat").mockResolvedValue(undefined);
  vi.spyOn(api, "approve").mockResolvedValue({ run_id: "run-1", status: "ok", kind: "final" });
  vi.spyOn(api, "reject").mockResolvedValue({ run_id: "run-1", status: "ok", kind: "final" });
});

afterEach(() => vi.restoreAllMocks());

describe("ChatView 发送与流式渲染", () => {
  it("输入后按 Enter：用户气泡出现，并按当前 run/用户调 streamChat", async () => {
    setup();
    const ta = screen.getByPlaceholderText("给 Warden Agent 发消息…");
    await userEvent.type(ta, "你好");
    await userEvent.keyboard("{Enter}");
    expect(await screen.findByText("你好")).toBeInTheDocument();
    expect(api.streamChat).toHaveBeenCalledWith("run-1", "你好", expect.any(Function), "u1", {
      signal: expect.any(AbortSignal),
    });
  });

  it("空输入不发送（streamChat 不被调用）", async () => {
    setup();
    await userEvent.type(screen.getByPlaceholderText("给 Warden Agent 发消息…"), "   ");
    await userEvent.keyboard("{Enter}");
    expect(api.streamChat).not.toHaveBeenCalled();
  });

  it("delta 逐字累积、final 收尾：最终气泡显示完整文本", async () => {
    emit([
      { type: "delta", text: "你" },
      { type: "delta", text: "好世" },
      { type: "final", text: "你好世界" },
    ]);
    setup();
    await userEvent.type(screen.getByPlaceholderText("给 Warden Agent 发消息…"), "hi");
    await userEvent.keyboard("{Enter}");
    expect(await screen.findByText("你好世界")).toBeInTheDocument();
    // 不应残留 delta 拼接出来的旧文本节点
    expect(screen.queryByText("你好世")).toBeNull();
  });

  it("tool 事件渲染成独立的工具调用气泡", async () => {
    emit([
      { type: "tool", name: "shell.exec", arguments: { cmd: "ls" } },
      { type: "final", text: "done" },
    ]);
    setup();
    await userEvent.type(screen.getByPlaceholderText("给 Warden Agent 发消息…"), "ls");
    await userEvent.keyboard("{Enter}");
    expect(await screen.findByText("🔧 调用工具：shell.exec")).toBeInTheDocument();
    expect(screen.getByText("done")).toBeInTheDocument();
  });

  it("error 事件在输入区上方显示错误条", async () => {
    emit([{ type: "error", message: "429: 触发限流" }]);
    setup();
    await userEvent.type(screen.getByPlaceholderText("给 Warden Agent 发消息…"), "hi");
    await userEvent.keyboard("{Enter}");
    expect(await screen.findByText(/429: 触发限流/)).toBeInTheDocument();
  });

  it("streamChat 抛错（网络断开）时也走错误条，不卡在 busy", async () => {
    vi.spyOn(api, "streamChat").mockRejectedValue(new Error("Failed to fetch"));
    setup();
    const ta = screen.getByPlaceholderText("给 Warden Agent 发消息…");
    await userEvent.type(ta, "hi");
    await userEvent.keyboard("{Enter}");
    expect(await screen.findByText(/Failed to fetch/)).toBeInTheDocument();
    await waitFor(() => expect(ta).not.toBeDisabled());
  });
});

describe("ChatView 审批气泡", () => {
  async function askForApproval() {
    emit([
      {
        type: "needs_approval",
        approval: {
          approval_id: "ap-1",
          run_id: "run-1",
          tool_name: "shell.exec",
          arguments: { cmd: "rm -rf" },
          reason: "高危",
        },
      },
    ]);
    setup();
    await userEvent.type(screen.getByPlaceholderText("给 Warden Agent 发消息…"), "删除文件");
    await userEvent.keyboard("{Enter}");
    return screen.findByText(/需要审批/);
  }

  it("收到 needs_approval 渲染审批卡片（工具名/参数）", async () => {
    await askForApproval();
    expect(screen.getByText("shell.exec")).toBeInTheDocument();
    expect(screen.getByText(/rm -rf/)).toBeInTheDocument();
  });

  // 审批改成两步（治理动作误点即执行高危工具），所以所有用例都必须点两次。
  // 用 helper 收口，避免每个用例各写一遍。
  async function clickTwice(name: string, confirm: string) {
    await userEvent.click(screen.getByRole("button", { name }));
    await userEvent.click(screen.getByRole("button", { name: confirm }));
  }

  it("点批准：调 api.approve(runId) 并移除审批气泡", async () => {
    await askForApproval();
    await clickTwice("✅ 批准", "确认批准？");
    await waitFor(() => expect(api.approve).toHaveBeenCalledWith("run-1"));
    await waitFor(() => expect(screen.queryByText(/需要审批/)).toBeNull());
  });

  it("批准需要二次确认：第一次点击绝不触发 api.approve（误点不执行高危工具）", async () => {
    await askForApproval();
    await userEvent.click(screen.getByRole("button", { name: "✅ 批准" }));
    expect(api.approve).not.toHaveBeenCalled();
    // 按钮切成确认态，并给出取消路径
    expect(screen.getByRole("button", { name: "确认批准？" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "取消" })).toBeInTheDocument();
    // 点取消则退回初始态，依旧不会调后端
    await userEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.getByRole("button", { name: "✅ 批准" })).toBeInTheDocument();
    expect(api.approve).not.toHaveBeenCalled();
  });

  it("批准时后端报「没有等待审批」（已被别处处理）：静默清掉过期气泡，不弹错误", async () => {
    vi.spyOn(api, "approve").mockRejectedValue(new Error("409: 没有等待审批的记录"));
    const { onApprovalAction } = setup();
    emit([
      {
        type: "needs_approval",
        approval: {
          approval_id: "ap-1",
          run_id: "run-1",
          tool_name: "shell.exec",
          arguments: {},
          reason: "",
        },
      },
    ]);
    const ta = screen.getByPlaceholderText("给 Warden Agent 发消息…");
    await userEvent.type(ta, "go");
    await userEvent.keyboard("{Enter}");
    await screen.findByText(/需要审批/);
    await clickTwice("✅ 批准", "确认批准？");
    await waitFor(() => expect(screen.queryByText(/需要审批/)).toBeNull());
    expect(screen.queryByText(/没有等待审批/)).toBeNull();
    expect(onApprovalAction).toHaveBeenCalled();
  });

  it("拒绝：调 api.reject 并移除审批气泡", async () => {
    await askForApproval();
    await clickTwice("🚫 拒绝", "确认拒绝？");
    await waitFor(() => expect(api.reject).toHaveBeenCalledWith("run-1"));
    await waitFor(() => expect(screen.queryByText(/需要审批/)).toBeNull());
  });
});

describe("ChatView 历史恢复与畸形数据", () => {
  it("从 /messages 恢复历史：user/assistant 文本与 tool 调用（system 不入聊天区）", async () => {
    vi.spyOn(api, "messages").mockResolvedValue([
      { role: "system", content: "你是一个助手" },
      { role: "user", content: "帮我查天气" },
      { role: "assistant", tool_call: { name: "web.search", arguments: {} } },
      { role: "assistant", content: "今天晴" },
    ]);
    render(
      <ChatView
        runId="run-1"
        userId="u1"
        onApprovalAction={vi.fn()}
        onToggleRail={vi.fn()}
        railOpen
      />,
    );
    expect(await screen.findByText("帮我查天气")).toBeInTheDocument();
    expect(screen.getByText("🔧 调用工具：web.search")).toBeInTheDocument();
    expect(screen.getByText("今天晴")).toBeInTheDocument();
    expect(screen.queryByText("你是一个助手")).toBeNull();
  });

  it("后端返回畸形/空历史（null、缺字段、错误类型）时退化为空态而不是白屏", async () => {
    vi.spyOn(api, "messages").mockResolvedValue([
      { role: "assistant" } as unknown as DisplayMsg,
      {} as unknown as DisplayMsg,
      123 as unknown as DisplayMsg,
      { role: "assistant", tool_call: null, content: null } as unknown as DisplayMsg,
    ]);
    render(
      <ChatView
        runId="run-1"
        userId="u1"
        onApprovalAction={vi.fn()}
        onToggleRail={vi.fn()}
        railOpen
      />,
    );
    // 无有效消息 → 展示欢迎空态
    expect(await screen.findByText("有什么可以帮你？")).toBeInTheDocument();
  });

  it("历史消息 content 为 null 不崩：user 气泡渲染为空文本", async () => {
    vi.spyOn(api, "messages").mockResolvedValue([{ role: "user", content: null }]);
    const { container } = render(
      <ChatView
        runId="run-1"
        userId="u1"
        onApprovalAction={vi.fn()}
        onToggleRail={vi.fn()}
        railOpen
      />,
    );
    // 空态消失（说明确实渲染了一条气泡），且没有抛错/白屏
    await waitFor(() => expect(screen.queryByText("有什么可以帮你？")).toBeNull());
    expect(container.textContent).not.toContain("null");
  });

  it("/messages 请求失败时保持空态并可用，不抛错", async () => {
    vi.spyOn(api, "messages").mockRejectedValue(new Error("500: boom"));
    render(
      <ChatView
        runId="run-1"
        userId="u1"
        onApprovalAction={vi.fn()}
        onToggleRail={vi.fn()}
        railOpen
      />,
    );
    expect(await screen.findByText("有什么可以帮你？")).toBeInTheDocument();
  });

  it("历史里混入 null / 数字等非对象元素：走窄守卫，保住有效消息且不崩", async () => {
    vi.spyOn(api, "messages").mockResolvedValue([
      null as unknown as DisplayMsg,
      42 as unknown as DisplayMsg,
      { role: "user", content: "存活消息" } as unknown as DisplayMsg,
    ]);
    render(
      <ChatView
        runId="run-1"
        userId="u1"
        onApprovalAction={vi.fn()}
        onToggleRail={vi.fn()}
        railOpen
      />,
    );
    expect(await screen.findByText("存活消息")).toBeInTheDocument();
  });
});

describe("ChatView 取消与可访问性", () => {
  it("输入框、对话栏开关有可访问名（不只靠 placeholder/title）", async () => {
    render(
      <ChatView
        runId="run-1"
        userId="u1"
        onApprovalAction={vi.fn()}
        onToggleRail={vi.fn()}
        railOpen
      />,
    );
    await screen.findByText("有什么可以帮你？"); // 等历史拉取落地，避免测试结束后 setState
    expect(screen.getByLabelText("给 Warden Agent 发消息")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "收起对话列表" })).toBeInTheDocument();
  });

  it("卸载时 abort 在途流式请求（reader 会被 cancel，不会挂着读）", async () => {
    let captured: AbortSignal | null | undefined;
    vi.spyOn(api, "streamChat").mockImplementation(
      async (_runId, _text, _onEvent, _userId, opts) => {
        captured = opts?.signal;
        await new Promise<void>(() => {}); // 永不结束：模拟长回答
      },
    );
    const { unmount } = setup();
    await userEvent.type(screen.getByPlaceholderText("给 Warden Agent 发消息…"), "hi");
    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(captured).toBeDefined());
    expect(captured?.aborted).toBe(false);
    unmount();
    expect(captured?.aborted).toBe(true);
  });

  it("切会话（runId 变化）：abort 在途流，旧会话事件不写进新会话，输入框解锁", async () => {
    let captured: AbortSignal | null | undefined;
    let fire: StreamFn = () => {};
    vi.spyOn(api, "streamChat").mockImplementation(
      async (_runId, _text, onEvent, _userId, opts) => {
        captured = opts?.signal;
        fire = onEvent as StreamFn;
        await new Promise<void>(() => {}); // 长任务还在跑
      },
    );
    const { rerender } = render(
      <ChatView
        runId="run-A"
        userId="u1"
        onApprovalAction={vi.fn()}
        onToggleRail={vi.fn()}
        railOpen
      />,
    );
    const ta = screen.getByPlaceholderText("给 Warden Agent 发消息…");
    await userEvent.type(ta, "跑个长任务");
    await userEvent.keyboard("{Enter}");
    await screen.findByText("跑个长任务");
    expect(ta).toBeDisabled(); // run-A 正在生成

    rerender(
      <ChatView
        runId="run-B"
        userId="u1"
        onApprovalAction={vi.fn()}
        onToggleRail={vi.fn()}
        railOpen
      />,
    );

    // ① 连接被真正断掉（不是只丢结果），否则后端还在算、事件还在往回送
    expect(captured?.aborted).toBe(true);
    // ② 旧会话的迟到事件一律丢弃：不能污染新会话的消息区
    fire({ type: "delta", text: "runA的回答" });
    fire({ type: "final", text: "runA的回答" });
    await waitFor(() => expect(screen.queryByText(/runA的回答/)).toBeNull());
    // ③ busy 归零：新会话能发消息（否则输入框永久 disabled，交互锁死）
    expect(ta).not.toBeDisabled();
    expect(screen.queryByText("跑个长任务")).toBeNull(); // 新会话消息区已清空
  });
});

describe("ChatView 停止生成", () => {
  // 可控流：先回调一个 delta，然后挂起——直到「被 abort」由测试决定何时收尾。
  // deferreds 让测试能把「旧流的 finally」精确地排在「新流已经开始」之后，
  // 这才是真实场景（用户点停止后立刻发下一条，旧连接的收尾晚到）。
  function controllableStream() {
    const signals: AbortSignal[] = [];
    const deferreds: Array<() => void> = [];
    vi.spyOn(api, "streamChat").mockImplementation(async (_r, _t, onEvent, _u, opts) => {
      if (opts?.signal) signals.push(opts.signal);
      (onEvent as StreamFn)({ type: "delta", text: "半句" });
      await new Promise<void>((resolve) => deferreds.push(resolve));
    });
    return { signals, deferreds };
  }

  async function submit(text: string) {
    const ta = screen.getByPlaceholderText("给 Warden Agent 发消息…");
    await userEvent.type(ta, text);
    await userEvent.keyboard("{Enter}");
    return ta;
  }

  it("生成中点「停止生成」：abort 在途流、已生成内容保留、输入框立即可用", async () => {
    setup();
    const { signals } = controllableStream();
    const ta = await submit("写一篇长文");
    await screen.findByText("半句");
    expect(ta).toBeDisabled(); // 生成中不许再发第二条

    await userEvent.click(screen.getByRole("button", { name: "停止生成" }));

    expect(signals[0]?.aborted).toBe(true);
    await waitFor(() => expect(ta).not.toBeDisabled());
    // 已经吐出来的部分不能被清掉：用户看到的是「停在这」而不是「全没了」
    expect(screen.getByText("半句")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("button", { name: "停止生成" })).toBeNull());
    // 主动停止不是错误：不该弹红条
    expect(screen.queryByText(/AbortError|超时/)).toBeNull();
  });

  it("停止后立刻再发一条：旧流的迟到收尾不影响新流的状态与停止按钮", async () => {
    setup();
    const { signals, deferreds } = controllableStream();

    const ta = await submit("第一条");
    await screen.findByText("半句");
    await userEvent.click(screen.getByRole("button", { name: "停止生成" }));

    const ta2 = await submit("第二条");
    expect(ta2).toBe(ta);
    await screen.findByRole("button", { name: "停止生成" });

    deferreds[0]?.(); // 第一条流现在才真正结束（它的 finally 迟到）

    // 旧流的 finally 不能把第二条的「生成中」抹掉，否则用户能再发第三条、
    // 两条回答会交错写进同一个气泡
    await waitFor(() => expect(ta2).toBeDisabled());

    await userEvent.click(screen.getByRole("button", { name: "停止生成" }));
    expect(signals[1]?.aborted).toBe(true);
    await waitFor(() => expect(ta2).not.toBeDisabled());
  });
});

/** 让 streamChat 先吐给定事件，再补一个「断线」信号（模拟代理 idle 超时掐连接）。 */
function emitThenBreak(events: StreamEvent[]) {
  return vi.spyOn(api, "streamChat").mockImplementation(async (_runId, _text, onEvent) => {
    for (const ev of events) (onEvent as StreamFn)(ev);
    (onEvent as StreamFn)({ type: "disconnected" });
  });
}

describe("ChatView 断线续读", () => {
  it("断线后提示「仍在后台继续」并自动从 /events 续读，拿到 final 就补齐回答", async () => {
    emitThenBreak([{ type: "delta", text: "上半" }]);
    const watch = vi.spyOn(api, "watchEvents").mockImplementation(async (_runId, onEvent) => {
      // 后端那一轮仍在跑完，并把同一份 final 发到了事件总线。
      // 这里故意推迟 300ms：真实链路上续读不可能在同一 tick 就返回，而「连接中断」提示
      // 必须在这段时间里是**可见**的——否则用户看到的就是静默卡住。
      // （推迟太短反而会漏测：拿到 final 后提示会被撤销，断言可能扑空。）
      await new Promise((r) => setTimeout(r, 300));
      (onEvent as StreamFn)({ type: "final", text: "上半段下半段" });
    });
    setup();
    await userEvent.type(screen.getByPlaceholderText("给 Warden Agent 发消息…"), "hi");
    await userEvent.keyboard("{Enter}");
    // 同步断言：断线信号在 streamChat 返回之前就发了，此刻提示已经渲染出来
    expect(screen.getByText(/连接中断/)).toBeInTheDocument();
    expect(screen.getByText(/续读/)).toBeInTheDocument();
    expect(await screen.findByText("上半段下半段")).toBeInTheDocument();
    expect(watch).toHaveBeenCalledTimes(1);
    expect(watch).toHaveBeenCalledWith(
      "run-1",
      expect.any(Function),
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    // 续读成功就撤掉提示，不留一条已经过期的告警
    await waitFor(() => expect(screen.queryByText(/连接中断/)).toBeNull());
  });

  it("续读通道也不可用：改成「未能取回」并给「重新加载会话」兜底", async () => {
    emitThenBreak([]);
    vi.spyOn(api, "watchEvents").mockRejectedValue(new Error("events 不可用"));
    // 第一次是挂载时的历史加载（保持空），第二次才是兜底重载
    vi.spyOn(api, "messages")
      .mockResolvedValueOnce([])
      .mockResolvedValue([{ role: "assistant", content: "后台跑完的回答" }]);
    setup();
    await userEvent.type(screen.getByPlaceholderText("给 Warden Agent 发消息…"), "hi");
    await userEvent.keyboard("{Enter}");
    expect(await screen.findByText(/未能取回/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "重新加载会话" }));
    expect(await screen.findByText("后台跑完的回答")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText(/未能取回/)).toBeNull());
  });

  it("正常说完（没有断线信号）：既不提示，也不去连 events", async () => {
    emit([
      { type: "delta", text: "hi" },
      { type: "final", text: "完整回答" },
    ]);
    const watch = vi.spyOn(api, "watchEvents");
    setup();
    await userEvent.type(screen.getByPlaceholderText("给 Warden Agent 发消息…"), "hi");
    await userEvent.keyboard("{Enter}");
    expect(await screen.findByText("完整回答")).toBeInTheDocument();
    expect(screen.queryByText(/连接中断/)).toBeNull();
    expect(watch).not.toHaveBeenCalled();
  });

  it("用户中途点了停止：断线信号迟到也不再自动续读（不擅自再拉一轮）", async () => {
    // 用数组装回调而不是裸 let：TS 对「只在闭包里赋值、之后才读」的 let 会窄化成 never
    const fire: Array<StreamFn> = [];
    const release: Array<() => void> = [];
    // 挂住这一轮：busy 保持 true，才能看到「停止」按钮
    vi.spyOn(api, "streamChat").mockImplementation(async (_runId, _text, onEvent) => {
      fire.push(onEvent as StreamFn);
      await new Promise<void>((res) => {
        release.push(res);
      });
    });
    const watch = vi.spyOn(api, "watchEvents");
    setup();
    await userEvent.type(screen.getByPlaceholderText("给 Warden Agent 发消息…"), "hi");
    await userEvent.keyboard("{Enter}");
    await userEvent.click(await screen.findByRole("button", { name: /停止/ }));
    fire[0]?.({ type: "disconnected" });
    release[0]?.();
    await waitFor(() => expect(watch).not.toHaveBeenCalled());
  });
});

// 侧栏审批队列是**另一条入口**：在那边点「批准」不经过 ChatView，但服务端已经把这一轮
// 接着跑完并把回答写进了会话记录。App 用自增的 refreshTick 通知聊天区"去重拉一次"，
// 否则用户会觉得"我批了，可是聊天区什么都没变"（浏览器 e2e 就是这么撞出来的）。
describe("ChatView 侧栏审批决策后的重拉（refreshTick）", () => {
  const baseProps = {
    runId: "run-1",
    userId: "u1",
    onApprovalAction: vi.fn(),
    onToggleRail: vi.fn(),
    railOpen: true,
  };

  it("refreshTick 变化后重拉会话，把恢复后的回答显示出来", async () => {
    const messages = vi.spyOn(api, "messages");
    messages.mockResolvedValueOnce([]);
    messages.mockResolvedValue([
      { role: "user", content: "帮我改点东西" },
      { role: "assistant", content: "已批准并执行完成" },
    ] as never);

    const { rerender } = render(<ChatView {...baseProps} refreshTick={0} />);
    await waitFor(() => expect(messages).toHaveBeenCalledTimes(1));

    rerender(<ChatView {...baseProps} refreshTick={1} />);

    expect(await screen.findByText("已批准并执行完成")).toBeInTheDocument();
    expect(messages.mock.calls.length).toBeGreaterThan(1);
  });

  it("初次挂载（refreshTick=0）不额外多拉一次：runId 那个 effect 已经拉过了", async () => {
    render(<ChatView {...baseProps} refreshTick={0} />);
    await waitFor(() => expect(api.messages).toHaveBeenCalledTimes(1));
    // 再渲染一次同样的 0，也不该触发
    await waitFor(() => expect(api.messages).toHaveBeenCalledTimes(1));
  });

  it("有在途流式请求时不抢：不重拉、不打断打字机", async () => {
    // 挂住这一轮：busy 保持 true，模拟"正在流式生成"
    vi.spyOn(api, "streamChat").mockImplementation(async () => {
      await new Promise<void>(() => {});
    });
    const messages = vi.spyOn(api, "messages");

    const { rerender } = render(<ChatView {...baseProps} refreshTick={0} />);
    await waitFor(() => expect(messages).toHaveBeenCalledTimes(1));

    await userEvent.type(screen.getByPlaceholderText("给 Warden Agent 发消息…"), "hi");
    await userEvent.keyboard("{Enter}");
    await screen.findByRole("button", { name: /停止/ });

    rerender(<ChatView {...baseProps} refreshTick={1} />);

    // 在途流的 finally 会自己把结果收尾，这里绝不能插一脚重拉
    expect(messages).toHaveBeenCalledTimes(1);
    // 打字机还在（停止按钮仍在 = busy 没被清掉）
    expect(screen.getByRole("button", { name: /停止/ })).toBeInTheDocument();
  });
});

describe("ChatView 工具结果上屏", () => {
  // 这一组盯的是一个真实的“信息黑洞”：工具真的跑了、真的输了东西，
  // 但界面上以前只有“🔧 调用工具：x”——用户看不到输出，也不知道成没成。
  // 同时它把 display.ts 里的“成败润色前缀”契约钉住：前缀改了这里必须红。

  async function send(text = "查一下"): Promise<void> {
    await userEvent.type(screen.getByPlaceholderText("给 Warden Agent 发消息…"), text);
    await userEvent.keyboard("{Enter}");
  }

  it("成功的工具结果：上屏标题+正文", async () => {
    emit([
      { type: "tool", name: "weather.get", arguments: { city: "上海" } },
      { type: "tool_result", name: "weather.get", ok: true, text: "上海 晴 26℃" },
      { type: "final", text: "上海今天晴。" },
    ]);
    setup();
    await send();
    expect(await screen.findByText("工具结果")).toBeInTheDocument();
    expect(screen.getByText("上海 晴 26℃")).toBeInTheDocument();
    // 调用气泡与结果气泡都要在（少了任何一个都是丢信息）
    expect(screen.getByText(/调用工具：weather\.get/)).toBeInTheDocument();
  });

  it("失败的工具结果：标成“工具执行失败”（一眼能看出没成）", async () => {
    emit([
      { type: "tool", name: "db.query", arguments: {} },
      {
        type: "tool_result",
        name: "db.query",
        ok: false,
        text: "[工具 db.query 执行失败，请修正后重试] TimeoutError",
      },
    ]);
    setup();
    await send();
    expect(await screen.findByText("工具执行失败")).toBeInTheDocument();
    expect(screen.queryByText("工具结果")).not.toBeInTheDocument();
  });

  it("长结果默认折叠，点“展开全部”后能看到全部行", async () => {
    const long = Array.from({ length: 12 }, (_, i) => `第${i + 1}行`).join("\n");
    emit([{ type: "tool_result", name: "files.read", ok: true, text: long }]);
    const { container } = setup();
    await send();
    const pre = await waitFor(() => {
      const el = container.querySelector("pre");
      if (!el) throw new Error("还没有 pre");
      return el;
    });
    // 默认只显示前 6 行（COLLAPSED_LINES），后面的行必须在
    expect(pre.textContent).toContain("第1行");
    expect(pre.textContent).not.toContain("第12行");
    await userEvent.click(screen.getByRole("button", { name: /展开全部（共 12 行）/ }));
    expect(container.querySelector("pre")?.textContent).toContain("第12行");
  });

  it("刷新后从历史恢复：落库的 tool 消息同样渲染成工具结果（两条路径同一个转换）", async () => {
    // 以前“刷新页面跳过 tool 消息、断线兜底却渲染”，同一份历史长得不一样。
    vi.spyOn(api, "messages").mockResolvedValue([
      { role: "user", content: "跑一下" },
      { role: "tool", content: "[工具 shell.exec 执行失败，请修正后重试] 没找到命令" },
    ]);
    setup();
    expect(await screen.findByText("工具执行失败")).toBeInTheDocument();
    expect(screen.getByText(/没找到命令/)).toBeInTheDocument();
  });
});
