/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 开发模式：Vite(5173) 把 /chat、/events 等 API 请求代理到后端 FastAPI(8000)。
// 这样前端和后端分开热更新，交互式开发；SSE 走代理也正常。
// 生产模式：npm run build 生成的静态产物由 FastAPI 挂载托管，这里只负责开发。
export default defineConfig({
  plugins: [react()],
  test: {
    // 单元测试跑在 jsdom 里（组件渲染需要 DOM；fetch/SSE 全部用 vi.fn mock）。
    environment: "jsdom",
    // 打开后 describe/it/expect 是全局的，测试文件不必每行 import。
    globals: true,
    // 每个测试文件先加载：注入 jest-dom 断言扩展（toBeInTheDocument 等）。
    setupFiles: ["./src/test/setup.ts"],
    // 只扫 src 下的 *.test.ts(x)，避免误跑 dist/ 里的编译产物。
    include: ["src/**/*.test.{ts,tsx}"],
    // 覆盖率门禁：数字不是 KPI，而是「防倒退」的锁——新代码使覆盖率掉下去
    // 会让 CI 变红，逼着补测。阈值设在当前实测水位（见下方注释），只允许往上调。
    coverage: {
      provider: "v8",
      reporter: ["text", "json-summary", "lcov"],
      // 只统计前端源码：测试自身、测试夹具、类型声明、入口挂载不算覆盖率
      include: ["src/**/*.{ts,tsx}"],
      exclude: [
        "src/**/*.test.{ts,tsx}",
        "src/test/**",
        "src/vite-env.d.ts",
        "src/main.tsx",
        // 纯类型声明文件：编译后没有任何可执行代码，算进分母只会稀释覆盖率
        "src/lib/types.ts",
      ],
      // 实测水位：lines 93 / statements 92 / functions 84 / branches 85（四舍五入后留
      // 一点余量作为下限，不设成刚好等于实测值，否则任何无害改动都会卡 CI）。
      thresholds: {
        lines: 90,
        statements: 90,
        functions: 80,
        branches: 80,
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      // 所有后端 API 与 SSE 都代理到后端（开发时前后端分离热更新）。
      // 用**一条正则**而不是逐个前缀枚举：漏一个前缀 = 该接口在 dev 下被 Vite 的
      // SPA fallback 接走、返回 index.html，前端 `res.json()` 解析失败。
      // 这个坑真实发生过：/runs /users /models /messages 当初就漏在了枚举之外，
      // 导致 dev 模式下对话列表、历史恢复、用户列表、模型面板全部不可用
      // （生产同源所以没暴露）。加前缀时请一并补进这条正则。
      "^(/chat|/events|/status|/approvals|/approve|/reject|/capabilities|/memory|/audit|/health|/runs|/users|/models|/messages|/alerts)":
        "http://127.0.0.1:8000",
    },
  },
});
