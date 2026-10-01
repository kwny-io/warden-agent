import { expect, test, type Page } from '@playwright/test'

/**
 * 真浏览器里验证 CSP：这条用例补的是 `web/security_headers.py` 里写明的那个缺口——
 * "响应头存在"已经有用例断言了，但"**浏览器加载真产物时 CSP 没挡到自己**"从没验过。
 *
 * 这里刻意断言"零违规"而不是"几类资源能加载"：CSP 的失败模式是**静默降级**——
 * 被挡的东西不会报错，只是不生效（样式丢了、SSE 连不上、字体回退）。
 * 所以要把 `securitypolicyviolation` 与"console 里 Refused to…"当作出错信号来收。
 */

type ViolationList = { __cspViolations?: string[] }

/** 装监听器：任何一次策略拦截都记到 window 上（安装点在页面脚本之前）。 */
async function trackCspViolations(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const w = window as unknown as ViolationList
    w.__cspViolations = []
    document.addEventListener('securitypolicyviolation', (e) => {
      w.__cspViolations?.push(`${e.violatedDirective} ← ${e.blockedURI || '(inline)'}`)
    })
  })
}

async function readCspViolations(page: Page): Promise<string[]> {
  return page.evaluate(() => (window as unknown as ViolationList).__cspViolations ?? [])
}

test('响应头确实带上了 CSP 与其余安全头（先确认真后端在跑）', async ({ request }) => {
  const res = await request.get('/')
  expect(res.status()).toBe(200)

  const headers = res.headers()
  const csp = headers['content-security-policy']
  expect(csp, '缺 CSP：这套策略是防"模型输出成 XSS 后偷 localStorage 密钥"的第一道').toBeTruthy()
  expect(csp).toContain("default-src 'self'")
  expect(csp).toContain("frame-ancestors 'none'")
  // 脚本面必须保持"无内联、无 eval"：这是整套里真正值钱的一条。
  // 如果为了"让某个第三方脚本能跑"而放宽它，请在安全评审里显式说明，别静默改掉。
  expect(csp).not.toMatch(/script-src[^;]*unsafe-(inline|eval)/)
  expect(headers['x-frame-options']).toBe('DENY')
  expect(headers['x-content-type-options']).toBe('nosniff')

  // 服务的是**打包产物**而不是内置回退页：否则这套 e2e 等于没测到 React 控制台。
  const html = await res.text()
  expect(html).toContain('/assets/')
})

test('真浏览器加载 + 交互：一次安全策略拦截都没有', async ({ page }) => {
  const cspConsoleErrors: string[] = []
  page.on('console', (msg) => {
    // 只收 CSP 相关：无鉴权模式下 API 可能 401，那不是本用例要管的事（会引入假红）
    if (msg.type() === 'error' && /Content Security Policy|Refused to/i.test(msg.text())) {
      cspConsoleErrors.push(msg.text())
    }
  })
  page.on('pageerror', (err) => cspConsoleErrors.push(`pageerror: ${err.message}`))

  await trackCspViolations(page)
  await page.goto('/', { waitUntil: 'load' })

  // SPA 真的挂上了。脚本被 CSP 挡住 / 产物引用了不存在的资源，都会在这里失败。
  await expect(page.getByRole('heading', { name: 'Warden Agent' })).toBeVisible()

  // 走一次真实交互：输入 → 发送 → 用户消息落地。
  // 这一条同时验证了 React 事件链路与 SSE（若连接被 CSP 挡掉，消息不会回来）。
  const composer = page.getByLabel('给 Warden Agent 发消息')
  await composer.fill('e2e-csp-探针：请随便回一句')
  await page.getByRole('button', { name: '发送' }).click()
  await expect(page.getByText('e2e-csp-探针', { exact: false }).first()).toBeVisible({
    timeout: 15_000,
  })
  // 给流式回包留出时间：违规往往在"连上 SSE/收到增量"那一步才发生
  await page.waitForTimeout(1500)

  const violations = await readCspViolations(page)
  expect(
    violations,
    'CSP 拦了页面自己的东西：要么产物里有内联/动态执行，要么策略该收敛口径——两边选一个修，别只放宽策略',
  ).toEqual([])
  expect(cspConsoleErrors, '浏览器自己报了策略违规（控制台 Refused to…）').toEqual([])
})
