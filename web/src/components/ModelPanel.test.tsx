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
  vi.spyOn(api, "probeModel").mockResolvedValue({
    id: "gpt-4o",
    ok: true,
    reason: null,
    latency_ms: 120,
  });
});

afterEach(() => vi.restoreAllMocks());

describe("ModelPanel", () => {
  it("/models 拉取失败：说「加载失败」+ 重试，绝不一直显示「加载中…」（转圈也是假话）", async () => {
    vi.spyOn(api, "models").mockRejectedValue(new Error("503: 网关不可用"));
    render(<ModelPanel />);
    expect(await screen.findByText(/加载模型列表失败：503: 网关不可用/)).toBeInTheDocument();
    expect(screen.queryByText("加载中…")).toBeNull();
    expect(screen.getByRole("button", { name: "重试" })).toBeInTheDocument();
  });

  it("/models 拉取失败后点「重试」能恢复", async () => {
    vi.spyOn(api, "models")
      .mockRejectedValueOnce(new Error("500: boom"))
      .mockResolvedValue(info);
    render(<ModelPanel />);
    await screen.findByText(/加载模型列表失败/);
    await userEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("GPT-4o")).toBeInTheDocument();
    expect(screen.queryByText(/加载模型列表失败/)).toBeNull();
  });

  it("有 Key 但探不通：把原因写在卡片里，不把「configured」当成「可用」", async () => {
    vi.spyOn(api, "models").mockResolvedValue({
      current: "a",
      models: [
        {
          id: "b",
          name: "B",
          needs_key: true,
          configured: true,
          last_probe: { id: "b", ok: false, reason: "APIConnectionError: 连不上" },
        },
      ],
    });
    render(<ModelPanel />);
    expect(await screen.findByText("探不通")).toBeInTheDocument();
    // 没有 hint 时退回原始原因（老服务端也能显示，只是不可操作）
    expect(screen.getByText(/有凭据，但探不通：APIConnectionError/)).toBeInTheDocument();
  });

  it("探不通要说清「该做什么」：先给 hint，再把原始原因附在下面", async () => {
    vi.spyOn(api, "models").mockResolvedValue({
      current: "a",
      models: [
        {
          id: "b",
          name: "B",
          needs_key: true,
          configured: true,
          last_probe: {
            id: "b",
            ok: false,
            code: "quota_exhausted",
            hint: "账户余额不足或用额度用完（上游拒绕服务）——去上游充值",
            reason: "AuthenticationError: Error code: 402 - 余额不足（当前 ¥-0.01）",
          },
        },
      ],
    });
    render(<ModelPanel />);
    // 动作句在前：用户一眼知道是「去充值」，不是「换 Key」
    expect(await screen.findByText(/去上游充值/)).toBeInTheDocument();
    // 原始原因仍然可见（排障需要），但不能是唯一信息
    expect(screen.getByText(/Error code: 402/)).toBeInTheDocument();
  });

  it("点「测一下」会真的去探，并把实测结论显示出来", async () => {
    vi.spyOn(api, "models").mockResolvedValue({
      current: "a",
      models: [{ id: "b", name: "B", needs_key: true, configured: true }],
    });
    const probe = vi.spyOn(api, "probeModel").mockResolvedValue({
      id: "b",
      ok: true,
      reason: null,
      latency_ms: 88,
    });
    render(<ModelPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "测一下" }));
    expect(probe).toHaveBeenCalledWith("b", true, undefined, undefined, undefined);
    expect(await screen.findByText("实测连通")).toBeInTheDocument();
  });

  it("切换成功后自动探一次（不让用户等到发消息才知道 provider 挂了）", async () => {
    vi.spyOn(api, "models").mockResolvedValue({
      current: "a",
      models: [
        { id: "a", name: "A", needs_key: false, configured: true },
        { id: "b", name: "B", needs_key: false, configured: true },
      ],
    });
    const probe = vi.spyOn(api, "probeModel").mockResolvedValue({
      id: "b",
      ok: true,
      reason: null,
    });
    render(<ModelPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "切换" }));
    await vi.waitFor(() =>
      expect(probe).toHaveBeenCalledWith("b", true, undefined, undefined, undefined),
    );
  });

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
    expect(api.selectModel).toHaveBeenCalledWith("b", undefined, undefined, undefined);
  });

  it("需要密钥且未配置：先展开 Key 输入，不直接切换", async () => {
    render(<ModelPanel />);
    await screen.findByText("Claude");
    await userEvent.click(screen.getByRole("button", { name: "切换" }));
    expect(api.selectModel).not.toHaveBeenCalled();
    expect(screen.getByPlaceholderText(/粘贴访问密钥/)).toBeInTheDocument();
    expect(screen.getByText("未配置")).toBeInTheDocument();
  });

  it("在 Key 输入里填值后点「导入并切换」把 key 传给后端", async () => {
    render(<ModelPanel />);
    await screen.findByText("Claude");
    await userEvent.click(screen.getByRole("button", { name: "切换" }));
    await userEvent.type(screen.getByPlaceholderText(/粘贴访问密钥/), "sk-test");
    await userEvent.click(screen.getByRole("button", { name: "导入并切换" }));
    expect(api.selectModel).toHaveBeenCalledWith("claude", "sk-test", undefined, undefined);
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

describe("ModelPanel 的自建 / 私有部署接入", () => {
  const customInfo: ModelsInfo = {
    current: "fake",
    models: [
      { id: "fake", name: "离线假模型", needs_key: false, configured: true },
      {
        id: "custom",
        name: "自建 / 私有部署（OpenAI 兼容）",
        needs_key: true,
        needs_endpoint: true,
        configured: false,
        base_url: null,
        model: null,
      },
    ],
  };

  it("点「切换」展开地址 + 模型名两个输入框，而不是先给你一个 400", async () => {
    vi.spyOn(api, "models").mockResolvedValue(customInfo);
    render(<ModelPanel />);
    await screen.findByText("自建 / 私有部署（OpenAI 兼容）");
    await userEvent.click(screen.getByRole("button", { name: "切换" }));
    // 自建接入即使还没 key，展开的也必须是端点输入——那是这一项存在的理由
    expect(screen.getByLabelText("自建 / 私有部署（OpenAI 兼容） 的端点地址")).toBeInTheDocument();
    expect(screen.getByLabelText("自建 / 私有部署（OpenAI 兼容） 的模型名")).toBeInTheDocument();
    // 地址没填之前不许提交（点不动比点了再报错好）
    expect(screen.getByRole("button", { name: "导入并切换" })).toBeDisabled();
  });

  it("填完地址与 key 后提交，三样一起发给后端", async () => {
    vi.spyOn(api, "models").mockResolvedValue(customInfo);
    render(<ModelPanel />);
    await screen.findByText("自建 / 私有部署（OpenAI 兼容）");
    await userEvent.click(screen.getByRole("button", { name: "切换" }));
    await userEvent.type(
      screen.getByLabelText("自建 / 私有部署（OpenAI 兼容） 的端点地址"),
      "http://10.0.0.7:8000/v1",
    );
    await userEvent.type(
      screen.getByLabelText("自建 / 私有部署（OpenAI 兼容） 的模型名"),
      "qwen2.5-7b-instruct",
    );
    await userEvent.type(
      screen.getByLabelText("自建 / 私有部署（OpenAI 兼容） 的 API Key"),
      "not-needed",
    );
    await userEvent.click(screen.getByRole("button", { name: "导入并切换" }));
    expect(api.selectModel).toHaveBeenCalledWith(
      "custom",
      "not-needed",
      undefined,
      { base_url: "http://10.0.0.7:8000/v1", model: "qwen2.5-7b-instruct" },
    );
  });

  it("已配置过：地址回填进输入框，key 留空即可保存（不重填就沿用库里那把）", async () => {
    vi.spyOn(api, "models").mockResolvedValue({
      current: "fake",
      models: [
        { id: "fake", name: "离线假模型", needs_key: false, configured: true },
        {
          id: "custom",
          name: "自建 / 私有部署（OpenAI 兼容）",
          needs_key: true,
          needs_endpoint: true,
          configured: true,
          base_url: "http://10.0.0.7:8000/v1",
          model: "qwen2.5-7b-instruct",
        },
      ],
    });
    render(<ModelPanel />);
    await screen.findByText("自建 / 私有部署（OpenAI 兼容）");
    await userEvent.click(screen.getByRole("button", { name: "切换" }));
    expect(screen.getByLabelText("自建 / 私有部署（OpenAI 兼容） 的端点地址")).toHaveValue(
      "http://10.0.0.7:8000/v1",
    );
    // 已有配置 ⇒ 地址和 key 都不用动，直接可保存
    const save = screen.getByRole("button", { name: "保存并切换" });
    expect(save).toBeEnabled();
    await userEvent.click(save);
    expect(api.selectModel).toHaveBeenCalledWith("custom", undefined, undefined, {
      base_url: "http://10.0.0.7:8000/v1",
      model: "qwen2.5-7b-instruct",
    });
  });

  it("还没保存就探通了，也要当场显示「实测连通」——那是「我这地址填对了吗」的答案", async () => {
    vi.spyOn(api, "models").mockResolvedValue(customInfo);
    vi.spyOn(api, "probeModel").mockResolvedValue({
      id: "custom",
      ok: true,
      reason: null,
      latency_ms: 42,
    });
    render(<ModelPanel />);
    await screen.findByText("自建 / 私有部署（OpenAI 兼容）");
    await userEvent.click(screen.getByRole("button", { name: "切换" }));
    await userEvent.type(
      screen.getByLabelText("自建 / 私有部署（OpenAI 兼容） 的端点地址"),
      "http://127.0.0.1:11434/v1",
    );
    await userEvent.type(
      screen.getByLabelText("自建 / 私有部署（OpenAI 兼容） 的 API Key"),
      "not-needed",
    );
    await userEvent.click(screen.getByRole("button", { name: /用当前填写的地址测一下/ }));
    // 按 configured 挡住成功徽标，等于"探了但不告诉你结果"
    expect(await screen.findByText("实测连通")).toBeInTheDocument();
  });

  it("「测一下」带着**还没保存**的地址与密钥去探（先探通再保存）", async () => {
    vi.spyOn(api, "models").mockResolvedValue(customInfo);
    const probe = vi.spyOn(api, "probeModel").mockResolvedValue({
      id: "custom",
      ok: false,
      reason: "APIConnectionError: 连不上",
    });
    render(<ModelPanel />);
    await screen.findByText("自建 / 私有部署（OpenAI 兼容）");
    await userEvent.click(screen.getByRole("button", { name: "切换" }));
    await userEvent.type(
      screen.getByLabelText("自建 / 私有部署（OpenAI 兼容） 的端点地址"),
      "http://127.0.0.1:11434/v1",
    );
    // 密钥也得带上：没保存过的模型一把已登记的 key 都没有，
    // 不带 key 时后端只会回「需要先导入」，探测这条路等于走不通。
    await userEvent.type(
      screen.getByLabelText("自建 / 私有部署（OpenAI 兼容） 的 API Key"),
      "not-needed",
    );
    // 编辑器里那个「测一下」：按可访问名取，别撞上别的卡片标题栏里那个
    await userEvent.click(
      screen.getByRole("button", { name: /用当前填写的地址测一下/ }),
    );
    expect(probe).toHaveBeenCalledWith(
      "custom",
      true,
      undefined,
      { base_url: "http://127.0.0.1:11434/v1", model: null },
      "not-needed",
    );
    // 探不通的原因要当场显示（不然用户不知道是该改地址还是该看服务）
    expect(await screen.findByText(/APIConnectionError/)).toBeInTheDocument();
  });
});
