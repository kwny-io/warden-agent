// lib/deepLink.ts 的单元测试。
// 重点不在"能读能写"，而在**外部输入的边界**：URL 里的东西是别人给的，
// 读进来之前必须校验形态；写回历史时不能让"后退"变成退会话。
import { describe, it, expect, vi } from "vitest";
import { isValidRunId, readRunFromUrl, writeRunToUrl } from "./deepLink";

describe("isValidRunId", () => {
  it("放行常规 run-id", () => {
    expect(isValidRunId("run-abc_1.2:x")).toBe(true);
  });

  it("拒绝空、超长、含空格或路径片段的字符串", () => {
    expect(isValidRunId("")).toBe(false);
    expect(isValidRunId("a".repeat(81))).toBe(false);
    expect(isValidRunId("run abc")).toBe(false);
    expect(isValidRunId("../../etc/passwd")).toBe(false);
    expect(isValidRunId("run%2Fother")).toBe(false);
  });
});

describe("readRunFromUrl", () => {
  it("读到合法 run 参数", () => {
    expect(readRunFromUrl("?run=run-1")).toBe("run-1");
  });

  it("两侧空白会被 trim", () => {
    expect(readRunFromUrl("?run=%20run-1%20")).toBe("run-1");
  });

  it("没有 run 参数、空串、空查询串都返回 null", () => {
    expect(readRunFromUrl("")).toBeNull();
    expect(readRunFromUrl("?user=demo")).toBeNull();
    expect(readRunFromUrl("?run=")).toBeNull();
    expect(readRunFromUrl("?run=%20%20")).toBeNull();
  });

  it("形态不对的 run 参数当作“没有深链”，而不是拿它去请求", () => {
    expect(readRunFromUrl("?run=../../secrets")).toBeNull();
    expect(readRunFromUrl("?run=has%20space")).toBeNull();
  });

  it("同名的多个参数取第一个，不抛错", () => {
    expect(readRunFromUrl("?run=run-1&run=run-2")).toBe("run-1");
  });
});

describe("writeRunToUrl", () => {
  function fakeHistory() {
    return { replaceState: vi.fn() };
  }

  it("把 run 写进查询串（保留其它参数与 hash）", () => {
    const history = fakeHistory();
    writeRunToUrl("run-9", history, { pathname: "/", search: "?tab=1", hash: "#bottom" });
    expect(history.replaceState).toHaveBeenCalledWith(null, "", "/?tab=1&run=run-9#bottom");
  });

  it("用 replaceState 而不是 pushState：切会话不该污染后退历史", () => {
    const history = fakeHistory();
    writeRunToUrl("run-9", history, { pathname: "/", search: "", hash: "" });
    expect(history.replaceState).toHaveBeenCalledTimes(1);
  });

  it("已是当前值就不动历史", () => {
    const history = fakeHistory();
    writeRunToUrl("run-9", history, { pathname: "/", search: "?run=run-9", hash: "" });
    expect(history.replaceState).not.toHaveBeenCalled();
  });

  it("非法 ID 不写进 URL（免得把脏值制成可分享的链接）", () => {
    const history = fakeHistory();
    writeRunToUrl("bad id", history, { pathname: "/", search: "", hash: "" });
    expect(history.replaceState).not.toHaveBeenCalled();
  });

  it("history 写入抛异常时不冒泡（URL 只是便利功能）", () => {
    const history = {
      replaceState: vi.fn(() => {
        throw new Error("sandboxed");
      }),
    };
    expect(() =>
      writeRunToUrl("run-9", history, { pathname: "/", search: "", hash: "" }),
    ).not.toThrow();
  });
});
