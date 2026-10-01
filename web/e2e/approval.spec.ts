import { expect, test, type Page } from '@playwright/test'

/**
 * 真浏览器里跑一遍**审批闸门**（这条链路以前只有 pytest 覆盖到 HTTP 层）。
 *
 * 为什么值得单独一套 e2e：审批是"高危动作要过第二个人"这道门，而它的**可用性**和
 * **不可绕过性**都只有把真浏览器接上真后端才发现得了：
 *   · 待审批会话要能被侧栏轮询到（`ApprovalPanel` 每 2s 打一次 `/approvals`），
 *     轮询断了/渲染错了，人根本看不到"有待审批"，于是任务永远挂着；
 *   · 点"批准"之后这一轮要真的接着跑完（不是只把队列清掉）；
 *   · 点"拒绝"之后**工具绝不能执行** —— 这是安全语义，不是体验问题。
 *
 * 触发方式：消息里点名 `git.apply_patch`。目录里的假模型有一条写死的规则——
 * "用户消息里出现哪个工具名，就假装要调用它"，而生产策略是
 * `ask_when_tool_not_pure`（没声明 `pure=True` 的工具一律要人工批准），
 * 于是这条链路是**确定**的，不需要真模型、不需要网络。
 *
 * 为什么用 `git.apply_patch` 而不是别的：它 `pure=False`（会被门禁拦），
 * 而假模型猜出来的参数是垃圾（`diff: "示例值"`），落到一次性临时仓库上只会解析失败——
 * **不会真的改动任何东西**（见 `e2e/start-e2e-server.mjs` 里造临时仓库的注释）。
 */

const TOOL = 'git.apply_patch'
const TRIGGER = `请用 ${TOOL} 给我改点东西`

/**
 * run_id 带时间戳后缀：e2e 必须**可重复跑**。
 * 固定 run_id 的话，上一次跑剩下的"同 run 还挂着待审批/租约"会波及这一次，
 * 红在一个跟本次改动毫无关系的地方（这个问题已经不是假设了，真撞到过）。
 */
const SALT = Date.now().toString(36)
const APPROVE_RUN = `run-e2e-approve-${SALT}`
const REJECT_RUN = `run-e2e-reject-${SALT}`

/**
 * 「**待审批**里这个 run 的那一行」。
 *
 * 为什么必须用「有批准按钮」当限定：`ApprovalPanel` 里**待审批**和**最近决策**
 * （已批准/已拒绝）都渲染成 `li`，而且都带 run_id。只用 `li` + run_id 去断言
 * “队列里没了”时，历史那条会在某个渲染帧里被当成“还在队列里”——于是这句断言
 * 是否通过取决于**队列轮询（2s）与历史轮询（5s）谁先落到界面上**。
 * 这不是理论问题：本轮把 e2e 从 7 条扩到 10 条后，它在全量跑里真的红了
 * （单跑却绿）——典型的“靠时序通过”。用按钮限定后，“待审批”与“历史”语义分行。
 */
function pendingRow(page: Page, runId: string) {
  return page.locator('li').filter({ hasText: runId }).filter({
    has: page.getByRole('button', { name: '批准' }),
  })
}

/** 「最近决策」里这个 run 的那一行（历史列表最多显示 5 条，所以不能用行数做断言）。 */
function historyRow(page: Page, runId: string) {
  return page.locator('li').filter({ hasText: runId }).filter({ hasText: '已批准' })
}

async function openRun(page: Page, runId: string): Promise<void> {
  await page.goto(`/?run=${runId}`, { waitUntil: 'load' })
  await expect(page.getByRole('heading', { name: 'Warden Agent' })).toBeVisible()
}

/** 发一条会触发审批的消息，并等到"需要审批"落到聊天区 + 审批队列出现这一单。 */
async function triggerApproval(page: Page, runId: string): Promise<void> {
  await openRun(page, runId)
  await page.getByLabel('给 Warden Agent 发消息').fill(TRIGGER)
  await page.getByRole('button', { name: '发送' }).click()

  // 1) 聊天区出现"需要审批"（ApprovalPanel 是 2s 轮询，给足时间）
  await expect(page.getByText('需要审批', { exact: false }).first()).toBeVisible({
    timeout: 20_000,
  })
  // 2) 审批队列里出现这一单，且点名了是哪个 run
  const queueItem = pendingRow(page, runId)
  await expect(queueItem).toBeVisible({ timeout: 20_000 })
  await expect(queueItem).toContainText(TOOL)
}

test('批准：队列里的待审批能把这一轮接着跑完（真浏览器点按钮，不是打 HTTP）', async ({
  page,
  request,
}) => {
  await triggerApproval(page, APPROVE_RUN)

  // 服务端确实挂起了这一轮
  const pending = await request.get('/approvals')
  expect(pending.status()).toBe(200)
  const pendingBody = (await pending.json()) as Array<{ run_id: string; tool_name: string }>
  expect(pendingBody.some((a) => a.run_id === APPROVE_RUN && a.tool_name === TOOL)).toBe(true)

  const queueItem = pendingRow(page, APPROVE_RUN)
  await queueItem.getByRole('button', { name: '批准' }).click()

  // 1) 这一单从**待审批**里消失（不是“页面上再也找不到这个 run_id”——历史里还会有）
  await expect(queueItem).toHaveCount(0, { timeout: 20_000 })
  // 2) 决策历史里留下"已批准"
  const decidedRow = historyRow(page, APPROVE_RUN)
  await expect(decidedRow.first()).toContainText('已批准', { timeout: 20_000 })
  // 3) 这一轮**真的跑完了**：假模型看到工具结果后给出结论
  await expect(page.getByText('已读到上面的工具结果', { exact: false }).first()).toBeVisible({
    timeout: 20_000,
  })

  // 4) 服务端侧对账：队列清空、历史记成 approved
  const after = (await (await request.get('/approvals')).json()) as Array<{ run_id: string }>
  expect(after.some((a) => a.run_id === APPROVE_RUN)).toBe(false)
  const history = (await (await request.get('/approvals/history')).json()) as Array<{
    run_id: string
    decision: string
  }>
  expect(history.find((h) => h.run_id === APPROVE_RUN)?.decision).toBe('approved')

  // 5) 工具的输出要**被人看到**。
  //
  // 这是这一条用例最值钱的断言：以前工具真的跑了、真的有输出，但界面上只有一行
  // “🔧 调用工具：git.apply_patch”——用户既看不到工具输出了什么，也不知道成没成。
  // 所以这里不看“有没有某个标题”，而是把服务端落库的那条 tool 消息读出来，
  // 再断言浏览器里能看到**同一段文字**（胜利条件是“内容上屏”，不是“标题上屏”）。
  const rows = (await (await request.get(`/messages/${APPROVE_RUN}`)).json()) as Array<{
    role: string
    content: string | null
  }>
  const toolRow = rows.filter((m) => m.role === 'tool' && m.content !== null).at(-1)
  expect(toolRow, '落库里应该有工具结果（否则模型也不可能“读到上面的工具结果”）').toBeTruthy()
  const storedText = toolRow?.content ?? ''
  // 成败都要一眼看得出来（成败判定是 display.ts 的契约：只认后端拼的失败前缀）
  const kind = storedText.includes('执行失败') ? '工具执行失败' : '工具结果'
  await expect(page.getByText(kind).first()).toBeVisible({ timeout: 20_000 })
  const firstLine = storedText.split('\n')[0].trim()
  await expect(page.getByText(firstLine, { exact: false }).first()).toBeVisible()
})

test('拒绝：工具**绝不执行**，只留一条拒绝记录（安全语义）', async ({ page, request }) => {
  await triggerApproval(page, REJECT_RUN)
  const queueItem = pendingRow(page, REJECT_RUN)
  await queueItem.getByRole('button', { name: '拒绝' }).click()

  await expect(queueItem).toHaveCount(0, { timeout: 20_000 })
  await expect(
    page.locator('li').filter({ hasText: REJECT_RUN }).filter({ hasText: '已拒绝' }).first(),
  ).toContainText('已拒绝', { timeout: 20_000 })

  // 关键断言：被拒绝的工具**没有执行**——会话里给模型的是一条"用户拒绝"的说明。
  // 这一条如果坏了，就是"审批门禁可绕过"，比 UI 挂掉严重得多。
  const messages = await request.get(`/messages/${REJECT_RUN}`)
  expect(messages.status()).toBe(200)
  const rows = (await messages.json()) as Array<{ role: string; content: string | null }>
  const toolRows = rows.filter((m) => m.role === 'tool')
  expect(toolRows.length).toBeGreaterThan(0)
  expect(toolRows.some((m) => (m.content ?? '').includes(`用户拒绝执行 ${TOOL}`))).toBe(true)
  // 拒绝也不能变成"放过"：工具正常执行的成功文案不该出现
  expect(toolRows.some((m) => (m.content ?? '').includes('已应用'))).toBe(false)

  const history = (await (await request.get('/approvals/history')).json()) as Array<{
    run_id: string
    decision: string
  }>
  expect(history.find((h) => h.run_id === REJECT_RUN)?.decision).toBe('rejected')
})
