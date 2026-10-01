/**
 * e2e 的端口与密钥口径：**单一来源**。
 *
 * 为什么单独一个文件：`playwright.config.ts`（起服务）与 spec（连服务）必须用同一套
 * 端口/密钥。两边各写一份常量，改一处忘另一处就会出现"服务起在 8123、用例去打 8124"
 * 这种只在改配置那天才暴露的假红。放这里两边 import。
 */

/** 无鉴权档（默认档）：测"真浏览器 + 打包产物 + 安全头 + 对话/审批链路"。 */
export const ANON_PORT = 8123

/** 鉴权档：测"登录路径"——没密钥时被 401 挡住、填对密钥后整条链路恢复。 */
export const AUTH_PORT = 8124

/**
 * 鉴权档的访问密钥。**它必须和真实部署的密钥区分开**：这个值会写进命令行/环境变量，
 * 而且只在 127.0.0.1 上存在几秒钟。任何人把它当真凭据用都会立刻失败（后端只认这个值），
 * 所以叫"e2e 专用"而不是"默认密钥"。
 *
 * 另一个约束：**只能 ASCII**。这个值会被前端放进 `Authorization: Bearer <key>` 请求头，
 * 而 HTTP 头的值必须是 Latin-1 可编码的——密钥里放中文，`fetch` 会直接抛
 * "Invalid header value"，用例会红在一个完全看不懂的地方。
 */
export const AUTH_API_KEY = 'e2e-loopback-key-local-only'

export const ANON_BASE = `http://127.0.0.1:${ANON_PORT}`
export const AUTH_BASE = `http://127.0.0.1:${AUTH_PORT}`

/**
 * 多副本档（`multi-replica.spec.ts`）：**两组副本配一个对照组**，每组两个副本。
 *
 * 这三组副本**不由 Playwright 的 `webServer` 起**（它们要共享一个"每次运行新建的空库"，
 * 见 `replicas.ts` 顶部注释），但端口与密钥仍然登记在这里——单一来源不是为了
 * "配置好看"，而是为了让"服务起在哪个端口"与"用例打哪个端口"永远只有一处定义。
 */

/** 共享组：`WARDEN_SHARED_STATE=1`，协调状态（幂等/事件/限流/锁）都在同一个库里。 */
export const SHARED_REPLICA_PORTS = [8125, 8126]

/**
 * 对照组：同一个库，但**关掉** `WARDEN_SHARED_STATE`。
 *
 * 有它才有结论：只有共享组时，"跨副本读得到"与"跨副本读不到"的差别无法归因到
 * 这个开关上（也可能是别的机制在起作用）；有了同库对照，变量就只剩这一个。
 */
export const LOCAL_REPLICA_PORTS = [8127, 8128]

/** 限流组：共享状态 + 一个很小的额度，用来验"额度是全局一份，不是每副本一份"。 */
export const RATE_REPLICA_PORTS = [8129, 8130]

/**
 * 限流档的额度：`次数/窗口秒数`。
 *
 * 为什么是 3 而不是 1：这一组要**先发一条消息**才能拿到一个有意义的 run_id
 * （而发消息本身也走限流），所以 3 = 发消息 1 次 + 查询 2 次。
 * 这个数字必须写在这里而不是散在断言里——它是用例的"前置条件"，改了口径要一起改。
 */
export const REPLICA_RATE_LIMIT = '3/60'

/** 副本的 base URL（端口来自上表，不允调用方另写一个端口常量）。 */
export const replicaBase = (port: number): string => `http://127.0.0.1:${port}`
