// App.tsx —— 中控台外壳（此前 0 覆盖：327 行全靠手点验收）。
// 这里只测"外壳自己的职责"：账号登记与切换、会话归属切换、三栏折叠/拖拽。
// 面板内部逻辑由各自组件的测试覆盖（这里全部给空数据，避免耦合）。
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "./App";
import { api } from "./lib/api";

function mockAll() {
  vi.spyOn(api, "createUser").mockResolvedValue({ ok: true, user_id: "u" });
  vi.spyOn(api, "createRun").mockResolvedValue({ run_id: "r", status: "IDLE" });
  vi.spyOn(api, "users").mockResolvedValue([]);
  vi.spyOn(api, "messages").mockResolvedValue([]);
  vi.spyOn(api, "runs").mockResolvedValue([]);
  vi.spyOn(api, "status").mockResolvedValue({ run_id: "r", status: "COMPLETED" });
  vi.spyOn(api, "capabilities").mockResolvedValue({
    tools: ["shell.exec"],
    features: { memory: true, skills: [], web: true, mcp_server: null },
  });
  vi.spyOn(api, "memory").mockResolvedValue([]);
  vi.spyOn(api, "health").mockResolvedValue({ status: "ok", checks: [] });
  vi.spyOn(api, "models").mockResolvedValue({ current: "fake", models: [] });
  vi.spyOn(api, "approvals").mockResolvedValue([]);
  vi.spyOn(api, "approvalHistory").mockResolvedValue([]);
}

/** 等各面板的首轮请求都落地，避免测试结束时还在 setState。 */
async function settle() {
  await screen.findByText("shell.exec"); // 能力面板
  await screen.findByLabelText("健康状态：ok"); // 健康轮询
  await screen.findByText("（暂无待审批）"); // 审批队列
}

beforeEach(() => {
  // 两处都清：密钥现在默认存 sessionStorage，漏清会让上一个用例的 key 泄漏到下一个用例
  // （表现为输入框里已经有值、typing 变成拼接——这个坑刚好就踩过一次）
  localStorage.clear();
  sessionStorage.clear();
  // URL 也要清：会话 ID 会写回查询串，而 jsdom 的 location 在同一文件的用例间是共享的
  // ——不清的话下一个用例会把上一条留下的 `?run=...` 当成“深链”直接选中旧会话。
  window.history.replaceState(null, "", "/");
  mockAll();
});

afterEach(() => vi.restoreAllMocks());

describe("App 外壳：账号与会话", () => {
  it("挂载即登记当前 USER_ID，并把新会话归属到它名下", async () => {
    render(<App />);
    await waitFor(() => expect(api.createUser).toHaveBeenCalledWith("demo-user"));
    // 聊天区按当前 run 拉历史
    await waitFor(() => expect(api.messages).toHaveBeenCalled());
    const runId = vi.mocked(api.messages).mock.calls[0]![0];
    expect(runId.startsWith("run-")).toBe(true);
    // 对话列表按当前账号过滤
    expect(api.runs).toHaveBeenCalledWith("demo-user", { signal: expect.any(AbortSignal) });
  });

  it("切换 USER_ID：登记新账号、落盘 localStorage、聊天区换到全新会话", async () => {
    render(<App />);
    await settle();
    const before = vi.mocked(api.messages).mock.calls[0]![0];

    const input = screen.getByLabelText("USER_ID");
    await userEvent.clear(input);
    await userEvent.type(input, "alice{Enter}");

    await waitFor(() => expect(api.createUser).toHaveBeenCalledWith("alice"));
    expect(localStorage.getItem("warden.userId")).toBe("alice");
    // 换账号必须换会话：否则会在 alice 视角下看到 demo-user 的对话
    await waitFor(() => {
      const runs = vi.mocked(api.messages).mock.calls.map((c) => c[0]);
      expect(runs.some((r) => r !== before)).toBe(true);
    });
    // 对话列表也跟着切到新账号
    await waitFor(() =>
      expect(api.runs).toHaveBeenCalledWith("alice", { signal: expect.any(AbortSignal) }),
    );
  });

  it("上次的 USER_ID 存在 localStorage 时直接恢复（不必重新输入）", async () => {
    localStorage.setItem("warden.userId", "bob");
    render(<App />);
    await waitFor(() => expect(api.createUser).toHaveBeenCalledWith("bob"));
    expect(screen.getByLabelText("USER_ID")).toHaveValue("bob");
  });

  it("USER_ID 下拉列出已创建的账号，点选即切换", async () => {
    vi.spyOn(api, "users").mockResolvedValue([
      { user_id: "alice", created_at: "2024-01-01T00:00:00Z" },
      { user_id: "demo-user", created_at: "2024-01-02T00:00:00Z" },
    ]);
    render(<App />);
    await settle();

    await userEvent.click(screen.getByTitle("切换 USER_ID"));
    // 当前账号在列表里被标为「当前」
    await screen.findByText("当前");
    await userEvent.click(screen.getByText("alice"));

    await waitFor(() => expect(api.createUser).toHaveBeenCalledWith("alice"));
    expect(screen.getByLabelText("USER_ID")).toHaveValue("alice");
  });
});

describe("App 外壳：三栏布局", () => {
  it("顶栏 ☰ 折叠/展开左对话栏（折叠后留一条可点回来的细条）", async () => {
    render(<App />);
    await settle();
    expect(screen.queryByTitle("展开对话栏")).toBeNull(); // 默认展开

    await userEvent.click(screen.getByLabelText("切换对话列表"));
    expect(screen.getByTitle("展开对话栏")).toBeInTheDocument();

    await userEvent.click(screen.getByTitle("展开对话栏"));
    await waitFor(() => expect(screen.queryByTitle("展开对话栏")).toBeNull());
  });

  it("「信息」按钮收起右栏：审批/模型面板一起隐藏，再点回来还在", async () => {
    render(<App />);
    await settle();
    expect(screen.getByText("审批队列")).toBeInTheDocument();

    await userEvent.click(screen.getByLabelText("切换信息栏"));
    await waitFor(() => expect(screen.queryByText("审批队列")).toBeNull());
    expect(screen.queryByText("模型")).toBeNull();

    await userEvent.click(screen.getByLabelText("切换信息栏"));
    await waitFor(() => expect(screen.getByText("审批队列")).toBeInTheDocument());
  });

  it("拖分隔条把左栏挤到最左（<24px）即收起；拖拽期间不选中文本", async () => {
    render(<App />);
    await settle();
    const sep = screen.getAllByTitle("拖拽调整宽度")[0]!;

    fireEvent.mouseDown(sep);
    fireEvent.mouseMove(window, { clientX: 400 });
    fireEvent.mouseMove(window, { clientX: 10 }); // 拖到贴边 → 归零
    fireEvent.mouseUp(window);

    expect(screen.getByTitle("展开对话栏")).toBeInTheDocument();
  });

  it("左栏折叠后内容不留在 DOM（否则是看不到却能 Tab 到的按钮）", async () => {
    render(<App />);
    await settle();
    expect(screen.getByText("＋ 新对话")).toBeInTheDocument();

    await userEvent.click(screen.getByLabelText("切换对话列表"));
    expect(screen.getByTitle("展开对话栏")).toBeInTheDocument();
    expect(screen.queryByText("＋ 新对话")).toBeNull();
  });

  it("「＋ 新对话」打开会话 ID 输入行，输入 ID 回车即建档并切过去", async () => {
    render(<App />);
    await settle();
    await userEvent.click(screen.getByText("＋ 新对话"));
    const box = screen.getByLabelText("新会话 ID");

    await userEvent.type(box, "run-alice-1{Enter}");
    await waitFor(() => expect(api.createRun).toHaveBeenCalledWith("run-alice-1"));
    // 建完就切过去：聊天区按新 run 拉历史，输入行收起
    await waitFor(() =>
      expect(vi.mocked(api.messages).mock.calls.map((c) => c[0])).toContain("run-alice-1"),
    );
    expect(screen.queryByLabelText("新会话 ID")).toBeNull();
  });

  it("外壳有可访问名：账号、密钥、拖拽条都不只靠 placeholder", async () => {
    render(<App />);
    await settle();
    expect(screen.getByLabelText("访问密钥")).toBeInTheDocument();
    expect(screen.getByLabelText("切换对话列表")).toBeInTheDocument();
    expect(screen.getByLabelText("切换信息栏")).toBeInTheDocument();
  });
});

describe("App 外壳：密钥输入", () => {
  it("输入密钥后失焦写入 sessionStorage（当前标签页有效），不落 localStorage", async () => {
    // 默认不落 localStorage 是**安性行为**：页面要渲染模型输出的 Markdown，
    // key 落在 localStorage 就是跨浏览器重启长期待在一个 XSS 能读到的地方。
    render(<App />);
    await settle();
    const key = screen.getByLabelText("访问密钥");
    await userEvent.type(key, "sk-secret");
    fireEvent.blur(key);
    expect(sessionStorage.getItem("warden.apiKey")).toBe("sk-secret");
    expect(localStorage.getItem("warden.apiKey")).toBeNull();
  });

  it("勾上「记住」后改存 localStorage，取消勾选又只留在 sessionStorage", async () => {
    render(<App />);
    await settle();
    const key = screen.getByLabelText("访问密钥");
    await userEvent.type(key, "sk-secret");
    fireEvent.blur(key);

    const remember = screen.getByLabelText("记住密钥");
    await userEvent.click(remember);
    // 勾上要“立刻”生效：不能等下一次输入才搬运
    expect(localStorage.getItem("warden.apiKey")).toBe("sk-secret");
    expect(sessionStorage.getItem("warden.apiKey")).toBeNull();

    await userEvent.click(remember);
    expect(localStorage.getItem("warden.apiKey")).toBeNull();
    expect(sessionStorage.getItem("warden.apiKey")).toBe("sk-secret");
  });
});
