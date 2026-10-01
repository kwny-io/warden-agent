/**
 * 多副本 e2e 的"起副本"工具箱（建库 → 起 N 个副本 → 探活 → 收掉 → 删库）。
 *
 * **为什么这些副本不进 `playwright.config.ts` 的 `webServer`**：
 * `webServer` 的模型是"测试开始前统一起好、跑完由 Playwright 收掉"，而多副本有两个硬需求
 * 它满足不了：
 *   1. 两个副本必须指向**同一个库**，且这个库必须是**每次运行新建的空库**。库里有跨请求
 *      累积的状态：限流计数（键=来源 IP，窗口 60 秒）与幂等表。上一次运行留下的计数会让
 *      下一次运行**第一个**请求就 429；留下的幂等记录会让"同 key 应重新执行"的对照用例假绿。
 *      而"建库"必须发生在"起服务"之前、"删库"必须发生在"收服务"之后——只有能控制完整
 *      生命周期的调用方做得到。
 *   2. 用例要的不是一个档位，而是**共享开关开/关两组副本同时在线**（对照组见 spec 里的注释）。
 *
 * 于是：库与进程的生命周期都归 spec 管（`beforeAll` / `afterAll`）。
 * 端口与密钥口径仍走 `endpoints.ts`（单一来源，与其它 spec 一致）。
 */
import { spawn, spawnSync, type ChildProcess } from 'node:child_process'
import { existsSync, mkdtempSync } from 'node:fs'
import net from 'node:net'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { replicaBase } from './endpoints'

const here = path.dirname(fileURLToPath(import.meta.url)) // web/e2e
const repoRoot = path.resolve(here, '..', '..') // 仓库根

/** pg 工具脚本的路径（探活/建库/删库都在它里面，见文件顶部注释）。 */
const PG_ADMIN = path.join(repoRoot, 'scripts', 'e2e_pg_admin.py')

/**
 * 找 venv 里的解释器。
 *
 * 这里与 `start-e2e-server.mjs` 里那段**是重复的**（那边是 .mjs、这边是 .ts，
 * 互相 import 不上，而 .mjs 又没法 `import` 一个 .ts）。重复的代价是"两处要一起改"，
 * 所以把它收敛到**一处判据**上：候选路径与报错文案保持一致，并允许用
 * `WARDEN_E2E_PYTHON` 直接指定（两边都认这个变量）。
 */
export function findPython(): string {
  const override = process.env.WARDEN_E2E_PYTHON
  if (override) return override
  const candidates = [
    path.join(repoRoot, '.venv', 'Scripts', 'python.exe'),
    path.join(repoRoot, '.venv', 'bin', 'python'),
  ]
  const found = candidates.find((p) => existsSync(p))
  if (!found) {
    throw new Error(`找不到虚拟环境解释器，找过：\n  ${candidates.join('\n  ')}`)
  }
  return found
}

/** 本机/CI 的 PG 连接口径（与 pytest 的 PG 测试同一套环境变量）。 */
function pgEnv(): Record<string, string> {
  return {
    WARDEN_TEST_PG_HOST: process.env.WARDEN_TEST_PG_HOST ?? 'localhost',
    WARDEN_TEST_PG_PORT: process.env.WARDEN_TEST_PG_PORT ?? '5432',
    WARDEN_TEST_PG_DB: process.env.WARDEN_TEST_PG_DB ?? 'warden',
    WARDEN_TEST_PG_USER: process.env.WARDEN_TEST_PG_USER ?? 'postgres',
    WARDEN_TEST_PG_PASSWORD: process.env.WARDEN_TEST_PG_PASSWORD ?? '',
  }
}

function runPgAdmin(args: string[]): { code: number; out: string; err: string } {
  const res = spawnSync(findPython(), [PG_ADMIN, ...args], { env: { ...process.env, ...pgEnv() } })
  if (res.error) throw new Error(`起不了 pg 工具：${res.error.message}`)
  return {
    code: res.status ?? 1,
    out: (res.stdout ?? '').toString().trim(),
    err: (res.stderr ?? '').toString().trim(),
  }
}

/**
 * 同步探活：在**收集阶段**（文件顶层）就能决定"整体 skip"。
 *
 * 同步是刻意的——Playwright 的 `test.skip(条件, 原因)` 要一个已知的布尔值，
 * 放在顶层算比放在 `beforeAll` 里"跑到一半再跳"更清楚：报告里会显示成
 * "skipped: 需要可用的 PostgreSQL（…）"，而不是一条失败的 beforeAll。
 */
export function pgProbe(): { available: boolean; detail: string } {
  try {
    const res = runPgAdmin(['probe'])
    if (res.code === 0) return { available: true, detail: 'ok' }
    // 退出码 2 = 没有可用的库（环境没准备好），与"工具出错"(1) 区分开
    return { available: false, detail: res.err || `exit=${res.code}` }
  } catch (e) {
    return { available: false, detail: e instanceof Error ? e.message : String(e) }
  }
}

/** 建一个**空的**库，返回库名（同名库会被先删掉再建，见 pg 工具里的注释）。 */
export function createFreshDatabase(): string {
  // 只用小写字母/数字/下划线：脚本会校验这个名字，非法名直接拒绝
  const name = `warden_e2e_${Date.now().toString(36)}_${process.pid.toString(36)}`
  const res = runPgAdmin(['create', name])
  if (res.code !== 0) throw new Error(`建库失败（${name}）：${res.err || `exit=${res.code}`}`)
  return res.out || name
}

/** 删库（`WITH (FORCE)`，副本还没完全释放连接时也能删掉）。失败只告警，不让用例红。 */
export function dropDatabase(name: string): void {
  const res = runPgAdmin(['drop', name])
  if (res.code !== 0) {
    console.warn(`[多副本] 删库失败（${name}）：${res.err || `exit=${res.code}`}——它在 CI 的 PG 里会留下来`)
  }
}

export interface Replica {
  name: string
  port: number
  base: string
  /** 该副本最近的后端日志（断言失败时打出来，不然只能看到"连不上"）。 */
  log: () => string
  stop: () => Promise<void>
}

/**
 * 起一个副本。环境变量的组装**只在这里做一处**——两处各写一遍必然漂移，
 * 而漂移的后果是"某个副本其实是无鉴权/无共享状态档"，用例会以假绿的形式骗人。
 */
export function startReplica(opts: {
  name: string
  port: number
  dbName: string
  sharedState: boolean
  rateLimit?: string
}): Replica {
  const port = opts.port
  const base = replicaBase(port)
  // 每个副本一个**自己的** SQLite 目录：多副本场景下不该走 SQLite（库名由 PG 提供），
  // 但万一 PG 环境变量丢了、后端静默回退到 SQLite，各副本**各自的**文件会让用例立刻红，
  // 而不是因为"碰巧共用了同一个文件"而假绿。同理，它也绝不能落到开发用的
  // warden-agent-local.db 上（会往开发库里混测试会话）。
  const stateDir = mkdtempSync(path.join(os.tmpdir(), `warden-e2e-replica-${port}-`))
  const env: Record<string, string> = {
    ...process.env,
    PORT: String(port),
    WARDEN_HOST: '127.0.0.1',
    // 产物已由 global-setup 构建过一次：这里再构建就是 6 个进程抢着写 web/dist
    WARDEN_E2E_PREBUILT: '1',
    // 无鉴权档：本组用例测的是"多副本的协调状态"，鉴权有自己的用例（auth.spec.ts）
    WARDEN_ALLOW_ANON: '1',
    // 强制离线假模型：后端启动时会读仓库里的 .env，那会让 e2e 悄悄打真模型
    // （非确定、要联网、花钱，而断言不会红）
    DEEPSEEK_API_KEY: '',
    WARDEN_DB_PATH: path.join(stateDir, 'replica.db'),
    WARDEN_SHARED_STATE: opts.sharedState ? '1' : '0',
    // PG 是"配了 WARDEN_PG_HOST 就用 PostgreSQL"（见 run_server.py 的 _store_from_env）
    WARDEN_PG_HOST: pgEnv().WARDEN_TEST_PG_HOST ?? '',
    WARDEN_PG_PORT: pgEnv().WARDEN_TEST_PG_PORT ?? '',
    WARDEN_PG_DB: opts.dbName,
    WARDEN_PG_USER: pgEnv().WARDEN_TEST_PG_USER ?? '',
    WARDEN_PG_PASSWORD: pgEnv().WARDEN_TEST_PG_PASSWORD ?? '',
  }
  if (opts.rateLimit) env.WARDEN_RATE_LIMIT = opts.rateLimit

  const child: ChildProcess = spawn(findPython(), ['-m', 'warden_agent.web.run_server'], {
    cwd: repoRoot,
    env,
    stdio: ['ignore', 'pipe', 'pipe'],
  })

  // 只留尾部若干行：日志的作用是"失败时能看见后端说了什么"，不需要全量
  const lines: string[] = []
  const keep = (buf: Buffer): void => {
    for (const line of buf.toString().split(/\r?\n/)) {
      if (line.trim()) lines.push(`[${opts.name}] ${line}`)
    }
    while (lines.length > 60) lines.shift()
  }
  child.stdout?.on('data', keep)
  child.stderr?.on('data', keep)
  child.on('exit', (code, signal) => lines.push(`[${opts.name}] 退出 code=${code} signal=${signal}`))

  return {
    name: opts.name,
    port,
    base,
    log: () => lines.join('\n'),
    stop: async () => {
      if (child.exitCode !== null || child.signalCode !== null) return
      child.kill('SIGTERM')
      // Windows 上 Node 的 kill 走 TerminateProcess，通常立刻结束；但 uvicorn 可能
      // 还有子进程/句柄没释放，所以再等一会，仍不退出就 taskkill 整棵进程树
      // （留着孤儿进程的后果是"下次跑变成端口被占"的假失败）。
      const exited = await waitForExit(child, 5000)
      if (!exited && process.platform === 'win32' && child.pid) {
        spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { stdio: 'ignore' })
        await waitForExit(child, 3000)
      }
    },
  }
}

function waitForExit(child: ChildProcess, timeoutMs: number): Promise<boolean> {
  if (child.exitCode !== null || child.signalCode !== null) return Promise.resolve(true)
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve(false), timeoutMs)
    child.once('exit', () => {
      clearTimeout(timer)
      resolve(true)
    })
  })
}

/**
 * 等一个副本真的能服务请求。
 *
 * 用 HTTP 探 `/health/live` 而不是"端口能连上"：uvicorn 先绑端口、后跑 lifespan 启动钩子，
 * 所以"端口开了"不等于"应用准备好了"（这中间打过去会 5xx）。
 * `/health/live` 同时**豁免鉴权与限流**（见 server.py 的 `_is_public`），
 * 所以在限流档位很紧的副本上探活也不会吃掉额度——这一点必须成立，
 * 否则"探活 2 次"会把 3/60 的额度吃掉 2 次，用例红在原因之外的地方。
 */
export async function waitForHealth(replica: Replica, timeoutMs = 60_000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  let lastError = '未知'
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`${replica.base}/health/live`)
      if (res.ok) return
      lastError = `HTTP ${res.status}`
    } catch (e) {
      lastError = e instanceof Error ? e.message : String(e)
    }
    await new Promise((r) => setTimeout(r, 250))
  }
  throw new Error(
    `副本 ${replica.name}（:${replica.port}）在 ${timeoutMs}ms 内没就绪：${lastError}\n` +
      `后端日志尾部：\n${replica.log()}`,
  )
}

/** 端口是否已被占用（起副本前先探一下，避免"起不来"被误读成代码问题）。 */
export async function portFree(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const server = net.createServer()
    server.once('error', () => resolve(false))
    server.once('listening', () => server.close(() => resolve(true)))
    server.listen(port, '127.0.0.1')
  })
}
