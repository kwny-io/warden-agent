/**
 * e2e 用的"起服务"脚本：起一个真后端（挂载前端产物、打安全头）。
 *
 * 同一份脚本被**两个** webServer 条目复用，靠环境变量区分档位：
 *   · 无鉴权档（默认，`WARDEN_E2E_API_KEY` 未设）：`WARDEN_ALLOW_ANON=1`，测对话/审批链路；
 *   · 鉴权档（设了 `WARDEN_E2E_API_KEY`）：只认 `Authorization: Bearer <key>`，测登录路径。
 *
 * **为什么不直接写 `npm run build && bash ../scripts/run_local.sh`**：
 * Playwright 在 Windows 上用 `cmd.exe` 执行 `webServer.command`，而 cmd 的 PATH 里未必有
 * Git Bash 的 bash（这台机器实测没有）。那种写法只在类 Unix 上能用，于是 e2e 会变成
 * "CI 绿、本地红"——恰恰是最没人愿意查的一类失败。用 node 起（node 一定在 PATH 上，
 * 否则 npm 自己都跑不起来）两边走的是同一条路径。
 *
 * 产物构建默认由 `e2e/global-setup.ts` 统一做一次（`WARDEN_E2E_PREBUILT=1` 时本脚本跳过），
 * 因为两个后端并发启动、各自 `vite build` 会同时写 `web/dist`。手动单独跑本脚本时
 * （不给这个变量）它仍然自己构建，不会出现"忘了构建就起服务"的空跑。
 *
 * 进程留在前台：Playwright 会等在 `PORT`/`/health/live` 上，用例结束再收掉它。
 */
import { spawn, spawnSync } from 'node:child_process'
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url)) // web/e2e
const webDir = path.resolve(here, '..') // web
const repoRoot = path.resolve(webDir, '..') // 仓库根

if (!existsSync(path.join(webDir, 'node_modules'))) {
  console.error(`[e2e] 缺少 web/node_modules，先在 ${webDir} 跑 npm ci（或 npm install）`)
  process.exit(1)
}

// 跨平台找 venv 解释器：Windows 在 Scripts/，其余在 bin/
const candidates = [
  path.join(repoRoot, '.venv', 'Scripts', 'python.exe'),
  path.join(repoRoot, '.venv', 'bin', 'python'),
]
const python = candidates.find((p) => existsSync(p))
if (!python) {
  console.error(`[e2e] 找不到虚拟环境解释器，找过：\n  ${candidates.join('\n  ')}`)
  console.error('[e2e] 这个项目假定依赖装在仓库根的 .venv 里（见 README 的安装步骤）')
  process.exit(1)
}

if (process.env.WARDEN_E2E_PREBUILT === '1') {
  console.log('[e2e] 跳过构建：产物已由 global-setup 构建（两个后端实例共用同一份 web/dist）')
} else {
  // 走 `npm run build` 而不是直呼 vite：产出方式（是否先 tsc、是否分 chunk）应该只有一处定义，
  // 免得以后改了 build 脚本、e2e 却还在跑旧管线。
  // Windows 上 npm 是 npm.cmd，Node 20+ 对 .cmd 不再允许不带 shell 直接 spawn（EINVAL），
  // 所以这里用 shell 跑一个**常量命令串**（没有插值，不存在注入面，也不会报 DEP0190）。
  const build = spawnSync('npm run build', { cwd: webDir, stdio: 'inherit', shell: true })
  if (build.error) {
    console.error(`[e2e] 起不了 npm：${build.error.message}`)
    process.exit(1)
  }
  if (build.status !== 0) {
    console.error(`[e2e] 前端构建失败（exit=${build.status}）`)
    process.exit(build.status ?? 1)
  }
}

const port = process.env.PORT ?? '8123'
// WARDEN_HOST=0.0.0.0 且没开鉴权时后端会**拒绝启动**（对外监听必须先声明白名单/密钥），
// 所以这里固定回环地址：e2e 只在本机打自己的后端。
const host = process.env.WARDEN_HOST ?? '127.0.0.1'

/**
 * 一次性 git 仓库：给 `git.apply_patch` 当落点。
 *
 * 审批用例需要"一个**未声明 pure=True** 的工具"来触发真实策略（`ask_when_tool_not_pure`），
 * 而 `git.apply_patch` 恰好就是这种（写仓库，`pure=False`）。它在目录里存在的前提是
 * `GIT_WORKDIR` 指向一个 git 仓库——**不能指本仓库**：那是"拿写工具对着自己的源码"，
 * 用例一旦出意外就是改动被测对象。所以每次运行新建一个临时仓库，用完删掉。
 *
 * 找不到 git 就返回 null（不假装成功）：调用方会明确说"审批用例将不可用"，
 * 而不是让用例在后面以"审批队列迟迟不出现"的形式含糊地失败。
 */
function makeThrowawayRepo() {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'warden-e2e-git-'))
  const git = (args) => spawnSync('git', args, { cwd: dir, stdio: 'pipe' })
  const init = git(['init', '-q'])
  if (init.error || init.status !== 0) return null
  writeFileSync(path.join(dir, 'README.md'), '# e2e 用例仓库（每次运行新建，用完即删）\n')
  if (git(['add', '.']).status !== 0) return null
  const commit = git([
    '-c', 'user.email=e2e@example.com', '-c', 'user.name=e2e', 'commit', '-qm', 'init',
  ])
  if (commit.status !== 0) return null
  return dir
}

const gitDir = makeThrowawayRepo()
if (!gitDir) {
  console.error(
    '[e2e] 造不出一次性 git 仓库（git 不可用？）→ git.apply_patch 不会注册，审批用例会跳过',
  )
}

const authKey = process.env.WARDEN_E2E_API_KEY ?? ''
// 状态目录：每次跑 e2e 都用一个**全新临时目录**。
//
// 为什么必须这么做（这是踩出来的）：不指定时后端用默认 `warden-agent-local.db`，
// 也就是**开发时你自己用的那个库** —— 后果有三个，一个比一个难受：
//   1. e2e 会把测试会话写进开发库（左边会话列表里混进一堆 `run-e2e-*`）；
//   2. 上一次失败的 e2e 会在库里留下**悬而未决的审批**，下一次跑的时候审批队列
//      里已经先有几单别人的；用固定 run_id 的用例还会撞上"同 run 上一轮
//      还挂着审批/租约"的状态，于是新发起的请求拿不到预期的响应，红在一个
//      完全看不懂的地方（本轮就这么红过两次）；
//   3. 用例之间开始隐式耦合：不确定是“代码变了”还是“脏数据”。
//
// 注意 GIT_WORKDIR 已经在用临时目录了（同一个理由），这里是把它贯彻到存储层。
const stateDir = mkdtempSync(path.join(os.tmpdir(), 'warden-e2e-state-'))
// 子进程环境：先**摘掉宿主 shell 里所有 `WARDEN_*`**，再显式决定姿态。
//
// 为什么必须摘（真缺陷，可复现）：这里原来是 `...process.env`，于是
//     WARDEN_SSE_HEARTBEAT_SECONDS=999 node e2e/start-e2e-server.mjs
// 会让"确定性档"的后端以 999 秒空闲心跳启动（启动日志里能看到，实测过）——
// 于是 e2e 断言的东西变成了"跑之前 shell 里恰好 export 了什么"，
// 红在一个和被测代码毫无关系的地方；更糟的是它把开发者的真凭据也一并递进子进程，
// 只靠下面"显式给空串"那一行兜住（那行一旦被改，就是悄悄打真模型）。
// 只摘 `WARDEN_*`：PATH/HOME/TEMP 等必须留（要能找到 python、要能跑 git）。
// harness 自己的两个入参（`WARDEN_E2E_API_KEY`、`WARDEN_ALLOW_ANON`）
// 仍在上面/下面显式读取后**当成档位**设置——入参是显式的，不是环境里碰巧有的。
const scrubbedEnv = Object.fromEntries(
  Object.entries(process.env).filter(([key]) => !key.startsWith('WARDEN_')),
)
const childEnv = {
  ...scrubbedEnv,
  PORT: port,
  WARDEN_HOST: host,
  WARDEN_DB_PATH: path.join(stateDir, 'e2e.db'),
  // 强制离线假模型：仓库里的密钥文件会被后端自己在启动时读进环境（`load_env()`），
  // 那样 e2e 就会**悄悄打真模型**——非确定、要联网、还花钱，而断言不会红。
  // 这里显式给空串：`load_env()` 不覆盖已存在的键，`DEEPSEEK_API_KEY=""` 于是被当成"没配"。
  DEEPSEEK_API_KEY: '',
  // 并且**根本不读 .env**：`.env` 是另一条进环境的路（只在本机存在、在 gitignore 里），
  // 不关掉的话 e2e 的档位仍会随"这台机器的 .env 恰好写了什么"变——
  // 和上面摘掉宿主 `WARDEN_*` 是同一个失败模式的两个入口。
  WARDEN_IGNORE_DOTENV: '1',
}
if (gitDir) childEnv.GIT_WORKDIR = gitDir

if (authKey) {
  // 鉴权档：只认这个 key。**必须删掉 anon 开关**，否则"鉴权档"其实是无鉴权档，
  // 登录用例会在"本该 401"的地方拿到 200，然后以断言失败的形式骗人。
  childEnv.WARDEN_API_KEY = authKey
  delete childEnv.WARDEN_ALLOW_ANON
  console.log(`[e2e] 档位=鉴权：PORT=${port} WARDEN_HOST=${host}（业务接口需 Bearer）`)
} else {
  // WARDEN_ALLOW_ANON=1：后端默认 fail-closed（没配 WARDEN_API_KEY 时直接 exit 2，
  // 哪怕只听回环），而**这套用例测的不是鉴权**——鉴权自己的用例在 pytest 与 auth.spec.ts 里。
  childEnv.WARDEN_ALLOW_ANON = process.env.WARDEN_ALLOW_ANON ?? '1'
  delete childEnv.WARDEN_API_KEY
  console.log(`[e2e] 档位=无鉴权：PORT=${port} WARDEN_HOST=${host}`)
}
console.log(`[e2e] 模型=离线假模型（确定性）｜GIT_WORKDIR=${gitDir ?? '(无)'}｜产物目录 web/dist`)
console.log(`[e2e] 状态目录=${stateDir}（临时，与开发库 warden-agent-local.db 无关）`)

const server = spawn(python, ['-m', 'warden_agent.web.run_server'], {
  cwd: repoRoot,
  stdio: 'inherit',
  env: childEnv,
})

/** 收尾：临时 git 仓库与状态目录只有在本后端退出后才删（用例期间工具可能还要读）。 */
function cleanup() {
  for (const dir of [gitDir, stateDir]) {
    if (!dir) continue
    try {
      rmSync(dir, { recursive: true, force: true })
    } catch {
      /* 删不掉不影响结论：它在系统临时目录里（前缀 warden-e2e-*），且下次会用新目录 */
    }
  }
}

// 信号透传：否则 Playwright 收尾时后端会变成孤儿进程（Windows 上尤其明显），
// 下次跑就变成"端口被占"的假失败。
for (const sig of ['SIGINT', 'SIGTERM']) {
  process.on(sig, () => server.kill(sig))
}
process.on('exit', cleanup)
server.on('exit', (code, signal) => {
  console.log(`[e2e] 后端退出：code=${code} signal=${signal}`)
  cleanup()
  process.exit(code ?? 0)
})
