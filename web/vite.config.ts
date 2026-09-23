import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 开发模式：Vite(5173) 把 /chat、/events 等 API 请求代理到后端 FastAPI(8000)。
// 这样前端和后端分开热更新，交互式开发；SSE 走代理也正常。
// 生产模式：npm run build 生成的静态产物由 FastAPI 挂载托管，这里只负责开发。
export default defineConfig({
  plugins: [react()],
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
