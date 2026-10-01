import { expect, test } from '@playwright/test'

import { AUTH_API_KEY, AUTH_BASE } from './endpoints'

/**
 * 真浏览器里跑一遍**登录路径**（鉴权档，见 `playwright.config.ts` 里 `e2e-auth` 那个实例）。
 *
 * 这条链路以前完全空白：pytest 覆盖的是"带上正确的 Bearer 头就能过"，
 * 但**"密钥从哪来、存在哪、刷新后还在不在"**这三件事全在前端，只有真浏览器能证明：
 *   · 没密钥时页面**不白屏**（要能看出"被 401 挡住了"，而不是一个空壳）；
 *   · 在界面上填一次密钥之后，整条链路（含 SSE 流）真的恢复；
 *   · `记住密钥` 勾与不勾，落点分别是 local 与 session 存储，刷新后的行为不一样。
 *
 * 最后一件事不是细节：不勾选时密钥只进 sessionStorage（关掉标签页即失效），
 * 这是安全默认值；搞反了就等于"悄悄把长期密钥落盘"。
 */

// 本文件所有用例都打鉴权档（默认配置打的是无鉴权档）
test.use({ baseURL: AUTH_BASE })

/** 前端存密钥的键名，与 `src/lib/api.ts` 里的 API_KEY_STORAGE 保持一致。 */
const KEY = 'warden.apiKey'

test('没密钥：后端逐条 401，页面不白屏，并把原因原样显示出来', async ({ page, request }) => {
  // 后端契约先钉死：状态类接口在没密钥时是 401，且 detail 说清楚缺什么
  const status = await request.get(`${AUTH_BASE}/status`)
  expect(status.status()).toBe(401)
  expect(JSON.stringify(await status.json())).toContain('缺少或格式错误的')

  // 连模型流也该被挡住（不是"只有读接口要鉴权"）
  const stream = await request.post(`${AUTH_BASE}/chat/stream/run-e2e-unauth`, {
    data: { text: 'hi' },
  })
  expect(stream.status()).toBe(401)

  await page.goto('/', { waitUntil: 'load' })
  // 页面本身是公开的（否则你连填密钥的地方都没有）
  await expect(page.getByRole('heading', { name: 'Warden Agent' })).toBeVisible()
  await expect(page.getByLabel('访问密钥（API Key）')).toBeVisible()
  // 但数据面板要如实报出 401，而不是假装"暂无数据"
  await expect(page.getByText(/状态获取失败/).first()).toBeVisible({ timeout: 20_000 })
  await expect(page.getByText(/401/).first()).toBeVisible({ timeout: 20_000 })
})

test('填对密钥：轮询错误消失，SSE 流也真的带上了密钥（不是只有读接口能过）', async ({
  page,
}) => {
  await page.goto('/', { waitUntil: 'load' })
  // 先制造"确实被挡住"的状态，后面才谈得上"恢复"（否则断言可能落在从未出现的文本上）
  await expect(page.getByText(/状态获取失败/).first()).toBeVisible({ timeout: 20_000 })

  await page.getByLabel('访问密钥（API Key）').fill(AUTH_API_KEY)
  await page.keyboard.press('Enter')

  // 状态面板是 3s 轮询：错误横幅该自己消失
  await expect(page.getByText(/状态获取失败/)).toHaveCount(0, { timeout: 20_000 })

  // 发一条消息，盯住那个请求本身：200 才证明 Bearer 真的加到了流式 POST 上
  const [response] = await Promise.all([
    page.waitForResponse(
      (r) => r.url().includes('/chat/stream/') && r.request().method() === 'POST',
    ),
    (async () => {
      await page.getByLabel('给 Warden Agent 发消息').fill('你好')
      await page.getByRole('button', { name: '发送' }).click()
    })(),
  ])
  expect(response.status()).toBe(200)
})

test('密钥存储：不勾"记住"只进 sessionStorage，勾了才进 localStorage', async ({ page }) => {
  await page.goto('/', { waitUntil: 'load' })

  // 默认不勾：只存 session（关掉标签页就失效）
  await page.getByLabel('访问密钥（API Key）').fill(AUTH_API_KEY)
  await page.keyboard.press('Enter')
  const plain = await page.evaluate(
    (k) => ({
      local: window.localStorage.getItem(k),
      session: window.sessionStorage.getItem(k),
    }),
    KEY,
  )
  expect(plain.local).toBeNull()
  expect(plain.session).toBe(AUTH_API_KEY)

  // 刷新后仍然可用（sessionStorage 活得比刷新长）
  await page.reload({ waitUntil: 'load' })
  await expect(page.getByText(/状态获取失败/)).toHaveCount(0, { timeout: 20_000 })
  await expect(page.getByLabel('访问密钥（API Key）')).toHaveValue(AUTH_API_KEY)

  // 勾上"记住密钥"再填一次：这次落 local
  await page.getByLabel('记住密钥').check()
  await page.getByLabel('访问密钥（API Key）').fill(`${AUTH_API_KEY}-2`)
  await page.keyboard.press('Enter')
  const remembered = await page.evaluate(
    (k) => ({
      local: window.localStorage.getItem(k),
      session: window.sessionStorage.getItem(k),
    }),
    KEY,
  )
  expect(remembered.local).toBe(`${AUTH_API_KEY}-2`)
  expect(remembered.session).toBeNull()
})
