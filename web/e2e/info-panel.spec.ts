import { expect, test, type Locator, type Page } from '@playwright/test'

import { ANON_BASE } from './endpoints'

/**
 * 真浏览器里跑一遍**能力面板**（右栏 `InfoPanel` 的「能力」区）。
 *
 * 为什么值得一套 e2e：这块是"用户判断**这东西现在到底能干什么**"的唯一入口，
 * 而它的失败模式全是**静默**的：
 *   · 工具清单折叠错/展开不回来 → 用户以为某些工具不存在，于是用别的路子绕；
 *   · 加载失败时只显示一行红字、`重试` 点不动 → 面板永久停在错误态（"刷新页面试试"
 *     成了唯一出路，而这是个能用按钮解决的问题）；
 *   · **演示档的"这不是真能力"提示不显示** → 最坏的一种：用户把写死的脚本回复
 *     当成模型能力、把内置样例当成真联网搜索结果。这一条不是体验问题，是**误导**。
 *
 * 断言口径：先向后端问真值（`/capabilities`），再拿它去比对页面——**不把"7 个工具"
 * 写死在用例里**（工具集变了用例不该红），但会把"前提"显式断言出来（见下），
 * 免得将来环境一变、用例悄悄变成空跑。
 */

/** run_id 带时间戳：e2e 必须可重复跑（同 run 上一轮残留的租约/审批会污染下一次）。 */
const SALT = Date.now().toString(36)
const PANEL_RUN = `run-e2e-panel-${SALT}`
const RETRY_RUN = `run-e2e-panel-retry-${SALT}`

/** 后端报告的能力真值。字段名与 `web/src/lib/types.ts` 的 `Capabilities` 对应。 */
type Capabilities = {
  tools: string[]
  features: {
    memory: boolean
    memory_persistent: boolean | null
    web: boolean
    web_search_mock: boolean
    model_kind_now: string
  }
}

async function openRun(page: Page, runId: string): Promise<void> {
  await page.goto(`/?run=${runId}`, { waitUntil: 'load' })
  // 右栏默认展开；标题在说明"我在看的是哪个面板"
  await expect(page.getByRole('heading', { name: '能力' })).toBeVisible()
}

/**
 * 把「能力」那一个面板圈出来。
 *
 * 为什么必须限定范围：右栏同时挂着**审批队列**、**运行信息**、**模型面板**，而
 * “待审批/最近决策”里会出现工具名（`工具=git.apply_patch`）。对全页用
 * `getByText('git.apply_patch')` 时会一次命中 3 个（能力芯片 + 审批队列的两处），
 * Playwright 的 strict 模式直接报 “resolved to 3 elements”。
 * 单跑本文件时审批队列是空的，所以不限定范围也能绿——**只有在全量跑里才红**，
 * 这正是“用例之间靠副作用隔离”的典型症状。
 */
function capsPanel(page: Page): Locator {
  return page.locator('div.wpanel').filter({ has: page.getByRole('heading', { name: '能力' }) })
}

test('能力面板：工具清单折叠/展开是真行为，且演示档的「这不是真能力」提示必须在', async ({
  page,
  request,
}) => {
  const res = await request.get(`${ANON_BASE}/capabilities`)
  expect(res.ok()).toBeTruthy()
  const caps = (await res.json()) as Capabilities

  // ── 前提断言（宁可红在前提上，也不要让下面的断言变成空跑）──────────────
  // 折叠行为只在工具数 > 6 时出现（`InfoPanel` 里是 `caps.tools.length > 6`）
  expect(caps.tools.length, '工具数不足 7 个时折叠行为无法被验证').toBeGreaterThan(6)
  // e2e 档必须跑在离线假模型上：不是的话，"演示脚本"提示与真实能力断言都没意义
  expect(caps.features.model_kind_now, 'e2e 档应为离线假模型').toBe('fake')

  await openRun(page, PANEL_RUN)

  const panel = capsPanel(page)
  const firstTool = caps.tools[0]
  const hiddenTool = caps.tools[caps.tools.length - 1] // 折叠态下看不见的那个

  // ── 折叠态：看得见靠前的，看不见最后一个 ──────────────────────────────
  await expect(panel.getByText(firstTool, { exact: true })).toBeVisible()
  await expect(panel.getByText(hiddenTool, { exact: true })).toHaveCount(0)

  // ── 展开：出现最后一个工具，并且按钮变成「收起」──────────────────────
  await panel.getByRole('button', { name: `展开全部 (${caps.tools.length})` }).click()
  await expect(panel.getByText(hiddenTool, { exact: true })).toBeVisible()
  // 展开态下可点的按钮是「收起」——它同时证明状态真的切了（不是复制了一份清单）
  // （exact 必带：页面上还有「收起对话栏」「收起对话列表」，它们都含「收起」）
  await panel.getByRole('button', { name: '收起', exact: true }).click()
  await expect(panel.getByText(hiddenTool, { exact: true })).toHaveCount(0)

  // ── 诚实性提示：演示模型必须自曝身份 ────────────────────────────────
  // 少这句话，用户会把写死的脚本回复读成"它真的会思考"
  await expect(panel.getByText('模型:演示脚本')).toBeVisible()
  await expect(
    panel.getByText('不能当作系统能力或回答质量的证据', { exact: false }),
  ).toBeVisible()

  // ── 诚实性提示：离线搜索/抓取也要自曝 ──────────────────────────────
  if (caps.features.web && caps.features.web_search_mock) {
    await expect(panel.getByText('web:离线演示')).toBeVisible()
    await expect(panel.getByText('不会真的建网', { exact: false })).toBeVisible()
  }

  // ── 记忆档：只有后端**明确说了**"进程内"才提醒（null 不吓人）────────
  if (caps.features.memory && caps.features.memory_persistent === false) {
    await expect(panel.getByText('memory:进程内重启即丢')).toBeVisible()
  } else {
    await expect(panel.getByText('memory:进程内重启即丢')).toHaveCount(0)
  }
})

test('能力加载失败时给的是「能用的重试」，不是一行死掉的红字', async ({ page }) => {
  // 第一次请求故意打 500（模拟后端/代理故障），之后放行到真后端。
  let failedOnce = false
  await page.route('**/capabilities', async (route) => {
    if (failedOnce) {
      await route.continue()
      return
    }
    failedOnce = true
    await route.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"e2e 模拟后端故障"}' })
  })

  await openRun(page, RETRY_RUN)

  await expect(page.getByText('加载失败', { exact: false })).toBeVisible()
  await expect(page.getByRole('button', { name: '重试' })).toBeVisible()

  // 点重试必须真的把面板救回来（而不是把错误清掉、面板停在"加载中…"）
  await page.getByRole('button', { name: '重试' }).click()
  await expect(page.getByText('模型:演示脚本')).toBeVisible()
  await expect(page.getByText('加载失败', { exact: false })).toHaveCount(0)
})
