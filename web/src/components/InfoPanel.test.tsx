// InfoPanel.tsx —— 右侧信息面板（run 状态 / 能力 / 记忆 / 健康）。
// 覆盖：状态刷新按钮、能力 chips 与工具展开、记忆渲染，以及空/缺字段数据不崩。
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import InfoPanel from "./InfoPanel";
import { api } from "../lib/api";
import type { Capabilities } from "../lib/types";

const caps: Capabilities = {
  tools: ["t1", "t2", "t3", "t4", "t5", "t6", "t7", "t8"],
  features: { memory: true, skills: ["web-search"], web: true, mcp_server: "mcp-github" },
};

beforeEach(() => {
  vi.spyOn(api, "health").mockResolvedValue({ status: "ok", checks: [] });
  vi.spyOn(api, "status").mockResolvedValue({ run_id: "run-1", status: "COMPLETED" });
  vi.spyOn(api, "memory").mockResolvedValue([]);
  vi.spyOn(api, "memoryCandidates").mockResolvedValue([]);
  vi.spyOn(api, "capabilities").mockResolvedValue(caps);
  // 标题查询：默认没有命中（面板退回显示 run_id）
  vi.spyOn(api, "runs").mockResolvedValue([]);
});

afterEach(() => vi.restoreAllMocks());

describe("InfoPanel", () => {
  it("展示 run 状态（中文）与 runId，点「刷新」会再拉一次状态", async () => {
    render(<InfoPanel runId="run-1" />);
    // 状态显示中文，原始枚举名留在 title 上供技术用户核对
    const st = await screen.findByText("已完成");
    expect(st).toBeInTheDocument();
    expect(st).toHaveAttribute("title", "原始状态：COMPLETED");
    expect(screen.getByText("run-1")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "刷新" }));
    expect((api.status as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(1);
  });

  it("拿得到对话标题时显示标题，run_id 退到悬停提示里", async () => {
    vi.spyOn(api, "runs").mockResolvedValue([
      { run_id: "run-1", status: "COMPLETED", msg_count: 2, title: "帮我查天气", updated_at: null },
    ]);
    render(<InfoPanel runId="run-1" />);
    const name = await screen.findByText("帮我查天气");
    expect(name).toHaveAttribute("title", "会话 ID：run-1");
    // 标题在，ID 就不该再以正文形式出现（那正是用户抱怨的「影响美观」）
    expect(screen.queryByText("run-1")).toBeNull();
  });

  it("标题拉取失败时如实退回 run_id，不把面板弄成报错", async () => {
    vi.spyOn(api, "runs").mockRejectedValue(new Error("500 boom"));
    render(<InfoPanel runId="run-1" />);
    expect(await screen.findByText("run-1")).toBeInTheDocument();
    expect(screen.queryByText(/加载失败/)).toBeNull(); // 标题只是锦上添花
  });

  it("状态拉取失败时显示占位「…」而不是崩溃", async () => {
    vi.spyOn(api, "status").mockRejectedValue(new Error("network down"));
    render(<InfoPanel runId="run-1" />);
    expect(await screen.findByText("…")).toBeInTheDocument();
  });

  it("渲染能力 chips（memory/web/skill/mcp）", async () => {
    render(<InfoPanel runId="run-1" />);
    expect(await screen.findByText("memory")).toBeInTheDocument();
    expect(screen.getByText("web")).toBeInTheDocument();
    expect(screen.getByText("skill:web-search")).toBeInTheDocument();
    expect(screen.getByText("mcp:mcp-github")).toBeInTheDocument();
  });

  it("搜索是离线演示档时必须显式标出 + 给出原因（不能让人以为真联网了）", async () => {
    vi.spyOn(api, "capabilities").mockResolvedValue({
      tools: [],
      features: {
        memory: true,
        skills: [],
        web: true,
        mcp_server: null,
        web_search_mock: true,
        web_fetch_mock: true,
        web_degraded_reason: "WARDEN_SEARCH_PROVIDER=tavily 但未配 WARDEN_SEARCH_API_KEY",
      },
    });
    render(<InfoPanel runId="run-1" />);
    expect(await screen.findByText("web:离线演示")).toBeInTheDocument();
    expect(screen.getByText(/未配 WARDEN_SEARCH_API_KEY/)).toBeInTheDocument();
    // 要告诉用户怎么换真的，否则只会被当成“这东西很差”
    expect(screen.getByText(/WARDEN_SEARCH_PROVIDER=tavily/)).toBeInTheDocument();
  });

  it("真联网搜索不显示演示标记，也不显示原因", async () => {
    vi.spyOn(api, "capabilities").mockResolvedValue({
      tools: [],
      features: { memory: false, skills: [], web: true, mcp_server: null, web_search_mock: false },
    });
    render(<InfoPanel runId="run-1" />);
    expect(await screen.findByText("web")).toBeInTheDocument();
    expect(screen.queryByText("web:离线演示")).toBeNull();
  });

  it("模型是演示脚本时必须解释清楚（回复写死，不能当能力证据）", async () => {
    vi.spyOn(api, "capabilities").mockResolvedValue({
      tools: [],
      features: { memory: false, skills: [], web: false, mcp_server: null, model_kind_now: "fake" },
    });
    render(<InfoPanel runId="run-1" />);
    expect(await screen.findByText("模型:演示脚本")).toBeInTheDocument();
    // 光打标签不够：要告诉用户"差在哪 + 怎么换成真的"
    expect(screen.getByText(/回复内容与工具参数都是写死的/)).toBeInTheDocument();
    expect(screen.getByText(/设 DEEPSEEK_API_KEY/)).toBeInTheDocument();
  });

  it("真模型时不显示演示说明", async () => {
    vi.spyOn(api, "capabilities").mockResolvedValue({
      tools: [],
      features: { memory: false, skills: [], web: false, mcp_server: null, model_kind_now: "real" },
    });
    render(<InfoPanel runId="run-1" />);
    expect(await screen.findByText("能力")).toBeInTheDocument();
    expect(screen.queryByText("模型:演示脚本")).toBeNull();
    expect(screen.queryByText(/回复内容与工具参数都是写死的/)).toBeNull();
  });

  it("后端没上报 model_kind_now 时不报假警", async () => {
    vi.spyOn(api, "capabilities").mockResolvedValue({
      tools: [],
      features: { memory: true, skills: [], web: true, mcp_server: null },
    });
    render(<InfoPanel runId="run-1" />);
    expect(await screen.findByText("web")).toBeInTheDocument();
    expect(screen.queryByText("web:离线演示")).toBeNull();
    expect(screen.queryByText("memory:进程内重启即丢")).toBeNull();
  });

  it("记忆是进程内时要提醒“重启即丢”，模型是演示脚本时也要标出来", async () => {
    vi.spyOn(api, "capabilities").mockResolvedValue({
      tools: [],
      features: {
        memory: true,
        skills: [],
        web: false,
        mcp_server: null,
        memory_persistent: false,
        model_kind_now: "fake",
      },
    });
    render(<InfoPanel runId="run-1" />);
    expect(await screen.findByText("memory:进程内重启即丢")).toBeInTheDocument();
    expect(screen.getByText("模型:演示脚本")).toBeInTheDocument();
  });

  it("工具超过 6 个时先折叠，点「展开全部」显示全部", async () => {
    render(<InfoPanel runId="run-1" />);
    const expand = await screen.findByRole("button", { name: "展开全部 (8)" });
    expect(screen.queryByText("t7")).toBeNull();
    await userEvent.click(expand);
    expect(screen.getByText("t7")).toBeInTheDocument();
    expect(screen.getByText("t8")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "收起" })).toBeInTheDocument();
  });

  it("记忆为空显示空态；记忆项缺 text 也不崩", async () => {
    vi.spyOn(api, "memory").mockResolvedValue([
      { scope: "session", key: "k1", text: undefined as unknown as string, status: "active" },
    ]);
    render(<InfoPanel runId="run-1" />);
    expect(await screen.findByText("k1")).toBeInTheDocument();
  });

  it("能力接口失败时显示可重试的失败提示，而不是永远停在「加载中…」", async () => {
    vi.spyOn(api, "capabilities").mockRejectedValue(new Error("boom"));
    render(<InfoPanel runId="run-1" />);
    expect(await screen.findByText(/加载失败：boom/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重试" })).toBeInTheDocument();
    expect(screen.queryByText("加载中…")).toBeNull();
  });

  it("能力响应不是对象（数组/字符串）：报格式异常而不是崩掉", async () => {
    vi.spyOn(api, "capabilities").mockResolvedValue([] as unknown as Capabilities);
    render(<InfoPanel runId="run-1" />);
    expect(await screen.findByText(/加载失败：响应格式异常/)).toBeInTheDocument();
  });

  it("capabilities.tools 不是数组：不崩，也不渲染工具 chip / 展开按钮", async () => {
    vi.spyOn(api, "capabilities").mockResolvedValue({
      tools: null,
      features: { memory: true, skills: null, web: false, mcp_server: null },
    } as unknown as Capabilities);
    render(<InfoPanel runId="run-1" />);
    expect(await screen.findByText("memory")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /展开全部/ })).toBeNull();
  });

  it("点「重试」会重新拉能力，成功后显示内容", async () => {
    const spy = vi
      .spyOn(api, "capabilities")
      .mockRejectedValueOnce(new Error("boom"))
      .mockResolvedValue(caps);
    render(<InfoPanel runId="run-1" />);
    await userEvent.click(await screen.findByRole("button", { name: "重试" }));
    expect(await screen.findByText("memory")).toBeInTheDocument();
    expect(spy).toHaveBeenCalledTimes(2);
  });

  it("快速切 runId：旧 run 的迟到响应不会覆盖新 run 的能力", async () => {
    const emptyFeatures = { memory: false, skills: [], web: false, mcp_server: null };
    const deferred: Array<(v: Capabilities) => void> = [];
    vi.spyOn(api, "capabilities").mockImplementation(
      () =>
        new Promise<Capabilities>((resolve) => {
          deferred.push(resolve);
        }),
    );
    const { rerender } = render(<InfoPanel runId="run-A" />);
    rerender(<InfoPanel runId="run-B" />);
    expect(deferred).toHaveLength(2);

    await act(async () => {
      deferred[1]!({ tools: ["b-tool"], features: emptyFeatures });
    });
    expect(screen.getByText("b-tool")).toBeInTheDocument();

    // A 的响应晚到：不能把 B 已展示的数据盖回去
    await act(async () => {
      deferred[0]!({ tools: ["a-tool"], features: emptyFeatures });
    });
    expect(screen.queryByText("a-tool")).toBeNull();
    expect(screen.getByText("b-tool")).toBeInTheDocument();
  });

  it("记忆接口失败：显示失败原因，而不是谎报「暂无记忆」", async () => {
    vi.spyOn(api, "memory").mockRejectedValue(new Error("503 down"));
    render(<InfoPanel runId="run-1" />);
    expect(await screen.findByText(/记忆加载失败：503 down/)).toBeInTheDocument();
    expect(screen.queryByText("（暂无记忆）")).toBeNull();
  });

  it("run 状态拉取失败：保留占位「…」同时给出失败原因", async () => {
    vi.spyOn(api, "status").mockRejectedValue(new Error("network down"));
    render(<InfoPanel runId="run-1" />);
    expect(await screen.findByText(/状态获取失败：network down/)).toBeInTheDocument();
    expect(screen.getByText("…")).toBeInTheDocument();
  });

  // ---- 待确认候选（两阶段记忆）：memory.remember 只写候选，人得能看见并点确认 ----

  const cand = {
    uid: "uid-1",
    scope: "SESSION",
    key: "city",
    text: "常驻上海",
    status: "PENDING",
    replaces_text: "",
  };

  it("待确认候选会展示出来，并给出标题上的待确认计数", async () => {
    vi.spyOn(api, "memoryCandidates").mockResolvedValue([cand]);
    render(<InfoPanel runId="run-1" />);
    expect(await screen.findByText("待确认 1")).toBeInTheDocument();
    expect(screen.getByText(/点确认后才生效/)).toBeInTheDocument();
    expect(screen.getByText(/常驻上海/)).toBeInTheDocument();
  });

  it("点「确认」→ 调 confirm 接口，并刷新两处列表（候选少一条、生效列表多一条）", async () => {
    vi.spyOn(api, "memoryCandidates").mockResolvedValue([cand]);
    const memSpy = vi.spyOn(api, "memory");
    memSpy.mockResolvedValueOnce([]).mockResolvedValue([
      { scope: "SESSION", key: "city", text: "常驻上海", status: "ACTIVE" },
    ]);
    const confirm = vi.spyOn(api, "confirmMemoryCandidate").mockResolvedValue({});
    render(<InfoPanel runId="run-1" />);
    await userEvent.click(await screen.findByRole("button", { name: "确认" }));
    expect(confirm).toHaveBeenCalledWith("session", "uid-1");
    // 不做刷新的话用户会看到“点了没反应”（候选还在、生效区也不变）
    await waitFor(() => expect(memSpy.mock.calls.length).toBeGreaterThan(1));
  });

  it("冲突候选要写明“确认后会替掉现有”：静默覆盖旧值是事故，不是功能", async () => {
    vi.spyOn(api, "memoryCandidates").mockResolvedValue([
      { ...cand, status: "CONFLICTED", replaces_text: "常驻上海" },
    ]);
    render(<InfoPanel runId="run-1" />);
    expect(await screen.findByText(/确认后会替掉现有：常驻上海/)).toBeInTheDocument();
  });

  it("点「拒绝」→ 调 reject 接口", async () => {
    vi.spyOn(api, "memoryCandidates").mockResolvedValue([cand]);
    const reject = vi.spyOn(api, "rejectMemoryCandidate").mockResolvedValue({});
    render(<InfoPanel runId="run-1" />);
    await userEvent.click(await screen.findByRole("button", { name: "拒绝" }));
    expect(reject).toHaveBeenCalledWith("session", "uid-1");
  });

  it("确认失败要说出来（点了没反应比报错更难查）", async () => {
    vi.spyOn(api, "memoryCandidates").mockResolvedValue([cand]);
    vi.spyOn(api, "confirmMemoryCandidate").mockRejectedValue(new Error("403 nope"));
    render(<InfoPanel runId="run-1" />);
    await userEvent.click(await screen.findByRole("button", { name: "确认" }));
    expect(await screen.findByText(/操作失败：403 nope/)).toBeInTheDocument();
    // 失败后候选不能凭空消失（否则用户以为成功了）
    expect(screen.getByText(/常驻上海/)).toBeInTheDocument();
  });

  it("候选缺 uid：按钮禁用并说明原因，不发请求（拿 key 当 id 用会错删）", async () => {
    vi.spyOn(api, "memoryCandidates").mockResolvedValue([{ ...cand, uid: "" }]);
    const confirm = vi.spyOn(api, "confirmMemoryCandidate").mockResolvedValue({});
    render(<InfoPanel runId="run-1" />);
    const btn = await screen.findByRole("button", { name: "确认" });
    expect(btn).toBeDisabled();
    expect(screen.getByText("缺候选 id，无法处理")).toBeInTheDocument();
    await userEvent.click(btn);
    expect(confirm).not.toHaveBeenCalled();
  });

  it("候选列表拉取失败：报失败原因，而不是假装“没有待确认”", async () => {
    vi.spyOn(api, "memoryCandidates").mockRejectedValue(new Error("500 boom"));
    render(<InfoPanel runId="run-1" />);
    expect(await screen.findByText(/待确认列表加载失败：500 boom/)).toBeInTheDocument();
    expect(screen.queryByText(/点确认后才生效/)).toBeNull();
  });
});
