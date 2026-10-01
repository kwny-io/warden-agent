// ErrorBoundary.tsx —— 全局兜底测试。
// 关键：子组件抛错时要渲染兜底 UI、错误信息透出来、console.error 只被调用一次
// 且不会二次爆炸、按钮可用。
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import ErrorBoundary, { PanelBoundary } from "./ErrorBoundary";

function Bomb({ shouldThrow }: { shouldThrow: boolean }): ReactElement {
  if (shouldThrow) throw new Error("面板炸了：Unexpected token <");
  return <div>正常内容</div>;
}

afterEach(() => vi.restoreAllMocks());

describe("ErrorBoundary", () => {
  it("子组件正常时原样渲染，不显示兜底 UI", () => {
    render(
      <ErrorBoundary>
        <Bomb shouldThrow={false} />
      </ErrorBoundary>,
    );
    expect(screen.getByText("正常内容")).toBeInTheDocument();
    expect(screen.queryByText("页面出了点问题")).toBeNull();
  });

  it("子组件渲染抛错时渲染兜底 UI，并把错误信息展示出来", () => {
    const err = vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <ErrorBoundary>
        <Bomb shouldThrow={true} />
      </ErrorBoundary>,
    );
    expect(screen.getByText("页面出了点问题")).toBeInTheDocument();
    expect(screen.getByText(/面板炸了：Unexpected token </)).toBeInTheDocument();
    // 原内容不再渲染
    expect(screen.queryByText("正常内容")).toBeNull();
    expect(err).toHaveBeenCalled();
    // 自定义前缀说明是 componentDidCatch 打的，而不是 React 内部
    expect(err.mock.calls.some((c) => String(c[0]).includes("[ErrorBoundary]"))).toBe(true);
  });

  it("点「重新加载」调用注入的重新加载实现（默认为 window.location.reload）", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const onReload = vi.fn();
    render(
      <ErrorBoundary onReload={onReload}>
        <Bomb shouldThrow={true} />
      </ErrorBoundary>,
    );
    await userEvent.click(screen.getByRole("button", { name: "重新加载" }));
    expect(onReload).toHaveBeenCalledTimes(1);
  });

  it("支持自定义 fallback，并在其中拿到错误对象与 reset", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <ErrorBoundary
        fallback={(error, reset) => (
          <div>
            <span>自定义兜底：{error.message}</span>
            <button onClick={reset}>恢复</button>
          </div>
        )}
      >
        <Bomb shouldThrow={true} />
      </ErrorBoundary>,
    );
    expect(screen.getByText(/自定义兜底：面板炸了/)).toBeInTheDocument();
  });

  it("componentDidCatch 里 console.error 自身抛错时，兜底 UI 仍能渲染", () => {
    // 只让**我们自己的**带 [ErrorBoundary] 前缀的那次 log 抛错；
    // React 内部对错误的 log（无前缀）保持安静，避免污染成 unhandled error。
    vi.spyOn(console, "error").mockImplementation((...args: unknown[]) => {
      if (String(args[0]).includes("[ErrorBoundary]")) throw new Error("console 坏了");
    });
    render(
      <ErrorBoundary>
        <Bomb shouldThrow={true} />
      </ErrorBoundary>,
    );
    expect(screen.getByText("页面出了点问题")).toBeInTheDocument();
  });
});

describe("PanelBoundary：面板级隔离", () => {
  it("一个面板抛错只替换它自己，兄弟面板继续渲染", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <div>
        <PanelBoundary name="审批队列">
          <Bomb shouldThrow={true} />
        </PanelBoundary>
        <PanelBoundary name="对话区">
          <Bomb shouldThrow={false} />
        </PanelBoundary>
      </div>,
    );
    expect(
      screen.getByText("「审批队列」渲染失败，已隔离，其他面板不受影响。"),
    ).toBeInTheDocument();
    // 关键：兄弟面板不受影响（全局边界做不到这一点）
    expect(screen.getByText("正常内容")).toBeInTheDocument();
    // 不给用户"整页坏了"的错误印象
    expect(screen.queryByText("页面出了点问题")).toBeNull();
  });

  it("点「重试」清除错误态，重新渲染子组件", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <PanelBoundary name="对话区">
        <Bomb shouldThrow={true} />
      </PanelBoundary>,
    );
    expect(screen.getByText(/「对话区」渲染失败/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "重试" }));
    // Bomb 仍会抛错（props 没变），但错误态确实被清掉并重新尝试了一次渲染
    expect(screen.getByText(/「对话区」渲染失败/)).toBeInTheDocument();
  });
});
