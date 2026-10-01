import { expect, test, type APIRequestContext } from '@playwright/test'

import {
  LOCAL_REPLICA_PORTS,
  RATE_REPLICA_PORTS,
  REPLICA_RATE_LIMIT,
  SHARED_REPLICA_PORTS,
} from './endpoints'
import {
  createFreshDatabase,
  dropDatabase,
  pgProbe,
  portFree,
  startReplica,
  waitForHealth,
  type Replica,
} from './replicas'

/**
 * 真浏览器/真副本跑一遍**多副本部署**（`WARDEN_SHARED_STATE=1` + 同一个 PostgreSQL 库）。
 *
 * 为什么需要这一套：`docs/deployment-boundaries.md` 一直有一条诚实边界——"多副本部署
 * 要求共享状态，代码里支持，但**没在真环境里跑过**"。而这一条恰好是最容易被问到、
 * 也最容易在真实事故里炸的：协调状态（幂等表 / 事件总线 / 限流计数 / Run 锁）如果
 * 只在进程内，多副本下会以"静默劣化"的形式出问题——
 *   · 客户端在 A 副本断了，连到 B 副本**续读不到**这一轮的结果；
 *   · 同一个幂等键重试打到 B，**又执行了一遍**（在最坏情况下是重复执行一次写操作）；
 *   · 限流额度**按副本各算一份**，加了副本数等于把额度乘以副本数。
 * 这三种都不会报错、不会 500，只会让"多副本"这件事看起来能用。
 *
 * 验收口径：**每组都不是只看共享组，而是配一个同库对照**（只关掉 `WARDEN_SHARED_STATE`）。
 * 只有对照在，才能把差别归因到"共享状态"这一个变量上——否则"跨副本读不到"也可能
 * 是因为别的原因（比如根本没写进库），那就会把一个真 bug 读成"预期行为"。
 *
 * 需要 PostgreSQL：本机用 `docker run -d --name warden-pg-test -e
 * POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=warden -p 5432:5432 postgres:16-alpine`，
 * CI 里由 quality job 的 service container 提供（连接口径见 `replicas.ts` 的 `pgEnv`）。
 * 没有可用的库时整体 skip（与 pytest 侧 `skipif` 同构），而不是假装通过。
 */

// 这一组要起 6 个后端 + 建/删库，默认 30 秒不够；但也不该无限等——
// 真起不来时应该"较快地红"，而不是把 CI 挂死。
test.setTimeout(180_000)

const SALT = Date.now().toString(36)
const pg = pgProbe()

/** 播种用的一句话：不点名任何工具名，假模型于是走"直接回答"那条规则（确定、瞬间）。 */
const prompt = (tag: string): string => `多副本验证 ${tag}：请直接回答，不要调用工具。`

/**
 * 两类流用**不同的字段名**报「这是什么事件」，这是最阴的一个坑：
 * - `/chat/stream/{run_id}` 直接透传会话事件：`{"type":"delta"|"final"|…}`
 * - `/events/{run_id}` 透传的是事件总线上的消息：`{"event":"final"|"error"|…}`
 *
 * 第一版只按 `event` 找终态，于是在 chat/stream 上「永远读不到 final」——其实 final 早就在，
 * 只是字段名不同（而且因为找不到终态，流会一直读到超时）。统一用 `frameName()` 取。
 */
interface SseFrame {
  type?: string
  event?: string
  text?: string
  message?: string
}

const frameName = (f: SseFrame): string | undefined => f.type ?? f.event
const isTerminal = (f: SseFrame): boolean => frameName(f) === 'final' || frameName(f) === 'error'
const finalText = (frames: SseFrame[]): string | undefined =>
  frames.find((f) => frameName(f) === 'final')?.text

/**
 * 读一段 SSE，直到出现终态事件（final/error）或超时。
 *
 * **为什么不用 `request.get`**：Playwright 的 APIRequestContext 会把响整体缓冲完再返回，
 * 而 `/events/{run_id}` 在"没有事件"时是**不会自己结束**的（它每 15 秒发一次心跳，
 * 一直挂着）。用它读对照组就会把用例挂到超时，而这个用例要断言的恰恰是"读不到终态"。
 * 所以这里用裸 `fetch` + `AbortController`，并且**超时算正常返回**（返回已收到的帧）。
 */
async function readSse(url: string, init: RequestInit, timeoutMs: number): Promise<SseFrame[]> {
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeoutMs)
  const frames: SseFrame[] = []
  try {
    const res = await fetch(url, { ...init, signal: ctrl.signal })
    if (!res.ok) throw new Error(`SSE 请求失败：HTTP ${res.status} ${await res.text()}`)
    if (!res.body) throw new Error('SSE 响应没有 body')
    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      let cut = buf.indexOf('\n\n')
      while (cut >= 0) {
        const chunk = buf.slice(0, cut)
        buf = buf.slice(cut + 2)
        for (const line of chunk.split('\n')) {
          if (line.startsWith('data: ')) frames.push(JSON.parse(line.slice(6)) as SseFrame)
        }
        if (frames.some(isTerminal)) return frames
        cut = buf.indexOf('\n\n')
      }
    }
  } catch (e) {
    // 超时（abort）与"流被服务端关掉"都按"读到什么就算什么"处理：
    // 这个函数的两类用法（等终态 / 确认没有终态）都不该因为超时抛出
    if (!(e instanceof Error) || e.name !== 'AbortError') {
      // 真正的连接错误要冒出来：那说明副本没在服务，不是"没有事件"
      if (!(e instanceof Error) || !/aborted/i.test(e.message)) throw e
    }
  } finally {
    clearTimeout(timer)
    ctrl.abort()
  }
  return frames
}

const jsonPost = (text: string): RequestInit => ({
  method: 'POST',
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify({ text }),
})

/**
 * 会话第一条永远是 system（系统提示词），断言「用户/助手说了什么」时必须过掉它——
 * 否则 `rows[0]` 拿到的是系统提示词，看起来像「用户消息被写坏了」。
 */
const talkRows = <T extends { role: string }>(rows: T[]): T[] =>
  rows.filter((r) => r.role !== 'system')

/** 只要角色序列（同样滤掉 system）。 */
const rolesOf = (rows: Array<{ role: string }>): string[] => talkRows(rows).map((r) => r.role)

const messagesOf = async (
  request: APIRequestContext,
  base: string,
  runId: string,
): Promise<Array<{ role: string; content: string | null }>> => {
  const res = await request.get(`${base}/messages/${runId}`)
  expect(res.ok(), `读 /messages 失败：HTTP ${res.status()}`).toBeTruthy()
  return (await res.json()) as Array<{ role: string; content: string | null }>
}

// ── 三组副本 + 每次运行新建的空库 ──────────────────────────────────────
// 两个库（不是共用一个）：限流组的额度是**按来源 IP 记在共享库里**的，而本次运行里
// 所有用例都从 127.0.0.1 发请求；如果限流组与前面几组共用一个库，前面用例花掉的额度
// 会把它的「第 1 个请求」直接变成 429（实测就是这么红的）。单独一个空库，“额度 3”
// 这个前提才真的成立。
let dbName: string | null = null
let rateDbName: string | null = null
const started: Replica[] = []
let shared: Replica[] = []
let local: Replica[] = []
let rate: Replica[] = []

test.describe('多副本（共享状态库）', () => {
  test.skip(!pg.available, `需要可用的 PostgreSQL：${pg.detail}（起法见本文件顶部注释）`)

  test.beforeAll(async () => {
    // 顶层的 test.setTimeout 只管用例，**管不到 hook**（见 test.d.ts 的说明：
    // hook 的 timeout 只能在 hook 里改）。这里要起 6 个后端 + 建一个空库，
    // 默认 30 秒必不够（实测 Windows 上就有 40+ 秒）。
    test.setTimeout(180_000)
    const allPorts = [...SHARED_REPLICA_PORTS, ...LOCAL_REPLICA_PORTS, ...RATE_REPLICA_PORTS]
    for (const port of allPorts) {
      // 先探端口：上一次跑崩（进程被杀、断电、afterAll 没跑到）会留下还在监听的
      // 后端，而“起不来”看起来像代码问题。这里直接红，并把怎么找出来写清楚。
      expect(
        await portFree(port),
        `端口 ${port} 被占用：有上一次跑留下的后端还在监听。\n` +
          `先看是谁：netstat -ano | findstr :${port}（最后一个数字是 PID），` +
          `再收掉：taskkill /PID <PID> /T /F`,
      ).toBe(true)
    }

    // 空库：限流计数与幂等表会跨请求累积，脏库会让用例红/假绿在原因之外的地方
    dbName = createFreshDatabase()
    // 限流组自己的空库：否则前面用例花掉的额度会把它的首个请求变成 429（见上方注释）
    rateDbName = createFreshDatabase()

    // 共享组：协调状态进库（幂等/事件/限流/锁都共享）
    shared = SHARED_REPLICA_PORTS.map((port, i) =>
      startReplica({ name: `shared-${i + 1}`, port, dbName: dbName as string, sharedState: true }),
    )
    // 对照组：**同一个库**，只关掉共享开关——变量只有一个
    local = LOCAL_REPLICA_PORTS.map((port, i) =>
      startReplica({ name: `local-${i + 1}`, port, dbName: dbName as string, sharedState: false }),
    )
    // 限流组：共享 + 很小的额度（额度口径在 endpoints.ts 的 REPLICA_RATE_LIMIT）
    rate = RATE_REPLICA_PORTS.map((port, i) =>
      startReplica({
        name: `rate-${i + 1}`,
        port,
        dbName: rateDbName as string,
        sharedState: true,
        rateLimit: REPLICA_RATE_LIMIT,
      }),
    )
    started.push(...shared, ...local, ...rate)
    await Promise.all(started.map((r) => waitForHealth(r)))
  })

  test.afterAll(async () => {
    // 同上：收 6 个进程 + 删库，也要自己申请够时间
    test.setTimeout(120_000)
    await Promise.all(started.map((r) => r.stop()))
    if (dbName) dropDatabase(dbName)
    if (rateDbName) dropDatabase(rateDbName)
  })

  test('会话历史跨副本：A 副本写的消息，B 副本（真浏览器）也读得到', async ({
    page,
    request,
  }) => {
    const runId = `run-e2e-mr-hist-${SALT}`
    const text = prompt('历史')

    const res = await request.post(`${shared[0]!.base}/chat/${runId}`, { data: { text } })
    expect(res.ok(), `A 副本发消息失败：HTTP ${res.status()} ${await res.text()}`).toBeTruthy()
    const body = (await res.json()) as { kind: string; text: string }
    expect(body.kind).toBe('final')

    // ── 1) 接口层：B 副本能读到这一轮的两条消息 ──────────────────────────
    const rows = await messagesOf(request, shared[1]!.base, runId)
    expect(rolesOf(rows), 'B 副本读到的对话消息（已滤掉 system 提示词）').toEqual([
      'user',
      'assistant',
    ])
    const talk = talkRows(rows)
    expect(talk[0]!.content).toBe(text)
    expect(talk[1]!.content).toBe(body.text)

    // ── 2) 用户视角：**从 B 副本**打开控制台，两条消息都在 ───────────────
    // 这一条才是"多副本能不能用"的真判据：用户不需要知道自己在哪个副本上，
    // 他只会打开页面。页面从 B 的 origin 加载，所以它所有请求都打向 B。
    await page.goto(`${shared[1]!.base}/?run=${runId}`, { waitUntil: 'load' })
    await expect(page.getByRole('heading', { name: 'Warden Agent' })).toBeVisible()
    const box = page.locator('[aria-live="polite"]')
    await expect(box.getByText(text, { exact: false })).toHaveCount(1, { timeout: 20_000 })
    // 助手回复用"前 12 个字"来定位：假模型的整句里有些字符是通配的，抓前缀更稳
    await expect(
      box.getByText(body.text.slice(0, 12), { exact: false }),
    ).toHaveCount(1, { timeout: 20_000 })
  })

  test('事件跨副本：A 副本的流式结果，B 副本能续读到同一条 final', async ({ request }) => {
    const runId = `run-e2e-mr-events-${SALT}`
    const text = prompt('事件')

    // 在 A 上跑完这一轮（客户端把流读到终态）
    const onA = await readSse(
      `${shared[0]!.base}/chat/stream/${runId}`,
      jsonPost(text),
      20_000,
    )
    const expected = finalText(onA)
    expect(expected, `A 副本这一轮没有 final：${JSON.stringify(onA)}`).toBeTruthy()

    // 换到 B 副本续读同一轮（等价于“浏览器在 A 上断了，重连时被负载均衡分到 B”）
    const onB = await readSse(`${shared[1]!.base}/events/${runId}`, {}, 10_000)
    expect(
      finalText(onB),
      `B 副本没续读到 A 的 final（收到：${JSON.stringify(onB)}）`,
    ).toBe(expected)

    // 跨副本的对话记录与这条事件是同一轮（不是“事件读到了、消息不在”）
    expect(rolesOf(await messagesOf(request, shared[1]!.base, runId))).toEqual([
      'user',
      'assistant',
    ])
  })

  test('对照：关掉共享状态后，B 副本**读不到** A 副本的事件（证明上一条不是空跑）', async () => {
    const runId = `run-e2e-mr-events-local-${SALT}`
    const text = prompt('事件对照')

    const onA = await readSse(`${local[0]!.base}/chat/stream/${runId}`, jsonPost(text), 20_000)
    expect(finalText(onA)).toBeTruthy()

    // 4 秒内一帧都不该有：心跳是 15 秒一次，所以“没有 final”不是被心跳掩盖的
    const onB = await readSse(`${local[1]!.base}/events/${runId}`, {}, 4_000)
    expect(
      onB.filter((f) => frameName(f) === 'final'),
      '关掉共享状态后事件只在进程内，跨副本不该读得到——读到了说明共享开关没生效',
    ).toHaveLength(0)
    expect(onB).toHaveLength(0)
  })

  test('幂等键跨副本：同一个 key 打到另一个副本不会执行第二遍', async ({ request }) => {
    const runId = `run-e2e-mr-idem-${SALT}`
    const key = `mr-idem-${SALT}`
    const text = prompt('幂等')

    const first = await request.post(`${shared[0]!.base}/chat/${runId}`, {
      data: { text },
      headers: { 'Idempotency-Key': key },
    })
    expect(first.status()).toBe(200)
    const firstBody = (await first.json()) as Record<string, unknown>

    // 网关重试 / 用户重发 / 负载均衡换副本：同 key 打到**另一个**副本
    const second = await request.post(`${shared[1]!.base}/chat/${runId}`, {
      data: { text },
      headers: { 'Idempotency-Key': key },
    })
    expect(second.status()).toBe(200)
    expect(await second.json()).toEqual(firstBody)

    // 关键：副作用只发生了一次（若真跑了第二遍，这里会多出 user+assistant 两条）
    const rows = await messagesOf(request, shared[1]!.base, runId)
    expect(
      rolesOf(rows),
      '同 key 的第二遍不该真的执行',
    ).toEqual(['user', 'assistant'])
  })

  test('对照：关掉共享状态时，同 key 打到另一个副本**会真的执行第二遍**', async ({
    request,
  }) => {
    const runId = `run-e2e-mr-idem-local-${SALT}`
    const key = `mr-idem-local-${SALT}`
    const text = prompt('幂等对照')

    const first = await request.post(`${local[0]!.base}/chat/${runId}`, {
      data: { text },
      headers: { 'Idempotency-Key': key },
    })
    expect(first.status()).toBe(200)
    const firstBody = (await first.json()) as Record<string, unknown>

    // 同一个副本内仍然幂等：幂等表本身在工作（所以下面那条差别**只**来自"没共享"）
    const again = await request.post(`${local[0]!.base}/chat/${runId}`, {
      data: { text },
      headers: { 'Idempotency-Key': key },
    })
    expect(await again.json()).toEqual(firstBody)

    // 换到另一个副本：进程内的幂等表里没有这个 key → 第二次真的执行了
    const other = await request.post(`${local[1]!.base}/chat/${runId}`, {
      data: { text },
      headers: { 'Idempotency-Key': key },
    })
    expect(other.status()).toBe(200)

    const rows = await messagesOf(request, local[1]!.base, runId)
    expect(
      rolesOf(rows),
      '这正是 WARDEN_SHARED_STATE 存在的理由：关掉它，跨副本重放会重复执行',
    ).toEqual(['user', 'assistant', 'user', 'assistant'])
  })

  test(`限流计数跨副本共享：额度是全局一份，不是每副本一份（${REPLICA_RATE_LIMIT}）`, async ({
    request,
  }) => {
    const runId = `run-e2e-mr-rate-${SALT}`
    // 额度 3：下面一共发 4 个请求，第 4 个必须被拒
    const first = await request.post(`${rate[0]!.base}/chat/${runId}`, {
      data: { text: prompt('限流') },
    })
    expect(first.status(), '第 1 个请求（额度内）').toBe(200)

    expect(
      (await request.get(`${rate[0]!.base}/status/${runId}`)).status(),
      '第 2 个请求（同一副本，额度内）',
    ).toBe(200)
    expect(
      (await request.get(`${rate[1]!.base}/status/${runId}`)).status(),
      '第 3 个请求（**另一个**副本，仍应在额度内：计数是共享的）',
    ).toBe(200)

    // 第 4 个：若额度是每副本各算一份，这里一定还是 200——
    // 所以这条断言能真正区分"共享计数"与"各算一份"
    const blocked = await request.get(`${rate[1]!.base}/status/${runId}`)
    expect(
      blocked.status(),
      '第 4 个请求必须 429：额度按调用者（本机即来源 IP）在库里全局计数',
    ).toBe(429)
    // 错误体是 RFC7807 problem+json：字段是 errorCode（不是 code），并带 type/status/detail。
    // 写错字段名这件事本身就是刚才首跑红的地方——改成与其它 e2e（auth.spec.ts）同一套断言口径。
    expect(await blocked.json()).toMatchObject({
      errorCode: 'RATE_LIMITED',
      type: 'urn:warden:problem:rate_limited',
      status: 429,
    })
    expect(blocked.headers()['retry-after'], '429 必须带 Retry-After，否则客户端只能盲重试').toBeTruthy()

    // 健康探针不该被额度影响（负载均衡探活被限流挡住 = 副本被踢出集群）
    expect((await request.get(`${rate[1]!.base}/health/ready`)).status()).toBe(200)
  })
})
