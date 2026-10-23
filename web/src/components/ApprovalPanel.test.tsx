// ApprovalPanel.tsx —— 审批队列面板。
// 断言的是**行为**：点批准/拒绝真的调后端对应接口并刷新队列、失败有兜底提示、
// 后端返回缺字段的畸形审批记录时面板不会白屏。
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ApprovalPanel from "./ApprovalPanel";
import { api } from "../lib/api";
import type { Approval } from "../lib/types";

const pending: Approval[] = [
  {
    approval_id: "ap-1",
    run_id: "run-1",
    tool_name: "git.apply_patch",
    arguments: { patch: "..." },
    reason: "高危命令",
  },
];

beforeEach(() => {
  vi.spyOn(api, "approvals").mockResolvedValue(pending);
  vi.spyOn(api, "approvalHistory").mockResolvedValue([]);
  vi.spyOn(window, "alert").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ApprovalPanel", () => {
  it("默认展示中文操作名（不是原始工具名）与原因", async () => {
    render(<ApprovalPanel />);
    // 默认人话：`git.apply_patch` → 「应用代码补丁」
    expect(await screen.findByText("应用代码补丁")).toBeInTheDocument();
    expect(screen.queryByText("git.apply_patch")).toBeNull();
    expect(screen.getByText(/高危命令/)).toBeInTheDocument();
    expect(screen.getByText(/run-1/)).toBeInTheDocument();
  });

  it("点「原始」切回工具名与原始标识，再点「通俗」切回来", async () => {
    render(<ApprovalPanel />);
    await screen.findByText("应用代码补丁");
    await userEvent.click(screen.getByRole("button", { name: "显示原始标识" }));
    expect(screen.getByText("git.apply_patch")).toBeInTheDocument();
    expect(screen.queryByText("应用代码补丁")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "隐藏原始标识" }));
    expect(screen.getByText("应用代码补丁")).toBeInTheDocument();
  });

  it("/approvals 拉取失败：报错且**不**说「暂无待审批」（队列里也许有人等着批）", async () => {
    vi.spyOn(api, "approvals").mockRejectedValue(new Error("503: 服务不可用"));
    render(<ApprovalPanel />);
    expect(await screen.findByText(/⚠️ 503: 服务不可用/)).toBeInTheDocument();
    expect(screen.queryByText("（暂无待审批）")).toBeNull();
  });

  it("/approvals/history 拉取失败：说出来，而不是当成「从没审批过」", async () => {
    vi.spyOn(api, "approvalHistory").mockRejectedValue(new Error("500: boom"));
    render(<ApprovalPanel />);
    expect(await screen.findByText(/审批历史加载失败：500: boom/)).toBeInTheDocument();
  });

  it("队列为空时给出空态文案", async () => {
    vi.spyOn(api, "approvals").mockResolvedValue([]);
    render(<ApprovalPanel />);
    expect(await screen.findByText("（暂无待审批）")).toBeInTheDocument();
  });

  it("点「批准」调用 api.approve(run_id) 并重新拉取队列", async () => {
    const approve = vi.spyOn(api, "approve").mockResolvedValue({
      run_id: "run-1",
      status: "ok",
      kind: "final",
    });
    render(<ApprovalPanel />);
    await screen.findByText("应用代码补丁");
    const before = (api.approvals as ReturnType<typeof vi.fn>).mock.calls.length;

    await userEvent.click(screen.getByRole("button", { name: "批准" }));

    expect(approve).toHaveBeenCalledWith("run-1");
    await waitFor(() =>
      expect((api.approvals as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(before),
    );
  });

  it("决策成功后回调 onDecided：让聊天区知道「这一轮恢复了、去重拉会话」", async () => {
    vi.spyOn(api, "approve").mockResolvedValue({
      run_id: "run-1",
      status: "ok",
      kind: "final",
    });
    const onDecided = vi.fn();
    render(<ApprovalPanel onDecided={onDecided} />);
    await screen.findByText("应用代码补丁");

    await userEvent.click(screen.getByRole("button", { name: "批准" }));

    await waitFor(() => expect(onDecided).toHaveBeenCalledTimes(1));
  });

  it("决策失败时**不**回调 onDecided（别让上层以为这一轮已经恢复）", async () => {
    vi.spyOn(api, "approve").mockRejectedValue(new Error("没有等待审批的请求"));
    const onDecided = vi.fn();
    render(<ApprovalPanel onDecided={onDecided} />);
    await screen.findByText("应用代码补丁");

    await userEvent.click(screen.getByRole("button", { name: "批准" }));

    await waitFor(() => expect(window.alert).toHaveBeenCalled());
    expect(onDecided).not.toHaveBeenCalled();
  });

  it("点「拒绝」调用 api.reject(run_id)", async () => {    const reject = vi.spyOn(api, "reject").mockResolvedValue({
      run_id: "run-1",
      status: "ok",
      kind: "final",
    });
    render(<ApprovalPanel />);
    await screen.findByText("应用代码补丁");
    await userEvent.click(screen.getByRole("button", { name: "拒绝" }));
    expect(reject).toHaveBeenCalledWith("run-1");
  });

  it("批准接口失败时走兜底提示，不抛未捕获异常", async () => {
    vi.spyOn(api, "approve").mockRejectedValue(new Error("409: 没有等待审批的记录"));
    render(<ApprovalPanel />);
    await screen.findByText("应用代码补丁");
    await userEvent.click(screen.getByRole("button", { name: "批准" }));
    await waitFor(() => expect(window.alert).toHaveBeenCalledWith("409: 没有等待审批的记录"));
  });

  it("轮询失败时在面板内显示错误，而不是整片白屏；且不说「（暂无待审批）」", async () => {
    vi.spyOn(api, "approvals").mockRejectedValue(new Error("503: 服务不可用"));
    render(<ApprovalPanel />);
    expect(await screen.findByText(/503: 服务不可用/)).toBeInTheDocument();
    // 第五十五批改了这里：原先断言“失败时依然渲染空态骨架”，
    // 而那句「（暂无待审批）」在拉取失败的那一刻是**假话**（队列里也许有人等着批）。
    // “不白屏”由上面那条红字负责；空态只能在真的拉到了空列表时才说。
    expect(screen.queryByText("（暂无待审批）")).toBeNull();
  });

  it("畸形审批数据（缺 run_id / reason / arguments）不会崩，仍能渲染", async () => {
    vi.spyOn(api, "approvals").mockResolvedValue([
      { approval_id: "x", tool_name: "no.runid" } as unknown as Approval,
    ]);
    render(<ApprovalPanel />);
    // 未登记的名字**原样显示**（不猜成「未知操作」：猜错比不猜更危险）
    expect(await screen.findByText("no.runid")).toBeInTheDocument();
    // 缺 reason 时不会渲染 undefined
    expect(screen.queryByText(/undefined/)).toBeNull();
  });

  it("决策历史默认收起：只留标题与条数，明细一个字都不渲染", async () => {
    vi.spyOn(api, "approvalHistory").mockResolvedValue([
      { run_id: "r1", approval_id: "a1", tool_name: "shell.run", decision: "approved", created_at: "" },
    ]);
    render(<ApprovalPanel />);
    // 折叠标题出现，说明"有历史"这件事本身没被藏掉
    const toggle = await screen.findByRole("button", { name: /最近决策/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("（1）")).toBeInTheDocument();
    // 明细必须真的不在 DOM 里 —— 不是 display:none 那种"看得见的藏"
    expect(screen.queryByText("✓已批准")).toBeNull();
    expect(screen.queryByText("执行命令")).toBeNull();
  });

  it("点「最近决策」展开明细，再点一次收起；默认中文工具名", async () => {
    vi.spyOn(api, "approvalHistory").mockResolvedValue([
      { run_id: "r1", approval_id: "a1", tool_name: "shell.run", decision: "approved", created_at: "" },
      { run_id: "r2", approval_id: "a2", tool_name: "web.search", decision: "rejected", created_at: "" },
    ]);
    render(<ApprovalPanel />);
    const toggle = await screen.findByRole("button", { name: /最近决策/ });

    fireEvent.click(toggle); // 真实点击，不绕过 onClick
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("✓已批准")).toBeInTheDocument();
    expect(screen.getByText("✗已拒绝")).toBeInTheDocument();
    expect(screen.getByText("执行命令")).toBeInTheDocument();
    expect(screen.getByText("联网搜索")).toBeInTheDocument();

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("✓已批准")).toBeNull();
  });
});
