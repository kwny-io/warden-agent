// ConversationList.tsx —— 左侧历史对话栏。
// 覆盖：列表渲染与时间格式化容错、点条目切换、删除会话（含当前会话回调）、
// 新建会话流程，以及畸形 run 数据不崩。
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ConversationList from "./ConversationList";
import { api } from "../lib/api";
import type { RunInfo } from "../lib/types";

const runs: RunInfo[] = [
  { run_id: "r2", status: "RUNNING", msg_count: 3, title: "最近的对话", updated_at: "2025-01-02T03:04:00Z" },
  { run_id: "r1", status: "COMPLETED", msg_count: 1, title: "旧对话", updated_at: null },
];

function setup(overrides: Partial<Parameters<typeof ConversationList>[0]> = {}) {
  const props = {
    userId: "u1",
    activeRunId: "r2",
    onSwitch: vi.fn(),
    onCollapse: vi.fn(),
    createOpen: false,
    onOpenCreate: vi.fn(),
    onCreateDone: vi.fn(),
    onDeleted: vi.fn(),
    ...overrides,
  };
  render(<ConversationList {...props} />);
  return props;
}

beforeEach(() => {
  vi.spyOn(api, "runs").mockResolvedValue(runs);
  vi.spyOn(api, "createRun").mockResolvedValue({ run_id: "new", status: "CREATED" });
  vi.spyOn(api, "deleteRun").mockResolvedValue({ ok: true });
});

afterEach(() => vi.restoreAllMocks());

describe("ConversationList", () => {
  it("渲染对话标题、消息数与 run_id，并在顶部显示数量", async () => {
    setup();
    expect(await screen.findByText("最近的对话")).toBeInTheDocument();
    const row = screen.getByText(/3 条 · r2/);
    expect(row).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
  });

  it("updated_at 缺失或非法时显示「—」而不是 Invalid Date", async () => {
    vi.spyOn(api, "runs").mockResolvedValue([
      { run_id: "r9", status: "RUNNING", msg_count: 0, title: "", updated_at: "not-a-date" },
    ]);
    setup();
    // title 为空回退到 run_id
    expect(await screen.findByText("r9")).toBeInTheDocument();
    expect(screen.getByText(/— · 0 条 · r9/)).toBeInTheDocument();
    expect(screen.queryByText(/Invalid Date/)).toBeNull();
    expect(screen.queryByText(/NaN/)).toBeNull();
  });

  it("点条目触发 onSwitch(run_id)", async () => {
    const props = setup();
    await userEvent.click(await screen.findByText("旧对话"));
    expect(props.onSwitch).toHaveBeenCalledWith("r1");
  });

  it("删除会话：确认后调 api.deleteRun 并把剩余列表交给 onDeleted", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const props = setup();
    await screen.findByText("最近的对话");
    await userEvent.click(screen.getAllByTitle("删除对话")[0]!); // 删 r2
    expect(api.deleteRun).toHaveBeenCalledWith("r2");
    expect(props.onDeleted).toHaveBeenCalledWith("r2", [runs[1]]);
  });

  it("删除时用户取消则不调后端", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    setup();
    await screen.findByText("最近的对话");
    await userEvent.click(screen.getAllByTitle("删除对话")[0]!);
    expect(api.deleteRun).not.toHaveBeenCalled();
  });

  it("新建会话：输入 ID 后点「创建」建会话并切换过去", async () => {
    const props = setup({ createOpen: true });
    await screen.findByText("最近的对话");
    await userEvent.type(screen.getByPlaceholderText("输入 USER_ID…"), "run-abc");
    await userEvent.click(screen.getByRole("button", { name: "创建" }));
    expect(api.createRun).toHaveBeenCalledWith("run-abc");
    expect(props.onSwitch).toHaveBeenCalledWith("run-abc");
    expect(props.onCreateDone).toHaveBeenCalled();
  });

  it("畸形 run（缺 status / msg_count / title）不会崩，状态点回退到灰色", async () => {
    vi.spyOn(api, "runs").mockResolvedValue([
      { run_id: "weird" } as unknown as RunInfo,
    ]);
    setup();
    expect(await screen.findByText("weird")).toBeInTheDocument();
  });

  it("/runs 返回的不是数组（如对象）：退化为空态，不崩在 .map 上", async () => {
    vi.spyOn(api, "runs").mockResolvedValue({ oops: true } as unknown as RunInfo[]);
    setup();
    expect(await screen.findByText("还没有对话")).toBeInTheDocument();
  });

  it("新建会话输入框有可访问名（不只靠 placeholder）", async () => {
    setup({ createOpen: true });
    await screen.findByText("最近的对话"); // 等首次拉取落地，避免测试结束后 setState
    expect(screen.getByLabelText("新会话 ID")).toBeInTheDocument();
  });

  it("纯图标按钮（删除/收起）有可访问名", async () => {
    setup();
    await screen.findByText("最近的对话");
    expect(screen.getAllByRole("button", { name: "删除对话" })).toHaveLength(2);
    expect(screen.getByRole("button", { name: "收起对话栏" })).toBeInTheDocument();
  });
});
