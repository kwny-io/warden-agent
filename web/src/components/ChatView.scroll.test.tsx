// ChatView.tsx 的**滚动行为**：长会话打开时必须钉在最新一条，且不许在用户翻历史时抢视口。
//
// 为什么单独一个文件、而且不测像素：
//   这类缺陷在本机几乎不现形（快的机器上 React 提交总排在 requestAnimationFrame 之前），
//   只在慢环境/CI 上挂——实测 `/runs/36960948354` 的真浏览器 e2e 就是
//   `web/e2e/long-conversation.spec.ts:90` 的 `isPinnedToBottom` 15s 一直是 false。
//   jsdom 不做布局，所以这里**不验几何算得对不对**，只验一条因果：
//   **钉底不许依赖 rAF 的时机**。把 rAF 关掉（永不执行），历史仍然必须钉到最新一条。
//   旧实现（在 `.then()` 里 rAF 钉一次）会因此变红，新实现（useLayoutEffect 在提交后钉）绿。
//
// 第二条因果是"跟随开关"：用户把视口滚上去看历史时，流式 delta **不许**把他拽回底部；
//   他滚回底部、或再发一条消息，就重新跟随。
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import ChatView from "./ChatView";
import { api } from "../lib/api";
import type { StreamEvent } from "../lib/types";

/** 假的几何：真浏览器里 12 轮长文本的 scrollHeight 远大于可视高度。 */
const SCROLL_HEIGHT = 1000;
const CLIENT_HEIGHT = 300;

/** 与 e2e 里 `isPinnedToBottom` 同一口径（4px 容差）。 */
const isPinned = (el: HTMLElement): boolean =>
  el.scrollTop + CLIENT_HEIGHT >= SCROLL_HEIGHT - 4;

function messageBox(): HTMLElement {
  const el = document.querySelector('[aria-live="polite"]');
  if (!el) throw new Error("没找到消息区（aria-live=polite）");
  return el as HTMLElement;
}

/** jsdom 不做布局（scrollHeight/clientHeight 恒为 0），手动钉上几何再谈滚动。 */
function stubGeometry(el: HTMLElement): void {
  Object.defineProperty(el, "scrollHeight", { value: SCROLL_HEIGHT, configurable: true });
  Object.defineProperty(el, "clientHeight", { value: CLIENT_HEIGHT, configurable: true });
}

/** 用真实浏览器里会发生的"用户滚动"告知组件：jsdom 不会为程序化改 scrollTop 派发 scroll。 */
function scrollTo(el: HTMLElement, top: number): void {
  el.scrollTop = top;
  el.dispatchEvent(new Event("scroll"));
}

function renderChat() {
  return render(
    <ChatView
      runId="run-1"
      userId="u1"
      onApprovalAction={vi.fn()}
      onToggleRail={vi.fn()}
      railOpen
    />,
  );
}

const HISTORY = Array.from({ length: 12 }, (_, i) => [
  { role: "user", content: `第 ${i + 1} 轮问题` },
  { role: "assistant", content: `第 ${i + 1} 轮回答` },
]).flat();

let rafSpy: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.spyOn(api, "messages").mockResolvedValue([]);
  vi.spyOn(api, "streamChat").mockResolvedValue(undefined);
  vi.spyOn(api, "approve").mockResolvedValue({ run_id: "run-1", status: "ok", kind: "final" });
  vi.spyOn(api, "reject").mockResolvedValue({ run_id: "run-1", status: "ok", kind: "final" });
  // 默认把 rAF 变成**永不执行**：钉底如果依赖它，下面的断言就会红。
  rafSpy = vi.fn(() => 0 as unknown as number);
  vi.stubGlobal("requestAnimationFrame", rafSpy);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("ChatView 滚动：长会话钉底", () => {
  it("历史加载完就钉在最新一条 —— 即使 requestAnimationFrame 从不执行", async () => {
    vi.spyOn(api, "messages").mockResolvedValue(HISTORY);
    renderChat();
    const box = messageBox();
    stubGeometry(box);

    // 历史确实渲染了（不是"钉住了但内容没上屏"）
    expect(await screen.findByText("第 12 轮回答")).toBeInTheDocument();
    await waitFor(() => expect(isPinned(box)).toBe(true));
  });

  it("用户往上翻看历史时，流式 delta 不许把视口拽回底部；滚回底部或再发一条才恢复跟随", async () => {
    let push: ((ev: StreamEvent) => void) | null = null;
    vi.spyOn(api, "streamChat").mockImplementation(async (_runId, _text, onEvent) => {
      push = onEvent as (ev: StreamEvent) => void;
    });
    renderChat();
    const box = messageBox();
    stubGeometry(box);

    // 发一条：用户主动发消息 = 明确的"看最新"，必须钉底
    await userEvent.type(screen.getByPlaceholderText("给 Warden Agent 发消息…"), "hi");
    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(isPinned(box)).toBe(true));

    // 助手正在回答时用户往上翻 → delta 不许抢视口
    act(() => scrollTo(box, 0));
    expect(isPinned(box)).toBe(false);
    await act(async () => push?.({ type: "delta", text: "你" }));
    await new Promise((r) => setTimeout(r, 0));
    expect(isPinned(box)).toBe(false);

    // 自己滚回底部 → 恢复跟随
    act(() => scrollTo(box, SCROLL_HEIGHT));
    await act(async () => push?.({ type: "delta", text: "好" }));
    await waitFor(() => expect(isPinned(box)).toBe(true));

    // 又翻上去，但这次**主动发消息** → 立刻回到最新
    act(() => scrollTo(box, 0));
    await userEvent.type(screen.getByPlaceholderText("给 Warden Agent 发消息…"), "再看");
    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(isPinned(box)).toBe(true));
  });

  it("用户正在翻历史时来了审批请求 → 必须强行钉到最新（看不到就会一直卡在等人操作）", async () => {
    let push: ((ev: StreamEvent) => void) | null = null;
    vi.spyOn(api, "streamChat").mockImplementation(async (_runId, _text, onEvent) => {
      push = onEvent as (ev: StreamEvent) => void;
    });
    renderChat();
    const box = messageBox();
    stubGeometry(box);

    await userEvent.type(screen.getByPlaceholderText("给 Warden Agent 发消息…"), "删文件");
    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(isPinned(box)).toBe(true));

    // 用户上翻看历史 → 普通的 delta 不抢视口（上一条用例已验）
    act(() => scrollTo(box, 0));
    // ……但审批是"必须被看见"的：它不清掉，run 就停在等审批上
    await act(async () =>
      push?.({
        type: "needs_approval",
        approval: {
          approval_id: "ap-1",
          run_id: "run-1",
          tool_name: "shell.exec",
          arguments: {},
          reason: "高危",
        },
      }),
    );
    await waitFor(() => expect(isPinned(box)).toBe(true));
    expect(screen.getByText(/需要审批/)).toBeInTheDocument();
  });

  it("切换会话会重新打开跟随：上一个会话里翻过历史，新会话打开也必须钉在最新", async () => {
    vi.spyOn(api, "messages").mockImplementation(async (runId: string) =>
      runId === "run-2" ? HISTORY : [],
    );
    const { rerender } = renderChat();
    const box = messageBox();
    stubGeometry(box);

    // run-1：用户翻上去看历史 → 跟随关闭
    act(() => scrollTo(box, 0));
    expect(isPinned(box)).toBe(false);

    // 切到 run-2：这是**新的一段对话**，必须重新跟随（否则新会话打开就停在顶部）
    rerender(
      <ChatView
        runId="run-2"
        userId="u1"
        onApprovalAction={vi.fn()}
        onToggleRail={vi.fn()}
        railOpen
      />,
    );
    expect(await screen.findByText("第 12 轮回答")).toBeInTheDocument();
    await waitFor(() => expect(isPinned(box)).toBe(true));
  });
});
