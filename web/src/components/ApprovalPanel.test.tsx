// ApprovalPanel.tsx —— 审批队列面板。
// 断言的是**行为**：点批准/拒绝真的调后端对应接口并刷新队列、失败有兜底提示、
// 后端返回缺字段的畸形审批记录时面板不会白屏。
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ApprovalPanel from "./ApprovalPanel";
import { api } from "../lib/api";
import type { Approval } from "../lib/types";

const pending: Approval[] = [
  {
    approval_id: "ap-1",
    run_id: "run-1",
    tool_name: "shell.exec",
    arguments: { cmd: "rm -rf /tmp/x" },
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
  it("展示待审批的工具名与原因", async () => {
    render(<ApprovalPanel />);
    expect(await screen.findByText("shell.exec")).toBeInTheDocument();
    expect(screen.getByText(/高危命令/)).toBeInTheDocument();
    expect(screen.getByText(/run-1/)).toBeInTheDocument();
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
    await screen.findByText("shell.exec");
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
    await screen.findByText("shell.exec");

    await userEvent.click(screen.getByRole("button", { name: "批准" }));

    await waitFor(() => expect(onDecided).toHaveBeenCalledTimes(1));
  });

  it("决策失败时**不**回调 onDecided（别让上层以为这一轮已经恢复）", async () => {
    vi.spyOn(api, "approve").mockRejectedValue(new Error("没有等待审批的请求"));
    const onDecided = vi.fn();
    render(<ApprovalPanel onDecided={onDecided} />);
    await screen.findByText("shell.exec");

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
    await screen.findByText("shell.exec");
    await userEvent.click(screen.getByRole("button", { name: "拒绝" }));
    expect(reject).toHaveBeenCalledWith("run-1");
  });

  it("批准接口失败时走兜底提示，不抛未捕获异常", async () => {
    vi.spyOn(api, "approve").mockRejectedValue(new Error("409: 没有等待审批的记录"));
    render(<ApprovalPanel />);
    await screen.findByText("shell.exec");
    await userEvent.click(screen.getByRole("button", { name: "批准" }));
    await waitFor(() => expect(window.alert).toHaveBeenCalledWith("409: 没有等待审批的记录"));
  });

  it("轮询失败时在面板内显示错误，而不是整片白屏", async () => {
    vi.spyOn(api, "approvals").mockRejectedValue(new Error("503: 服务不可用"));
    render(<ApprovalPanel />);
    expect(await screen.findByText(/503: 服务不可用/)).toBeInTheDocument();
    // 失败时依然渲染空态骨架
    expect(screen.getByText("（暂无待审批）")).toBeInTheDocument();
  });

  it("畸形审批数据（缺 run_id / reason / arguments）不会崩，仍能渲染", async () => {
    vi.spyOn(api, "approvals").mockResolvedValue([
      { approval_id: "x", tool_name: "no.runid" } as unknown as Approval,
    ]);
    render(<ApprovalPanel />);
    expect(await screen.findByText("no.runid")).toBeInTheDocument();
    // 缺 reason 时不会渲染 undefined
    expect(screen.queryByText(/undefined/)).toBeNull();
  });

  it("决策历史按 approved/rejected 分流显示", async () => {
    vi.spyOn(api, "approvalHistory").mockResolvedValue([
      { run_id: "r1", approval_id: "a1", tool_name: "shell.exec", decision: "approved", created_at: "" },
      { run_id: "r2", approval_id: "a2", tool_name: "web.search", decision: "rejected", created_at: "" },
    ]);
    render(<ApprovalPanel />);
    expect(await screen.findByText("✓已批准")).toBeInTheDocument();
    expect(screen.getByText("✗已拒绝")).toBeInTheDocument();
  });
});
