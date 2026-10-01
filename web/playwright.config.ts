import { defineConfig, devices } from '@playwright/test'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { ANON_BASE, ANON_PORT, AUTH_API_KEY, AUTH_PORT } from './e2e/endpoints'

/**
 * 真浏览器端到端（Playwright）。
 *
 * **它存在的唯一理由**：`src/warden_agent/web/security_headers.py` 的 CSP 一直有个写在
 * 文档里的诚实边界——"用 TestClient 断言响应头存在，但**浏览器里 CSP 真的没挡到自己资源**
 * 这件事没验证过"。响应头对不等于页面能跑：CSP 挡的往往是运行期的东西
 * （行内 `style` 属性、动态注入的 `<script>`、SSE 连接、data: 字体……），
 * 这些东西**只有真浏览器加载真产物**才会暴露。
 *
 * 所以这里的 webServer **不是 vite dev server**（dev server 注入的东西与生产产物不是一回事，
 * 还带 HMR/websocket eval），而是 `e2e/start-e2e-server.mjs`：起真后端（由它挂载 `web/dist`
 * 并打上 CSP 等安全头）。于是覆盖的是"发布形态"的链路：后端头 + 打包产物 + 真浏览器引擎。
 * 产物构建由 `e2e/global-setup.ts` 做一次，两个后端实例共用（见那里的注释）。
 *
 * **两个档位**（同一份后端代码、不同环境变量）：
 *   · `anon`（8123）：无鉴权档 —— CSP 与对话/审批链路（`csp.spec.ts` / `approval.spec.ts`）；
 *   · `auth`（8124）：鉴权档（`WARDEN_API_KEY`）—— 登录路径：没密钥被 401 挡住、
 *     填对密钥后整条链路恢复（`auth.spec.ts`）。
 * 端口刻意不用 8000：8000 是项目默认端口，本机常被别的服务占着，
 * e2e 用一个不那么容易被撞的端口，避免"跑不过是因为端口被占"这种假失败。
 */
const here = path.dirname(fileURLToPath(import.meta.url))

/** 两个 webServer 条目共用的形状，只有 env 与端口不同。 */
const server = (name: string, port: number, extraEnv: Record<string, string>) => ({
  name,
  // 用 node 起而不是 `bash scripts/...`：Windows 上 Playwright 走 cmd.exe，
  // 那里不一定有 Git Bash（实测没有）。细节见 e2e/start-e2e-server.mjs 顶部注释。
  command: 'node e2e/start-e2e-server.mjs',
  cwd: here,
  url: `http://127.0.0.1:${port}/health/live`,
  // 必须带上 process.env：Playwright 的 env 是**替换**而不是合并，
  // 只给 PORT/WARDEN_HOST 会把 PATH 抹掉，连 node 都找不到自己的子进程。
  env: {
    ...process.env,
    PORT: String(port),
    WARDEN_HOST: '127.0.0.1',
    // 产物已由 globalSetup 构建：两个实例并发 `vite build` 会互相覆盖 web/dist。
    WARDEN_E2E_PREBUILT: '1',
    ...extraEnv,
  },
  reuseExistingServer: !process.env.CI,
  // 冷启后端要几秒（构建已在 globalSetup 里做过）；超时说明真有问题，别靠加时间掩盖。
  timeout: 120_000,
  stdout: 'pipe' as const,
  stderr: 'pipe' as const,
})

export default defineConfig({
  testDir: './e2e',
  globalSetup: './e2e/global-setup.ts',
  // 共用一个后端进程：并发 worker 之间会互相干扰（同一个 SQLite 库/同一份会话），
  // 而这些用例本身很快，串行跑总代价更低、结论更可信。
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : [['list']],
  use: {
    // 默认打无鉴权档；鉴权档的用例（auth.spec.ts）在文件内用 test.use 覆盖 baseURL。
    baseURL: ANON_BASE,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: [
    server('e2e-anon', ANON_PORT, { WARDEN_ALLOW_ANON: '1' }),
    server('e2e-auth', AUTH_PORT, { WARDEN_E2E_API_KEY: AUTH_API_KEY }),
  ],
})
