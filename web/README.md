# Warden Agent · Web 前端

交互式前端控制台：**React + TypeScript + Tailwind CSS**（Vite 构建）。

它消费后端 FastAPI 的 HTTP + SSE 接口，提供：
- 🗨️ **流式对话**（打字机效果，SSE 增量）
- 🛂 **审批队列**（查看 + 批准/拒绝）
- 📊 **信息面板**（run 状态 / 能力 / 记忆 / 健康检查）

---

## 目录结构

```
web/
├── index.html          # SPA 入口
├── vite.config.ts      # 开发代理（/chat 等 → 后端 8000）
├── tailwind.config.js  # 深色控制台主题
└── src/
    ├── main.tsx        # React 挂载
    ├── App.tsx         # 应用壳（顶栏 + 左聊天 + 右侧栏）
    ├── index.css       # Tailwind 指令
    ├── components/
    │   ├── ChatView.tsx      # 流式对话 + 内联审批
    │   ├── ApprovalPanel.tsx # 审批队列
    │   └── InfoPanel.tsx     # run 状态 / 能力 / 记忆 / 健康
    └── lib/
        ├── api.ts            # 后端 REST + SSE 封装
        ├── types.ts          # 与后端对齐的类型
        └── usePolling.ts     # 轮询 hook
```

---

## 开发模式（热更新 + 代理）

1. 先启动后端（8000 端口，见项目根 README）：
   ```bash
   py -m warden_agent.web.run_server
   ```
2. 启动 Vite dev server（5173，自动代理 API → 8000）：
   ```bash
   cd web
   npm install
   npm run dev
   ```
3. 打开 **http://127.0.0.1:5173**

> 开发时前端和后端分开热更新；SSE、审批等请求由 Vite 代理到后端，交互式开发体验。

---

## 生产构建

```bash
cd web
npm run build   # 产物输出到 web/dist
```

构建产物（`web/dist/index.html` + `assets/*`）由 **FastAPI 自动托管**：
- `GET /` 返回 SPA
- `GET /assets/*` 返回静态 JS/CSS

所以**单端口部署**：一个 uvicorn 服务既当 API 又当前端。

---

## Docker 部署

项目根 `Dockerfile` 已是多阶段构建：
- 阶段 1：node 构建前端 → `web/dist`
- 阶段 2：Python 后端 + 把 `dist` 放进 `/app/web/dist` 由 FastAPI 托管

```bash
docker build -t warden-agent .
docker run -p 8000:8000 -e DEEPSEEK_API_KEY=sk-xxx warden-agent
# 打开 http://127.0.0.1:8000
```

---

## 后端对接接口

前端依赖这些后端端点（`web/src/lib/api.ts`）：

| 方法 & 路径 | 用途 |
|---|---|
| `POST /chat/{run_id}` | 非流式对话 |
| `POST /chat/stream/{run_id}` | 流式对话（SSE） |
| `GET /status/{run_id}` | 查 run 状态 |
| `GET /approvals` | 审批队列 |
| `POST /approve/{run_id}`、`POST /reject/{run_id}` | 批准 / 拒绝 |
| `GET /capabilities` | 能力（工具/特性） |
| `GET /memory/{scope}` | 记忆 |
| `GET /health/live` | 健康检查 |
