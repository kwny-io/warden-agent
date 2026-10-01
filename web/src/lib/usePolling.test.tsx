// lib/usePolling.ts —— 轮询 Hook 的行为测试。
// 这个 Hook 被审批队列/模型/健康检查/对话列表全部复用，出错会 4 处一起白屏，
// 所以单独测：初次加载、错误捕获、resetKey 清数据、手动 refresh、定时轮询。
import { describe, it, expect, vi, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { usePolling } from "./usePolling";

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("usePolling", () => {
  it("挂载后立刻拉一次，把结果放进 data", async () => {
    const fetcher = vi.fn().mockResolvedValue(["a", "b"]);
    const { result } = renderHook(() => usePolling(fetcher, 1000));
    await waitFor(() => expect(result.current.data).toEqual(["a", "b"]));
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(result.current.error).toBeNull();
  });

  it("fetcher 抛错时把 Error.message 放进 error，而不是让组件崩掉", async () => {
    const fetcher = vi.fn().mockRejectedValue(new Error("500: boom"));
    const { result } = renderHook(() => usePolling(fetcher, 1000));
    await waitFor(() => expect(result.current.error).toBe("500: boom"));
    expect(result.current.data).toBeNull();
  });

  it("一次失败后下一次成功会清空 error", async () => {
    const fetcher = vi
      .fn()
      .mockRejectedValueOnce(new Error("网络抖动"))
      .mockResolvedValue({ ok: true });
    const { result } = renderHook(() => usePolling(fetcher, 1000));
    await waitFor(() => expect(result.current.error).toBe("网络抖动"));
    await act(async () => {
      result.current.refresh();
    });
    await waitFor(() => expect(result.current.error).toBeNull());
    expect(result.current.data).toEqual({ ok: true });
  });

  it("resetKey 变化时立即作废旧数据，避免显示上一个 run 的结果", async () => {
    const fetcher = vi.fn().mockResolvedValue("run-A-data");
    const { result, rerender } = renderHook(
      ({ key }: { key: string }) => usePolling(fetcher, 1000, key),
      { initialProps: { key: "A" } },
    );
    await waitFor(() => expect(result.current.data).toBe("run-A-data"));

    fetcher.mockResolvedValue("run-B-data");
    rerender({ key: "B" });
    // 同步检查：换 key 的那一帧不该还挂着 A 的旧数据
    expect(result.current.data).toBeNull();
    await waitFor(() => expect(result.current.data).toBe("run-B-data"));
  });

  it("按 intervalMs 定时重复拉取", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn().mockResolvedValue(1);
    renderHook(() => usePolling(fetcher, 2000));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(fetcher).toHaveBeenCalledTimes(2);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000);
    });
    expect(fetcher).toHaveBeenCalledTimes(4);
  });

  it("标签页切到后台：暂停轮询；回到前台：立刻补一次再恢复定时", async () => {
    vi.useFakeTimers();
    let hidden = false;
    vi.spyOn(document, "hidden", "get").mockImplementation(() => hidden);
    const fetcher = vi.fn().mockResolvedValue(1);
    renderHook(() => usePolling(fetcher, 1000));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(fetcher).toHaveBeenCalledTimes(1);

    // 切后台：停掉定时器
    hidden = true;
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(fetcher).toHaveBeenCalledTimes(1); // 后台期间不再打后端

    // 回前台：马上补一次，并恢复定时
    hidden = false;
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(fetcher).toHaveBeenCalledTimes(2);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(fetcher).toHaveBeenCalledTimes(3);
  });

  it("挂载时标签页已在后台：只拉一次，不起定时器（不白烧请求）", async () => {
    vi.useFakeTimers();
    vi.spyOn(document, "hidden", "get").mockReturnValue(true);
    const fetcher = vi.fn().mockResolvedValue(1);
    renderHook(() => usePolling(fetcher, 1000));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("卸载后慢请求才返回：不再 setState，也不报错", async () => {
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    let resolveIt: ((v: string) => void) | undefined;
    const fetcher = vi.fn(
      () =>
        new Promise<string>((res) => {
          resolveIt = res;
        }),
    );
    const { unmount } = renderHook(() => usePolling(fetcher, 1000));
    expect(fetcher).toHaveBeenCalledTimes(1);
    unmount();
    await act(async () => {
      resolveIt?.("late-arrival");
    });
    expect(errSpy).not.toHaveBeenCalled();
  });

  // —— 以下三条是 P0-2 的回归测试：代际守卫 / 真取消 / 失败退避 ——

  it("乱序响应：旧一代的慢响应晚到，不能覆盖新一代的数据", async () => {
    let resolveA: ((v: string) => void) | undefined;
    const fetcher = vi
      .fn()
      .mockImplementationOnce(
        () =>
          new Promise<string>((res) => {
            resolveA = res;
          }),
      )
      .mockResolvedValueOnce("B-data");
    const { result, rerender } = renderHook(
      ({ key }: { key: string }) => usePolling(fetcher, 1000, key),
      { initialProps: { key: "A" } },
    );
    rerender({ key: "B" });
    await waitFor(() => expect(result.current.data).toBe("B-data"));
    // A 的响应现在才回来——没有代际守卫时它会盖掉 B
    await act(async () => {
      resolveA?.("A-data");
    });
    expect(result.current.data).toBe("B-data");
  });

  it("卸载时真正 abort 在途请求（不只丢结果），且 abort 不被当成错误显示", async () => {
    let captured: AbortSignal | undefined;
    const fetcher = vi.fn((signal?: AbortSignal) => {
      captured = signal;
      return new Promise<string>(() => {}); // 永不返回
    });
    const { result, unmount } = renderHook(() => usePolling(fetcher, 1000));
    await waitFor(() => expect(captured).toBeDefined());
    expect(captured?.aborted).toBe(false);
    unmount();
    expect(captured?.aborted).toBe(true);
    expect(result.current.error).toBeNull();
  });

  it("连续失败后指数退避：不按原节奏猛打后端", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn().mockRejectedValue(new Error("503: 后端挂了"));
    renderHook(() => usePolling(fetcher, 1000));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000); // 失败 1 次 → 已退到 2s，t=1000 不该再打
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000); // t=2000：第 2 次尝试
    });
    expect(fetcher).toHaveBeenCalledTimes(2);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000); // 失败 2 次 → 4s，t=6000：第 3 次
    });
    expect(fetcher).toHaveBeenCalledTimes(3);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000); // 失败 3 次 → 8s，t=10000 仍不该打
    });
    expect(fetcher).toHaveBeenCalledTimes(3);
  });
});
