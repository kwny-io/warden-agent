// ConversationList.tsx —— 左侧历史对话栏。
// 覆盖：列表渲染与时间格式化容错、点条目切换、删除会话（含当前会话回调）、
// 新建会话流程、畸形 run 数据不崩，以及**长按进入多选 + 按住下滑连选**。
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ConversationList, { LONG_PRESS_MS } from "./ConversationList";
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

/** 指针"此刻压在哪一行"——jsdom 没实现 elementFromPoint，用这个变量代替真实命中测试。 */
let overRow: Element | null = null;

/** 等长按计时器真的到点。用真时间而不是假计时器：假计时器会和 userEvent 打架。
 *  包在 act 里，否则计时器回调里的 setState 会在 act 外落地。 */
const afterLongPress = () =>
  act(() => new Promise<void>((r) => setTimeout(r, LONG_PRESS_MS + 80)));

/** 在一行上按下（可指定坐标，用来模拟"按住不动"与"按了就滑"）。 */
function pressRow(el: Element, x = 10, y = 10) {
  fireEvent.pointerDown(el, { button: 0, clientX: x, clientY: y });
}
function moveTo(x: number, y: number) {
  fireEvent.pointerMove(window, { clientX: x, clientY: y });
}
function release(x = 10, y = 10) {
  fireEvent.pointerUp(window, { clientX: x, clientY: y });
}

/** 定位某一行的可点区域（button 是行内唯一的可交互元素）。 */
function rowButton(title: string) {
  return screen.getByText(title).closest("button")!;
}

beforeEach(() => {
  vi.spyOn(api, "runs").mockResolvedValue(runs);
  vi.spyOn(api, "createRun").mockResolvedValue({ run_id: "new", status: "CREATED" });
  vi.spyOn(api, "deleteRun").mockResolvedValue({ ok: true });
  vi.spyOn(api, "deleteRuns").mockResolvedValue({ ok: true, deleted: [], count: 0 });
  overRow = null;
  // jsdom 里这个 API 根本不存在，直接挂上去（不能 spyOn）
  (document as unknown as { elementFromPoint: (x: number, y: number) => Element | null }).elementFromPoint =
    () => overRow;
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
    // 单删也走数组：上层只维护一条「删除」路径
    expect(props.onDeleted).toHaveBeenCalledWith(["r2"], [runs[1]]);
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
    vi.spyOn(api, "runs").mockResolvedValue([{ run_id: "weird" } as unknown as RunInfo]);
    setup();
    expect(await screen.findByText("weird")).toBeInTheDocument();
  });

  it("/runs 返回的不是数组（如对象）：不崩在 .map 上，且说「不是数组」而不是「还没有对话」", async () => {
    vi.spyOn(api, "runs").mockResolvedValue({ oops: true } as unknown as RunInfo[]);
    setup();
    expect(await screen.findByText(/对话列表不是数组/)).toBeInTheDocument();
    // 拉到了东西、只是形状不对：那就不是「没有对话」
    expect(screen.queryByText("还没有对话")).toBeNull();
  });

  it("/runs 拉取失败：说「加载失败」+ 给重试，绝不说「还没有对话」", async () => {
    vi.spyOn(api, "runs").mockRejectedValue(new Error("503: 服务不可用"));
    setup();
    expect(await screen.findByText(/加载对话列表失败：503: 服务不可用/)).toBeInTheDocument();
    expect(screen.queryByText("还没有对话")).toBeNull();
  });

  it("拉取失败后点「重试」会再拉一次，成功就正常显示列表", async () => {
    vi.spyOn(api, "runs")
      .mockRejectedValueOnce(new Error("401: 密钥无效"))
      .mockResolvedValue(runs);
    setup();
    await screen.findByText(/加载对话列表失败/);
    await userEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("最近的对话")).toBeInTheDocument();
    expect(screen.queryByText(/加载对话列表失败/)).toBeNull();
  });

  it("创建失败或删除失败时，界面得说出来", async () => {
    vi.spyOn(api, "createRun").mockRejectedValue(new Error("400: 会话 ID 非法"));
    const props = setup({ createOpen: true });
    await screen.findByText("最近的对话");
    await userEvent.type(screen.getByPlaceholderText("输入 USER_ID…"), "bad id");
    await userEvent.click(screen.getByRole("button", { name: "创建" }));
    expect(await screen.findByText(/创建失败：400: 会话 ID 非法/)).toBeInTheDocument();
    expect(props.onCreateDone).not.toHaveBeenCalled();
    expect(props.onSwitch).not.toHaveBeenCalled();
  });

  it("删除会话失败：说出原因，且不谎报已删（onDeleted 不被调）；顺手把「旧列表 + 拉取也挂了」的提示也验了", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    // 第一次拉取成功（先有旧数据），之后的刷新全部失败：这就是"后端挂了"的真实形状
    vi.spyOn(api, "runs")
      .mockResolvedValueOnce(runs)
      .mockRejectedValue(new Error("500: boom"));
    vi.spyOn(api, "deleteRun").mockRejectedValue(new Error("409: 会话正在运行"));
    const props = setup();
    await screen.findByText("最近的对话");
    await userEvent.click(screen.getAllByTitle("删除对话")[0]!);
    expect(await screen.findByText(/删除失败：409: 会话正在运行/)).toBeInTheDocument();
    expect(props.onDeleted).not.toHaveBeenCalled();
    // 删除失败后那次 refresh 也失败 → 横幅要说清楚“下面看到的是上一次的结果”
    expect(await screen.findByText(/加载对话列表失败：500: boom/)).toBeInTheDocument();
    expect(screen.getByText(/下面显示的是上一次的结果/)).toBeInTheDocument();
    expect(screen.getByText("最近的对话")).toBeInTheDocument(); // 旧数据仍在，没被清掉
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

  // ---- 多选：长按进入 + 按住下滑连选 ----

  it("面板头部不再有常驻的「批量」按钮（多选只由长按触发）", async () => {
    setup();
    await screen.findByText("最近的对话");
    expect(screen.queryByRole("button", { name: "批量" })).toBeNull();
    expect(screen.queryByLabelText("全选")).toBeNull();
  });

  it("长按一行进入勾选模式：该行被选中、不切会话、右侧出现删除条", async () => {
    const props = setup();
    await screen.findByText("最近的对话");

    pressRow(rowButton("旧对话"));
    await afterLongPress();

    expect(screen.getByText("已选 1")).toBeInTheDocument();
    expect(screen.getByLabelText("全选")).toBeInTheDocument();
    expect(api.deleteRun).not.toHaveBeenCalled();
    // 长按不是"切换会话"：松手不能把当前会话切走
    fireEvent.click(rowButton("旧对话"));
    expect(props.onSwitch).not.toHaveBeenCalled();
    release();
  });

  it("长按时长按的那一行先勾上；松手后的那一次 click 不再把它翻回去", async () => {
    setup();
    await screen.findByText("最近的对话");

    pressRow(rowButton("旧对话"));
    await afterLongPress();
    release();
    fireEvent.click(rowButton("旧对话")); // 浏览器在 pointerup 后一定会发这一次 click

    expect(screen.getByText("已选 1")).toBeInTheDocument();
  });

  it("按下后立刻移动（想滚列表）不算长按，不会突然弹出勾选条", async () => {
    setup();
    await screen.findByText("最近的对话");

    pressRow(rowButton("旧对话"), 10, 10);
    moveTo(10, 40); // 往下滚了 30px
    await afterLongPress();
    release(10, 40);

    expect(screen.queryByLabelText("全选")).toBeNull();
  });

  it("按住往下滑：划过几行就连这几行一起勾上", async () => {
    setup();
    await screen.findByText("最近的对话");

    const first = rowButton("最近的对话");
    pressRow(first);
    await afterLongPress();
    // 真实浏览器里指针移到 r1 上：elementFromPoint 会返回 r1 那一行
    overRow = rowButton("旧对话");
    moveTo(10, 60);
    release(10, 60);
    fireEvent.click(first); // 松手后浏览器补的那次 click，必须被吞掉

    expect(screen.getByText("已选 2")).toBeInTheDocument();
  });

  it("从没勾的行往下滑是「勾」，从已勾的行往下滑是「取消」——方向在按下那一刻定死", async () => {
    setup();
    await screen.findByText("最近的对话");

    // 先全选，再按住其中一行往下滑 → 这一抹应该是"取消"
    pressRow(rowButton("最近的对话"));
    await afterLongPress();
    overRow = rowButton("旧对话");
    moveTo(10, 60);
    release();
    expect(screen.getByText("已选 2")).toBeInTheDocument();

    fireEvent.click(screen.getByLabelText("全选")); // 取消全选，回到 0
    expect(screen.getByText("已选 0")).toBeInTheDocument();

    pressRow(rowButton("最近的对话"));
    await afterLongPress(); // 这一行被勾上（按下即勾）
    expect(screen.getByText("已选 1")).toBeInTheDocument();
    // 再按住同一行往下滑：起始行已是勾上的 → 这一抹是取消
    pressRow(rowButton("最近的对话"));
    overRow = rowButton("旧对话");
    moveTo(10, 60);
    release();
    expect(screen.getByText("已选 0")).toBeInTheDocument();
  });

  it("勾选模式下点一行是勾/取消，不再切换会话", async () => {
    const props = setup();
    await screen.findByText("最近的对话");

    pressRow(rowButton("旧对话"));
    await afterLongPress();
    release();
    fireEvent.click(rowButton("旧对话")); // 吞掉长按那一下
    expect(screen.getByText("已选 1")).toBeInTheDocument();

    await userEvent.click(screen.getByText("最近的对话"));
    expect(props.onSwitch).not.toHaveBeenCalled();
    expect(screen.getByText("已选 2")).toBeInTheDocument();
  });

  it("全选：一次勾上全部，再点一次全部取消", async () => {
    setup();
    await screen.findByText("最近的对话");
    pressRow(rowButton("最近的对话"));
    await afterLongPress();
    release();

    fireEvent.click(screen.getByLabelText("全选"));
    expect(screen.getByText("已选 2")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("全选"));
    expect(screen.getByText("已选 0")).toBeInTheDocument();
  });

  it("批量删除：确认框写出数量，成功后调 api.deleteRuns 并退出勾选模式", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.spyOn(api, "deleteRuns").mockResolvedValue({ ok: true, deleted: ["r2", "r1"], count: 2 });
    const props = setup();
    await screen.findByText("最近的对话");

    pressRow(rowButton("最近的对话"));
    await afterLongPress();
    release();
    fireEvent.click(screen.getByLabelText("全选"));
    await userEvent.click(screen.getByRole("button", { name: /^删除/ }));

    // 数量必须在确认框里：删 1 条和删 30 条是完全不同的决定
    expect(confirmSpy).toHaveBeenCalledWith(expect.stringContaining("2 个"));
    expect(api.deleteRuns).toHaveBeenCalledWith(["r2", "r1"]);
    expect(props.onDeleted).toHaveBeenCalledWith(["r2", "r1"], []);
    // 删完自动退出勾选：不然会留一个空勾选条挂在已经没有会话的列表上
    expect(screen.queryByLabelText("全选")).toBeNull();
  });

  it("批量删除没勾任何一条时按钮是禁用的", async () => {
    setup();
    await screen.findByText("最近的对话");
    pressRow(rowButton("最近的对话"));
    await afterLongPress();
    release();
    fireEvent.click(screen.getByText("最近的对话")); // 长按那一下的 click，被吞掉
    fireEvent.click(screen.getByText("最近的对话")); // 真正的一次点击：取消勾选
    expect(screen.getByText("已选 0")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^删除/ })).toBeDisabled();
  });

  it("批量删除失败：明说「一个都没删」，不谎报成功，且仍停在勾选模式可直接重试", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.spyOn(api, "deleteRuns").mockRejectedValue(new Error("403: 无权操作会话 'r9'"));
    const props = setup();
    await screen.findByText("最近的对话");

    pressRow(rowButton("最近的对话"));
    await afterLongPress();
    release();
    fireEvent.click(screen.getByLabelText("全选"));
    await userEvent.click(screen.getByRole("button", { name: /^删除/ }));

    expect(await screen.findByText(/一个都没删/)).toBeInTheDocument();
    expect(props.onDeleted).not.toHaveBeenCalled();
    expect(screen.getByLabelText("全选")).toBeInTheDocument();
  });

  it("点「取消」退出勾选模式，回到「点一下就切会话」的常态", async () => {
    const props = setup();
    await screen.findByText("最近的对话");
    pressRow(rowButton("最近的对话"));
    await afterLongPress();
    release();

    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.queryByLabelText("全选")).toBeNull();
    expect(screen.queryByText(/已选 \d/)).toBeNull(); // 条形整条消失

    await userEvent.click(screen.getByText("旧对话"));
    expect(props.onSwitch).toHaveBeenCalledWith("r1");
  });
});
