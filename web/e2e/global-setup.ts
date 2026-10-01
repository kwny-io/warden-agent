import { spawnSync } from 'node:child_process'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

/**
 * 全局准备：**构建一次**前端产物，供两个后端实例（无鉴权档 / 鉴权档）共用。
 *
 * 为什么提到这里来：`webServer` 两个条目都是 `node e2e/start-e2e-server.mjs`，
 * 而那个脚本原本自己跑 `npm run build`。两条 webServer 是**并发启动**的，
 * 于是会有两个 `vite build` 同时往 `web/dist` 里写——产物可能被两份构建交替覆盖，
 * 得到一个"谁的 chunk 都不完整"的目录。这种失败还有一个更坏的形态：**不报错**，
 * 只是页面偶发加载不到资源，然后被当成"CSP 有问题"去查。
 *
 * 构建放在这里就等于"一次构建、两次复用"，两个后端只是各自挂载同一份 `web/dist`。
 * 单独手动跑 `node e2e/start-e2e-server.mjs` 时它自己仍会构建（见那个脚本里的判断），
 * 所以这个脚本不自相矛盾：谁负责构建由 `WARDEN_E2E_PREBUILT` 说清楚。
 */
export default function globalSetup(): void {
  const webDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
  const build = spawnSync('npm run build', { cwd: webDir, stdio: 'inherit', shell: true })
  if (build.error) {
    throw new Error(`[e2e] 起不了 npm：${build.error.message}`)
  }
  if (build.status !== 0) {
    throw new Error(`[e2e] 前端构建失败（exit=${build.status}）——产物不对的话后面全是假红`)
  }
  console.log('[e2e] 前端产物已构建（两个后端实例共用这一份）')
}
