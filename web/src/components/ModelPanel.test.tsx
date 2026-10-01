// ModelPanel.tsx —— 模型切换面板。
// 覆盖交互分支：当前模型不可点切换、需要密钥且未配置时展开内联 Key 输入、
// 切换失败展示错误、以及畸形 models 数据不崩。
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ModelPanel from "./ModelPanel";
import { api } from "../lib/api";
import type { ModelsInfo } from "../lib/types";

const info: ModelsInfo = {
  current: "gpt-4o",
  models: [
    { id: "gpt-4o", name: "GPT-4o", needs_key: false, configured: true },
    { id: "claude", name: "Claude", needs_key: true, configured: false },
  ],
};

beforeEach(() => {
  vi.spyOn(api, "models").mockResolvedValue(info);
  vi.spyOn(api, "selectModel").mockResolvedValue({ ok: true, current: "x" });
});

afterEach(() => vi.restoreAllMocks());

describe("ModelPanel", () => {
  it("列出所有模型，当前模型标记「使用中」且不给切换按钮", async () => {
    render(<ModelPanel />);
    expect(await screen.findByText("GPT-4o")).toBeInTheDocument();
    expect(screen.getByText("使用中")).toBeInTheDocument();
    // 只有一个非当前模型 → 只有一个「切换」按钮
    expect(screen.getAllByRole("button", { name: "切换" })).toHaveLength(1);
  });

  it("点已配置模型的「切换」直接调 api.selectModel(id)", async () => {
    vi.spyOn(api, "models").mockResolvedValue({
      current: "a",
      models: [
        { id: "a", name: "A", needs_key: false, configured: true },
        { id: "b", name: "B", needs_key: false, configured: true },
      ],
    });
    render(<ModelPanel />);
    await screen.findByText("B");
    await userEvent.click(screen.getByRole("button", { name: "切换" }));
    expect(api.selectModel).toHaveBeenCalledWith("b", undefined);
  });

  it("需要密钥且未配置：先展开 Key 输入，不直接切换", async () => {
    render(<ModelPanel />);
    await screen.findByText("Claude");
    await userEvent.click(screen.getByRole("button", { name: "切换" }));
    expect(api.selectModel).not.toHaveBeenCalled();
    expect(screen.getByPlaceholderText("粘贴 API Key…")).toBeInTheDocument();
    expect(screen.getByText("未导入")).toBeInTheDocument();
  });

  it("在 Key 输入里填值后点「导入并切换」把 key 传给后端", async () => {
    render(<ModelPanel />);
    await screen.findByText("Claude");
    await userEvent.click(screen.getByRole("button", { name: "切换" }));
    await userEvent.type(screen.getByPlaceholderText("粘贴 API Key…"), "sk-test");
    await userEvent.click(screen.getByRole("button", { name: "导入并切换" }));
    expect(api.selectModel).toHaveBeenCalledWith("claude", "sk-test");
  });

  it("切换失败展示后端错误信息", async () => {
    vi.spyOn(api, "models").mockResolvedValue({
      current: "a",
      models: [
        { id: "a", name: "A", needs_key: false, configured: true },
        { id: "b", name: "B", needs_key: false, configured: true },
      ],
    });
    vi.spyOn(api, "selectModel").mockRejectedValue(new Error("400: 未知模型"));
    render(<ModelPanel />);
    await screen.findByText("B");
    await userEvent.click(screen.getByRole("button", { name: "切换" }));
    expect(await screen.findByText("400: 未知模型")).toBeInTheDocument();
  });

  it("models 字段缺失时降级为空列表，不崩", async () => {
    vi.spyOn(api, "models").mockResolvedValue({ current: "" } as unknown as ModelsInfo);
    render(<ModelPanel />);
    expect(await screen.findByText("加载中…")).toBeInTheDocument();
  });

  it("models 不是数组（后端返回字符串）时同样降级，不崩在 .map 上", async () => {
    vi.spyOn(api, "models").mockResolvedValue({
      current: "a",
      models: "oops",
    } as unknown as ModelsInfo);
    render(<ModelPanel />);
    expect(await screen.findByText("加载中…")).toBeInTheDocument();
  });

  it("Key 输入框有可访问名（含模型名，不只靠 placeholder）", async () => {
    render(<ModelPanel />);
    await screen.findByText("Claude");
    await userEvent.click(screen.getByRole("button", { name: "切换" }));
    expect(screen.getByLabelText("Claude 的 API Key")).toBeInTheDocument();
  });
});
