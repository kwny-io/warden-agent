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
      // 所有后端 API 和 SSE 都代理过去
      "/chat": "http://127.0.0.1:8000",
      "/events": "http://127.0.0.1:8000",
      "/status": "http://127.0.0.1:8000",
      "/approvals": "http://127.0.0.1:8000",
      "/approve": "http://127.0.0.1:8000",
      "/reject": "http://127.0.0.1:8000",
      "/capabilities": "http://127.0.0.1:8000",
      "/memory": "http://127.0.0.1:8000",
      "/audit": "http://127.0.0.1:8000",
      "/health": "http://127.0.0.1:8000",
    },
  },
});
