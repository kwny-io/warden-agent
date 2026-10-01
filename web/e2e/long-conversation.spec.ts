import { expect, test, type APIRequestContext, type Locator, type Page } from '@playwright/test'

import { ANON_BASE } from './endpoints'

/**
 * 真浏览器里跑一遍**长会话**（几十条消息之后，聊天区还对不对）。
 *
 * 为什么值得一套 e2e：这类问题在短会话里**永远看不见**——用一两条消息手工点过一遍，
 * 页面"看着挺好"，上线后用户聊到三十轮才发现：
 *   · 新回复落在视口**外面**，页面看起来像"没反应"（人会以为模型挂了，然后重发）；
 *   · 或者每次刷新都从顶部开始，"我刚才说到哪了"全靠手动滚；
 *   · 或者只渲染最后几条（历史被静默截断），用户以为记录丢了。
 *
 * 断言口径不看"有没有渲染注释"，而是看**滚动位置**与**是否落在可视框内**：
 * 用户能看到的唯一判据是"最新那条在我的屏幕上"，不是 DOM 里存在。
 *
 * 播种方式：直接打 `POST /chat/stream/{run_id}`（假模型是确定且瞬间的），
 * 复现"聊了很多轮"的状态；比在浏览器里真发 12 条**稳得多**（不依赖输入框焦点/时序），
 * 而这一批要验的是**渲染与滚动**，不是发送链路（发送链路在 chat.spec.ts 里）。
 */

const SALT = Date.now().toString(36)
const SCROLL_RUN = `run-e2e-scroll-${SALT}`
const TURNS = 12

const firstText = (i: number): string => `第 ${i} 轮：${'长会话滚动验证内容。'.repeat(8)}`
const FIRST_TEXT = firstText(1)
const LAST_SEEDED_TEXT = firstText(TURNS)
const NEW_TEXT = `第 ${TURNS + 1} 轮：${'这一条必须自动滚进视口。'.repeat(8)}`

/** 播种：连续 N 轮对话，每轮用户消息都够长（保证内容超出视口高度）。 */
async function seedTurns(
  request: APIRequestContext,
  runId: string,
  turns: number,
): Promise<void> {
  for (let i = 1; i <= turns; i += 1) {
    const res = await request.post(`${ANON_BASE}/chat/stream/${runId}`, {
      data: { text: firstText(i) },
    })
    expect(res.ok(), `第 ${i} 轮播种失败：HTTP ${res.status()} ${await res.text()}`).toBeTruthy()
  }
}

/** 消息区（全前端只有这一处 `aria-live`，语义就是"这里是消息流"）。 */
function messageBox(page: Page): Locator {
  return page.locator('[aria-live="polite"]')
}

/** 滚动条是否停在最底部（留 4px 容差：亚像素与滚动条宽度会带来 1~2px 误差）。 */
async function isPinnedToBottom(box: Locator): Promise<boolean> {
  return box.evaluate(
    (el) => el.scrollTop + el.clientHeight >= el.scrollHeight - 4,
  )
}

/**
 * 元素是否**完整落在消息区的可视框内**。
 *
 * 为什么不用 `toBeInViewport()`：它比的是"元素矩形 ∩ 浏览器视口"，而一个被
 * `overflow-y: auto` 容器滚出去的元素，矩形可能**仍然落在浏览器视口里**
 * （容器在页面中部时），于是断言会假绿——那正是这套用例要抓的 bug。
 */
async function isFullyInside(box: Locator, target: Locator): Promise<boolean> {
  const boxRect = await box.boundingBox()
  const targetRect = await target.boundingBox()
  if (!boxRect || !targetRect) return false
  return (
    targetRect.y >= boxRect.y - 1 &&
    targetRect.y + targetRect.height <= boxRect.y + boxRect.height + 1
  )
}

test('长会话：历史全量加载、打开就钉在最新一条，且新回复一定滚进视口', async ({
  page,
  request,
}) => {
  await seedTurns(request, SCROLL_RUN, TURNS)

  await page.goto(`/?run=${SCROLL_RUN}`, { waitUntil: 'load' })
  await expect(page.getByRole('heading', { name: 'Warden Agent' })).toBeVisible()

  const box = messageBox(page)

  // ── 1) 历史是全量的（不是只显示最后几条）────────────────────────────
  // 最早那条在 DOM 里：就算它在视口外，"记录还在"也必须成立
  await expect(box.getByText(FIRST_TEXT, { exact: false })).toHaveCount(1)

  // ── 2) 打开就停在最新一条，而不是顶部 ───────────────────────────────
  await expect.poll(() => isPinnedToBottom(box), { timeout: 15_000 }).toBe(true)
  await expect
    .poll(
      () => isFullyInside(box, box.getByText(LAST_SEEDED_TEXT, { exact: false }).first()),
      { timeout: 15_000 },
    )
    .toBe(true)

  // ── 3) 关键回归：手动滚到顶之后再发消息，新回复必须回到视口内 ────────
  // 用户真的会往上翻着看历史；此时新回复落在视口外 = 页面看起来"没反应"
  await box.evaluate((el) => {
    el.scrollTop = 0
  })
  await expect.poll(() => isPinnedToBottom(box)).toBe(false)

  await page.getByLabel('给 Warden Agent 发消息').fill(NEW_TEXT)
  await page.getByRole('button', { name: '发送' }).click()

  const newest = box.getByText(NEW_TEXT, { exact: false }).first()
  await expect(newest).toHaveCount(1, { timeout: 20_000 })
  await expect.poll(() => isFullyInside(box, newest), { timeout: 20_000 }).toBe(true)
  await expect.poll(() => isPinnedToBottom(box), { timeout: 20_000 }).toBe(true)

  // ── 4) 这一轮是**完整**收尾的（真有助手回复，不是只留下用户气泡）────
  // 关住一个真空白：用户气泡是本地乐观追加的，即使 SSE 整条链断了也会出现——
  // 那样上面的滚动断言会全绿，而用户等到的是"发出去没反应"。
  // 假模型对"没点名任何工具"的消息有一条写死的回复（见 model/fake.py 规则2），
  // 所以 N 轮播种 + 1 轮真发 = **N+1 条助手气泡**。
  await expect(
    box.getByText('就此结束', { exact: false }),
    `应有 ${TURNS + 1} 条助手回复（播种 ${TURNS} + 刚发的 1）`,
  ).toHaveCount(TURNS + 1, { timeout: 20_000 })
})
