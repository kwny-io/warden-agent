# Changelog

> 本文件按**时间戳**记录每次提交改动的内容,以及**尚未实现**的待办(未实现/路线图)。
> 每次提交都同步更新本文件,保持「git 历史 ↔ 文档」一致。

---

## 2026-09-22（第十二批：交付侧"低难度高价值"五件——漏洞扫描 / 审计导出 / 配置访问器 / 事件上限 / 仪表盘）

> 承接第十一批：把这批"对外交付会被问到、但不需要外部服务就能做"的补上。
> 需要外部环境的那几项（真 KMS 联调、OTLP 后端、Grafana 展示验收、压测结论、目标 K8s）另议。

### 1. 依赖与镜像漏洞扫描进 CI（原先完全没有）

- **依赖扫描**：`pip-audit`（进 dev extra，按 `uv.lock` 固定）+ CI 新增 `security` job，
  只扫**运行依赖**（`--no-dev`，dev 不进交付镜像）。本地实测：运行依赖与 dev 依赖均**无已知漏洞**。
- **镜像扫描**：CI `container` job 新增 **trivy**（`--severity HIGH,CRITICAL --ignore-unfixed
  --exit-code 1`）。`--ignore-unfixed` 的理由：基镜像里常有"尚无修复"的包，把它们也算失败会让
  门禁变噪声、最后被人关掉。
  ⚠️ **过程中发现一个真问题**：用**陈旧缓存的基镜像**扫描会命中 **13 条可修复的 HIGH/CRITICAL**
  （perl / gzip / util-linux 等）；**拉新基镜像后 0 条**。所以给 `container_smoke.sh` 的构建加了
  `--pull`（本机网络受限可 `SMOKE_NO_PULL=1` 跳过）——**门禁有意义的前提是基镜像新鲜**。
- **自动升级**：新增 `.github/dependabot.yml`（uv / npm / docker 基镜像 / github-actions 四类每周查）。
  扫描负责"发现"，dependabot 负责"升级"——否则门禁只会一直红。
- **定时跑**：CI 加 `schedule`（每周一 03:00 UTC）。依赖与基镜像的新 CVE **不需要改代码就会出现**，
  只在 push/PR 上扫描等于"没人提交就没人检查"。

### 2. 审计导出 `warden audit-export`（归档/取证）

- 导出 JSONL/CSV，**带链字段**（`id` / `prev_hash` / `hash`）——只导出内容的话，
  接收方无法独立复核"这份导出被动过没有"；带上之后可以拿同一把 `WARDEN_AUDIT_KEY` 重算整条链。
- `--after-id`（增量）、`--limit`、`--format jsonl|csv`；**导出前顺带校验链**，
  链已断则**退出码 4**（数据仍写出便于取证）——不让一份"被动过的审计"被当成正常导出。
- 写入位置由 `--out-dir` 决定（默认 `./audit-exports/`），文件名经 `_safe_export_name` 校验为
  **纯文件名**（拒 `..`/目录/盘符），所以写入位置不会被文件名带跑。
- 新增 `SqliteAuditStore.export_records()`（两条字面量 SQL + 占位符，不拼接）。

### 3. 配置读取收口：类型化访问器 + 守卫强制

- `core/settings.py` 新增 `env_str / env_opt / env_int / env_bool / env_positive_int`：
  把**解析**收口到一处（此前各模块自己 `int(...)` / 手写布尔判定）。约定仍是"变量名在调用处是字面量"。
- `tests/test_config_surface.py` 的 AST 守卫**扩展识别访问器调用**（`env_int("X", ...)` 也算"读了 X"），
  并加了一条测试锁住这个能力——否则模块改用访问器后守卫就"看不见"了。
- **顺带消掉三处口径漂移**（真实收益）：
  - `WARDEN_ALLOW_ANON` 原先只认 `1/true/yes`（**漏了 `on`**）；
  - `WARDEN_STABILITY` 原先用 `not in off`（**未知取值会被当成"开"**）；
  - `WARDEN_MAX_CONTEXT_CHARS` 原先 `int(raw) if raw.isdigit()`（写错**静默用默认值**）。
  三者现在统一走访问器（未知取值已被启动校验拦住，所以合法配置行为不变）。
- **范围说明（诚实）**：**解析类**读取已全部迁移；仍有约 40 处**纯字符串读取**直接 `.get`
  （无解析、无口径差异），未强制统一——访问器已就位，可按需分批迁移。

### 4. 进程内事件总线：保留上限可配 + 丢弃可观测

- `WARDEN_EVENT_KEEP`（默认 500）配置每个 run 的保留条数；`InProcessEventBus(keep=...)`。
- **丢弃不再静默**：首次丢弃打警告（含累计条数）；消费者 `poll` 发现自己的 `after_seq`
  比保留的最早事件还旧时，给出"事件流存在缺口"的提示；`dropped_count()` 可查。
  此前是"悄悄少几条、消费方无从察觉"。

### 5. Grafana 仪表盘（provisioning，挂载即生效）

- `deploy/observability/grafana/`：数据源 + 仪表盘 provisioning + **"Warden Agent 概览"仪表盘**
  （挂起 Run / 可用性 SLI / 5xx 占比 / 限流速率 / 请求速率 / 延迟 p50-p95-p99 / 错误速率 / 审批决策）。
- 可观测性 compose 加了 `grafana` 服务（演练环境匿名只读，**生产别照搬**）。
- `tests/test_alert_rules.py` 增加一条守卫：**仪表盘 PromQL 引用的指标必须真实存在**——
  指标改名后仪表盘会"静静地变成空图"，这条把它变成红灯。

### 6. `warden_stuck_runs` 抓取加 TTL 缓存

每次抓取都扫库在抓取密集时是浪费；加 15s TTL（与 Prometheus 默认抓取间隔一致）。
**刷新失败时保留上一次的值、绝不写 0**——写 0 等于把告警悄悄消掉。

### 验证

`786 passed / 2 skipped`（起了 PostgreSQL，共 788 项；跳过 = win32 无内核隔离档、当前权限不允许建符号链接；
不配库时 761 passed / 27 skipped），`ruff` 全绿，`mypy --strict` 93 文件零错误；
`pip-audit` 无已知漏洞（运行依赖与 dev 依赖）；trivy 对**拉新基镜像后**的镜像 0 命中。
⚠️ 本地注意：MCP 集成测试用 `npx` 拉包，**冷启动**（首跑、npm 缓存空）会超过客户端超时导致 2 条失败，
   热跑即通过；CI 里没有 `node_modules`、这些测试本就跳过（见 `client_ready`）。

---

## 2026-09-22（第十一批：安全 / 正确性审计后的修复）

> 触发：对全仓做了两轮**独立审计**（正确性与并发 / 安全边界），每条发现都带文件行号与触发路径。
> 本轮修掉 11 条（3 条高危、4 条中危、其余中低），每条都补了回归测试。**审计发现的问题见下方逐条**。

### 安全（可被利用）

- **【严重】`git.apply_patch` 路径穿越 → 任意文件写/删**：`_norm_path` 只剥 `a/`、`b/` 前缀，
  **不拒 `..`/绝对路径**；`PatchApplier.apply` 也没有包含校验。补丁路径来自**不可信输入**
  （模型生成的 diff、网页提示注入诱导的 diff），于是 `--- a/../../x` 能写到工作区之外。
  修法：解析期拒绝 `..`/绝对路径/盘符/反斜杠，应用期再用 `resolve()` 后的包含校验兜底（含符号链接逃逸）。
  回归：`tests/test_patch_path_guard.py`（10 条）。
- **【高】记忆没有归属维度 → 跨租户读 + 记忆投毒**：记忆只按 `(scope, key)` 存，`scope` 只区分
  "哪一类"（RUN/SESSION/USER/WORKSPACE）**不区分"谁的"**；`GET /memory/{scope}` 不做任何归属过滤，
  任一用户能读全部署的记忆；会话召回也不带归属，A 写的记忆会进所有人的提示词。
  修法：`MemoryItem` 增加 `owner`（+ Sqlite 列迁移与索引），仓储/服务/工具全链按 owner 过滤；
  会话驱动时用 ContextVar 把 `run.user_id` 传给共享的记忆工具（归属**不来自工具入参**）；
  `/memory/{scope}` 按调用者收敛。回归：`tests/test_memory.py`、`tests/test_memory_persistence.py`。
- **【中】`/chat/stream` 用 `?user_id=` 写归属**：非流式 `/chat` 走 `_identity`（身份来自凭证），
  只有流式这条直接用查询参数 → 客户端可把消息写进他人名下。修法：改用 `_identity`。
- **【中】`/audit` 按租户而非调用者过滤**：多用户默认共用同一租户（`WARDEN_TENANT=local`），
  只按 tenant 过滤 = 把同租户他人的操作账本（run_id/身份/路径）交出去，还能枚举账号。
  修法：加 `principal_id` 过滤；另外 403 错误信息**不再回显归属者 user_id**（错误信息是给攻击者的情报）。
- **【中】`/models/select` 无授权隔离**：任一认证用户调 `registry.set_model` 会切换**所有人**的模型
  （跨租户 DoS，或把别人的额度记到自己 key 上）。修法：模型选择**按调用者收敛**（`_owner_models`），
  会话按归属解析模型；`/models` 报调用者自己的当前模型。

### 正确性 / 可靠性

- **【高】Run 永不进 `FAILED` → 重试上限失效**：`AgentRun.fail()` 从未被调用，驱动抛异常后状态停在
  `RUNNING`；恢复计划只对 `FAILED` 走"重试 + attempts 上限"，于是 `attempts` 永不递增、
  坏 Run 被**无限重试**。修法：驱动期异常先标记 `FAILED` 再抛（`_fail_run_on_error`，覆盖 `_run_loop`
  与 `stream`）；回归同时把"崩溃续跑"的测试改成模拟**硬杀**（异常属于失败，不是崩溃）。
- **【高】非纯工具超时也重试 → 高危操作执行两次**：`_should_retry` 把超时当瞬时故障无条件重试，
  而超时只是"放弃等待"、被卡调用仍在后台跑 → 一次审批两次 `fs.delete`（与该文件自身的安全断言矛盾）。
  修法：超时只有 `pure` 工具能重试；抛出的瞬时异常仍按原设计重试（那是"这次没成功"）。
- **【中】稳定性层超时泄漏非守护线程**：每次超时新建 `ThreadPoolExecutor`，其工作线程非守护、
  且会在解释器退出时被 join → 卡死工具累积线程并**拖住进程退出**。修法：改守护线程 + `Event`，
  并给"卡死线程数"设上限（达到上限拒绝新调用，防无界增长）。
- **【中】幂等 TOCTOU**：原实现"先查→执行→再写"，两个同 `Idempotency-Key` 的并发请求都读到空、
  各执行一次副作用。修法：新增原子占位原语 `reserve`/`release`（存储层 `INSERT ... ON CONFLICT DO NOTHING`），
  占不到位的直接回 **409**；执行失败释放占位以便重试。回归：`tests/test_idempotency_toctou.py`。
- **【中】多轮里重复同一句用户消息被静默吞掉**：`_already_has_user_turn` 扫**全历史**匹配内容，
  于是第二轮完全相同的输入不落库——但模型照样被驱动，**历史与执行不同步**。
  修法：只检查"当前这一轮"（最后一条消息）；真正的重发去重用 `Idempotency-Key`（已测）。
- **【低】Windows Job 限制器单槽竞态**：limiter 只存 `self._last_limiter`，并发执行时后一个覆盖前一个，
  前一个可能被 GC → 该子进程的内存/CPU 限制提前失效。修法：limiter 随 `ManagedProcess` 条目持有。

### 仍未处理（已知，非本轮范围）

- SSRF 的 DNS 二次解析 TOCTOU（校验用的 IP 与实际连接的不是同一批）：需把已校验 IP 固定到连接上
  （自定义 transport/resolver）才能消除；`web/search.py` 的其余 SSRF 防护经审计确认为有效。
- 单副本 `InProcessEventBus` 的 `_KEEP=500` 上限：慢消费者在两次轮询间积累超过 500 条会丢早期事件
  （只影响进度展示，结果仍写 messages）。

### 验证

`772 passed / 2 skipped`（起了 PostgreSQL，共 774 项；跳过 = win32 无内核隔离档、且当前权限不允许建符号链接；
不配库时 747 passed / 27 skipped），`ruff` 全绿，`mypy --strict` 93 文件零错误。

---

## 2026-09-22（第十批：对外交付级的三项收尾 —— 容器冒烟 / 可观测性 / 密钥托管）

> 背景：成熟度评估把"对外/SaaS/多副本交付"还差的三项定为——① 真正的 KMS/HSM 托管
> ② 容器构建冒烟 ③ 告警规则库/SLO/链路追踪/灰度。这批把三项都做了。

### 本轮改动（工作区，尚未 commit）

- **容器构建冒烟（CI 真的会 build 镜像了）**：新增 `scripts/container_smoke.sh`——
  真的 `docker build`、真的起容器（只读 rootfs + `cap_drop ALL` + `no-new-privileges`）、
  打 `/health/live`、带 Bearer 校验 `/metrics`、并验"无 key 时受保护接口必须 401/403"。
  CI 新增 `container` job 调它。**此前 CI 全是"在源码树上"的检查，镜像从未被构建过**。
  ⚠️ **本轮它当场抓到一个真 bug**：Dockerfile 用未加引号的 `$(...)` 展开依赖，被 `pywin32`
  的环境标记（含空格）切成一个孤立的 `==` 依赖名，pip 报 `Invalid requirement: '=='`——
  **镜像其实一直构建不出来**（此前没人构建，所以无人知晓）。改成"依赖落 requirements 文件再 `-r` 装"。
  另修一处测试脚手架：/data 用 **数据卷**（与 compose 一致、从镜像继承属主）而非 tmpfs，
  否则非 root 应用写不了 SQLite（那是脚手架问题，不是镜像问题）。
- **链路追踪（W3C traceparent）**：新增 `core/tracing.py`——`traceparent` 的解析/生成、
  `ContextVar` 承载上下文、`span()`/`server_span()` 记结构化 span 日志（含 `trace_id/span_id/
  parent_span_id/duration_ms`）。接进 HTTP 中间件（入站接着上游的链、响应回写 `traceparent`）
  与出站抓取（透传 `traceparent`）。**零新增依赖**（不引入 opentelemetry）；
  开关 `WARDEN_TRACING`（默认开，关掉时上下文开销接近零）。脏入站头一律当没有、重新起链。
- **告警规则库 + SLO/错误预算**：新增 `deploy/observability/`——`alerts/warden.rules.yml`
  （可用性/错误率/延迟/业务共 7 条）、`alerts/warden.slo.yml`（可用性与延迟 SLI 记录规则 +
  多窗口燃烧率告警 4 条）、`prometheus.yml`（**Bearer 抓取**）/`alertmanager.yml`/一键起栈 compose/README。
  新增指标 `warden_stuck_runs`（抓取时现算，避免重启/多副本漂移）。新增 `tests/test_alert_rules.py`
  守卫"规则引用的指标必须真实存在、记录规则必须被引用"——本轮它就抓出一条**定义了却没人引用的死规则**。
  CI 用 `promtool check rules` 校验语法（本地实跑：7 + 9 条规则 SUCCESS）。
- **灰度发布 + 健康门控回滚**：新增 `scripts/canary_rollout.sh`——起金丝雀 → 等 `/health/ready`
  （含存储探活）→ 校验鉴权仍 fail-closed → 通过才允许放量，否则就地拆掉（旧版继续服务）。
  明确边界：本项目无内置网关/LB，脚本只做**门控**，切流是部署侧的事。
- **KMS/HSM 托管（信封加密）**：新增 `credential/kms.py`——`KeyProvider` 协议 +
  `EnvKeyProvider`（演进前行为） + `AwsKmsKeyProvider`（AWS KMS 解包 DEK，可选依赖 boto3） +
  `VaultTransitKeyProvider`（Vault transit，走 httpx，**零新增依赖**）。接进 `default_broker`
  （新增 `key_provider` 注入点）。根密钥待在 KMS/HSM、只分发被包装的 DEK；未知 provider 取值
  **直接报错不静默回落**。新增可选依赖 `aws = ["boto3>=1.34"]`（`uv.lock` 已同步）。
- **配置面**：新增登记 `WARDEN_TRACING` / `WARDEN_KMS_PROVIDER` / `WARDEN_KMS_WRAPPED_KEY` /
  `WARDEN_VAULT_ADDR` / `WARDEN_VAULT_TOKEN` / `WARDEN_VAULT_KEY_NAME`，并按其真实读取模块
  授权 consumers（`tests/test_config_surface.py` 全程把关）。
- **新增测试**：`tests/test_tracing.py`（12 条）、`tests/test_kms.py`（14 条）、
  `tests/test_alert_rules.py`（4 条）。
- **文档**：`docs/operations.md` 补第九～十二节（告警规则库与 SLO / 链路追踪 / 灰度与回滚 /
  密钥托管），并更新巡检清单与"已知边界"表；`deploy/observability/README.md`。

### 仍未做（诚实边界）

- span 送进 Jaeger/Tempo 需要接 OTLP exporter（本层只保证上下文不断 + 结构化日志）。
- 真实云 KMS/Vault 的联调需要凭据，仓库测试用桩替身，**不联真实云**。
- `warden_stuck_runs` 抓取时扫库；规模大应改物化视图/后台刷新。
- 告警阈值是起点，需按业务真实水位调。

---

## 2026-09-21（第八批：出站限速与配额）

### 本轮改动（工作区，尚未 commit）

- **给"Agent 主动往外发请求"补上总量闸门**。此前 `web.fetch` 只有**单次请求**的边界
  （超时 10s / 响应体 200KB / 跳转 3 次），而它们只界定"一次"，不界定"多少次"：
  模型一轮抓 50 个链接、并发用户相乘、高频打同一站点被 429、按次计费的检索 API 被失控
  循环跑光配额——四类事故一个都拦不住。新增 `web/outbound.py` 的四道闸：
  1. **全局速率**：默认每 60 秒 120 次（`WARDEN_OUTBOUND_LIMIT`）；
  2. **单 host 速率**：默认每 60 秒 20 次（`WARDEN_OUTBOUND_HOST_LIMIT`），对单个站点保持礼貌；
  3. **并发上限**：默认 8（`WARDEN_OUTBOUND_MAX_CONCURRENCY`），进程内信号量、不阻塞等待；
  4. **日配额**：默认**不限**（`WARDEN_OUTBOUND_DAILY_QUOTA=N` 开启）——硬性停机上限应由运维
     显式决定，与"Agent 能不能出网"同一个取向。
- **接在唯一的收口点上**：`make_web_tools`（`web/search.py`）——搜索与抓取都从这里出网，
  所以现在与将来的 provider 一并受管；`augment_catalog` / `build_agent` / `build_app` /
  `run_server` 透传。
- **顺序：URL 策略先于出站闸门**。被策略拒掉的 URL（内网/环回/元数据地址）**不消耗配额**——
  拒绝不等于"发出去了"；顺序反了会让内网 URL 把配额刷爆。
- **离线 provider 不占配额**：`LocalMockSearchProvider` 补上 `requires_network = False`
  （与 `LocalMockFetchProvider` 对齐），所以演示与测试完全不受影响。
- **被限流返回可读文本而不是抛异常**（`[限流] ...`）：工具不该因配额用尽把会话循环打崩，
  把"现在不行"交回给模型判断。
- **计数复用入站限流那套存储接缝**（`RateLimitStore`）→ 多副本下把 store 换成存储实现，
  出站限额才是**全局**的；否则实际额度 ≈ 配置 × 副本数（并发信号量天然是进程内的，已文档化）。
- 新增 20 条测试（`tests/test_outbound_limit.py`）：四道闸各自的行为、窗口过期恢复、
  **单 host 不串到别家**、`release` 幂等（不会把信号量越还越多）、**多线程不串号**、
  共享存储下两个"副本"共用额度 + **进程内不共享的对照组**、离线 provider 不占配额、
  联网抓取被限速且 provider 未被真的二次调用、**策略先行的顺序保证**、环境变量解析与报错。

- **修掉两处"一个变量名干两件事"（都是会静默出错的那种）**：
  1. **`WARDEN_API_KEY`** 同时被 **HTTP 鉴权**（`web/run_server.py`）和 **`custom` 模型的 API Key**
     （`model/deepseek.py`）读取 —— 于是"用 `WARDEN_API_KEY` 开鉴权 + 用 custom 接自建网关"时，
     模型会把**服务端的鉴权密钥**当成模型 key 发给那个第三方网关。现在模型侧改用
     **`WARDEN_MODEL_API_KEY`**，`WARDEN_API_KEY` 只做鉴权。
  2. **`WARDEN_BASE_URL`** 同时被 **CLI 的服务地址**（`cli.py`）和 **`custom` 模型的端点**读取 ——
     配了自建模型网关之后，`warden chat` 会把请求发到**模型网关**上去。现在 CLI 改用
     **`WARDEN_SERVER_URL`**，`WARDEN_BASE_URL` 只做模型端点。
  - 顺带修掉一个**会泄密**的隐患：`custom` 此前会回落到 `OPENAI_API_KEY` / `DEEPSEEK_API_KEY` ——
    等于"我把 custom 指向了某第三方网关、却忘了配 key"时，**把厂商密钥发给那个第三方**。现在
    custom 的 key 必须显式配置（本机网关/Ollama 填 `not-needed` 这类占位串即可），缺失时给出可操作的报错。
  - `.env.example` 里 `WARDEN_API_KEY` 原本**被赋值两次**（第 13 行鉴权、第 77 行当模型 key），
    后写的会**静默覆盖**鉴权密钥；且模板里预填了一个公开可见的 key（等于人人知道你的密钥）。
    现已改为注释 + 明确指引。
  - 新增 6 条回归测试（`tests/test_deepseek.py` ×4：不读鉴权密钥 / 不回落其他厂商密钥 /
    占位串可用；`tests/test_cli.py` ×2：`WARDEN_SERVER_URL` 生效 / 不读 `WARDEN_BASE_URL`）。

- **配置面有了单一事实源 + 可执行的守卫（企业级推进第 1 项）**。此前环境变量读取散在
  **8 个文件、24 处**，而 `core/config.py` 只有 34 行（仅 `load_env`）——没有任何地方声明
  "这个变量干什么、谁有权读"。这正是本批两个"同名两用"bug 的**根因**（`WARDEN_API_KEY`、
  `WARDEN_BASE_URL`）。新增 `core/settings.py`：
  - `ENV_SPECS` 登记全部 **37 个**变量：用途 / 归属模块 / **允许读它的模块** / 默认值 / 类型 / 是否敏感；
  - `validate_env()`：启动时校验格式（写错**拒绝启动**，与鉴权 fail-closed 同一取向）；
  - `unknown_warden_variables()`：揪出**拼错的** `WARDEN_*`（如 `WARDEN_RATELIMIT`）并告警——
    原先会被静默忽略、悄悄用默认值，是最难查的一类问题；
  - `describe()`：DEBUG 级打印生效配置（敏感值打码）。
  - **关键是让它可执行**：新增 `tests/test_config_surface.py`，用 AST 扫描源码断言
    ①代码读的每个变量都已登记；②**读它的模块 ⊆ 声明的 consumers**（这条就是"同名两用"的
    拦截点——实测把 `cli.py` 改成读 `WARDEN_BASE_URL`，守卫立刻红并指出越权模块）；
    ③登记了却没人读也要报（拼错/废弃）。没有这三条，注册表就只是文档。
  - 顺带把 `web/outbound.py` 的两个变量读取改成字面量形式，让守卫能完整覆盖。
  - 新增 12 条测试；端到端验证：`WARDEN_RATE_LIMIT=abc` → 退出码 2 + 可操作报错；
    `WARDEN_RATELIMIT=10/60` → 服务正常启动 + 明确告警。
  - **尚未做**：各模块的读取仍各自 `env.get(...)`（只是被守卫看着），还没统一改走类型化访问器。

- **静态审查 PostgreSQL 实现，发现一个"只在 PG 上炸"的真问题并修掉**。起因是推进"企业级"
  的第 2 项时发现：项目声称"存储可换 PostgreSQL"，但 `tests/test_store_interface.py` 对 PG 是
  `skipif` 跳过、CI 里也没有 PG 容器 —— **PostgresStore 从未被自动化验证过**。在真起一个库之前，
  先做能离线做的静态审查，结果查出：
  - **连接的"毒丸"问题（真 bug）**：`psycopg.connect(...)` 默认 `autocommit=False`，
    而该文件里**没有任何 `rollback()`**。PG 的语义是——事务内任一语句报错后，整个事务进入
    aborted 状态，**此后所有语句（含读、含 `/health/ready` 探针）全部失败**，直到有人回滚。
    也就是说：**一次坏写就能把这条连接废掉，而且不会自愈**。SQLite 没有这个语义
    （错的只是那一条语句），所以这个坑只在 PG 上暴露——正是"从未验证"所掩盖的那类问题。
    修法：连接改 `autocommit=True`（错误不再污染连接；读也不再长期占 idle-in-transaction），
    需要原子性的多语句写入改用显式事务块 `with self.conn.transaction():`（psycopg3 在 autocommit
    下也支持）。已给 `delete_run`（五条 DELETE 必须同生共死）与 `append_message`（查重+插入）
    补上事务块。
  - 其余静态项核对通过：三个 store 实现都齐了 `RunStore` 的 **19 个**协议方法；
    `PostgresStore` 也齐了凭证保管库的 8 个方法（缺了会**静默**退回进程内保管库）；
    每个 `ON CONFLICT (列)` 的目标列都有主键/唯一约束；SQL 全部用 `%s` 占位符。
  - 新增 `tests/test_postgres_contract.py`（8 条**纯静态**契约检查：autocommit、ON CONFLICT 的
    唯一约束、多语句事务块、占位符风格、两套协议的方法完整性、租约过期索引）。
    **实测有牙齿**：撤掉 `autocommit=True`、撤掉 `delete_run` 的事务块，对应检查都立刻变红。
  - ⚠️ **边界（别夸大）**：静态检查只能证明"代码写成这样"，**不能替代真跑一遍**——
    SQL 语义、类型转换、并发行为仍需真实数据库验证（推进项 2 仍待做）。

- **在真实 PostgreSQL 上跑通了**（推进项 2 的实质部分）。起了一个 `postgres:16-alpine`（PG 16.15），
  把上一个条目里静态审查出的「毒丸连接」以及**从未被跑过的 PG 路径**全部实测了一遍：
  - **对照实验证明那个 bug 是真的**：用修复前的配置（`autocommit=False`）走「一条失败语句 → 后续查询」，
    连接确实被污染、后续查询全部被拒（`current transaction is aborted, commands ignored until end of
    transaction block`）；修复后（`autocommit=True`）同一序列完全正常。**静态审查的结论由此有了实测背书。**
  - 实测通过：`RunStore` 全量协议方法（run / 消息含工具调用 / 待审批 / 审批历史 / 存档点 /
    `list_runs` 含 owner 过滤 / 用户表）、共享状态三件套（幂等 UPSERT、事件 `BIGSERIAL` 自增与增量读、
    限流窗口计数与滚动 —— 那段 `CASE WHEN` UPSERT 没写错）、凭证保管库（密文往返、租约往返含时间戳精度、
    过期惰性清理、删除）、`delete_run` 五张表全清（事务块）、`append_message` 查重。
  - 端到端：`as_vault(PostgresStore)` **确实被认成保管库**（否则会静默退回进程内、以为落库其实没落），
    `CredentialBroker` 加密落 PG 后**换一个新 broker（模拟重启）仍能取回明文**，且库里没有明文。
  - 新增 `tests/test_postgres_integration.py`（**11 条真库集成测试**，含那条对照实验）。
    连接参数可用 `WARDEN_TEST_PG_*` 环境变量覆盖（方便挂到 CI 的 service container）。
    **没起 PG 时整体自动跳过**（已实测：11 skip、0 failed），所以 CI 保持绿。
  - 测试数因此分两套：**无 PG = 591 passed / 13 skipped；起了 PG = 603 passed / 1 skipped**。
  - ~~仍待做：把 PG service 加进 GitHub Actions~~ → **已做并在推送后确认**（2026-09-22）：CI 里 16 条 PG 测试零跳过，另加了一条断言防「静默跳过」。

- **依赖锁定（企业级推进第 3 项）**。新增 `uv.lock`（48 个包全部固定版本）；CI 改为
  `uv sync --frozen` + `uv run --frozen ...`。**`--frozen` 是关键**：lock 与 pyproject 不一致时
  CI 直接失败，而不是"本地悄悄装到别的版本、CI 才挂"。本地已实测：按 lock 从零建一个隔离环境
  （101 个包），在该环境里 ruff / mypy / pytest 三项全过（**603 passed / 1 skipped**，与系统环境一致）。
  - **锁版本立刻抓出一个未声明的依赖**：`execution/_platform.py` 的 Windows 资源限制走 Windows
    Job Object，需要 pywin32 提供的 `win32job`，但 `pyproject.toml` 里**没声明**——系统 Python 里
    恰好装着它，才一直没暴露；照 lock 从零装的环境里 `test_sandbox.py` 两条直接
    `ModuleNotFoundError`。更要紧的是 `make_limiter` 是**无保护调用**（只捕获 `FileNotFoundError`），
    所以干净环境下跑"带资源限制的沙箱命令"会**硬失败**。已修：加
    `pywin32>=306; sys_platform == 'win32'`（只对 Windows 生效；POSIX 走标准库 `resource.setrlimit`，
    不受影响）。
  - **顺带查出一个 Windows 平台边界（如实记录，不是本项目 bug）**：venv 里的 `python.exe` 是个
    **转发器**，它自己还要再拉起真解释器；一旦转发器被放进 Job Object，这个子进程创建就失败
    （`Unable to create process using ...`，exit 101）。用四条命令隔离验证过：真解释器 ✅、
    `cmd` 内建命令 ✅、venv 转发器 ❌；且**与设了哪个具体限制无关**（连 `max_files` 这种不设 flag 的
    也中）——只要进程被放进 Job Object 就会中。影响面：Windows 上若应用跑在 venv 里，
    沙箱执行"venv 的 python"会失败；真解释器与普通可执行文件不受影响。已写进
    `execution/_platform.py`、README 与测试注释；测试改用 `sys._base_executable`（venv 背后的真解释器）
    以免被该边界误伤。
  - 验证：系统环境与 frozen 环境都是 **603 passed / 1 skipped**（PG 在跑时）+ ruff / mypy 全绿。

- **Run 级分布式锁（企业级推进第 4 项）**。多副本下"同一个 run 被两个副本同时驱动"一直没有闸门：
  同一个 `run_id` 若同时出现在两边的一份恢复计划里，两边都会去写状态与消息，结果是
  **后写覆盖前写**（历史分叉或丢失，且不报错）。此前项目对这件事的说法只是"建议做会话粘性"，
  等于把正确性交给部署方自觉。现在：
  - 新增 `runtime/locking.py`：`RunLock` 协议 + `InProcessRunLock`（单副本默认，行为不变）
    + `SqlRunLock`（多副本，复用 `RunStore` 所在库，新增 `run_locks` 表）+ `run_lock_for(store, shared=)`。
  - **取锁是单条原子语句**：`INSERT ... ON CONFLICT(run_id) DO UPDATE ... WHERE expires_at <= now`
    —— 键空闲或租约过期时可被接管，再读回核对 `(owner, expires_at)` 确认归属。
    SQLite 与 PostgreSQL 语义一致（已在两者上分别验证）。
  - **租约式而不是硬锁**：带 TTL，持有者崩了**不必人工解锁**，到期即可被别的副本接手——
    这对"崩溃恢复"场景是必须的，否则一次宕机就永久锁死一个 run。`release` 只删自己的锁
    （非持有者释放无效），`renew` 只能续自己的且未过期的锁。
  - **接进 `RecoveryWorker`**：每个 run 驱动前先抢锁，抢不到记为 `held_by_other` 并跳过本轮
    （不再"两边一起写"）；`finally` 里释放，**失败路径也释放**（否则一次失败要等租约过期才能重试）。
    `warden recover --apply` 会据 `WARDEN_SHARED_STATE` 选共享锁还是进程内锁。
  - 新增 16 条测试（`tests/test_run_lock.py`）：锁语义（互斥 / 同 owner 幂等 / 过期接管 /
    续租与误释放边界 / owner 标识）、**跨 store 实例互斥**（模拟两个副本）、装配回落与告警、
    worker 集成（抢不到不驱动、跑完释放、失败也释放）。
  - 真库验证（PG 16.15）：`run_locks` 表在 PostgreSQL 上互斥与接管行为正确；
    并做了一条**并发抢占测试——8 个"副本"各用独立连接同时抢同一把锁，恰好一个赢家**
    （`test_postgres_integration.py`，共 13 条真库测试）。
  - ⚠️ **仍未做的部分（如实标注）**：**HTTP 对话路径（`/chat/{run_id}`）没有接这把锁**——
    同一 run 被两个副本同时 `chat` 仍会后写覆盖前写。所以对外多副本**仍建议做粘性路由**，
    或把 Run 锁接进会话驱动路径（接口已备好）。这一点已写进 `docs/deployment-boundaries.md`。
  - 顺带：新增 `core.settings.env_flag()` 统一布尔开关解析（原先 `WARDEN_SHARED_STATE` 的解析
    散在两处）——结果**配置面守卫立刻抓到我自己**：先写成 `env_flag(env, name)` 导致变量名
    不是字面量、AST 扫不到读取点；改成 `env_flag(env.get("X"))` 后守卫又指出 `cli.py` 读该变量
    未在登记表里授权。两处都按守卫要求改正（详见 `9-排查日志.md`）。

- **Run 锁接进 HTTP 路径（补上第 4 项里如实标注的那个缺口）**。上一轮只接了无人值守的
  `RecoveryWorker`，并明确标注"HTTP 对话路径尚未接锁、对外多副本仍建议粘性路由"。现在补上：
  - `/chat/{run_id}`、`/chat/stream/{run_id}`、`/approve/{run_id}`、`/reject/{run_id}`
    四个会**推进会话状态**的端点都先抢 Run 锁；抢不到返回 **423 Locked**，客户端稍后重试。
  - 为什么用 423 而不是 409：**409 在本服务里已被用来表示"没有待审批的请求"**，语义会撞车。
  - **每请求一个 owner**（不是每副本一个）是刻意的：同进程内两个并发请求驱动同一个 run
    同样属于并发写，也必须被挡住——同 owner 重复取锁是允许的（那是给重入用的），
    所以不能复用。
  - **流式**请求在返回 `StreamingResponse` 之前取锁，整段 SSE 走完（含客户端断开触发
    `GeneratorExit`）才在 `finally` 里释放——提前释放等于开门让人并发写。
  - `run_server` 按 `WARDEN_SHARED_STATE` 自动选共享锁；启动日志与 `/capabilities`
    都会报出**当前用的是哪种锁**（进程内锁在多副本下等于没锁，运维必须能一眼看到）。
  - 新增 9 条测试（`tests/test_run_lock_http.py`）：顺序请求不受影响、被占用时 423、
    释放后可通过、请求结束/失败都要释放锁、审批路径受保护、流式取锁与释放的时序、
    两个 app 共享存储（= 两个副本）互斥。
  - ⚠️ **仍然存在的窗口（如实标注）**：单次驱动若**超过锁的 TTL**（默认 600 秒），
    另一个副本可以接管，于是又出现并发驱动。按"单次驱动最长耗时"设 TTL，或改用带心跳的
    续租循环（`RunLock.renew()` 已提供，但**没有内置后台心跳线程**）。
    文档里同步把"粘性路由"从"正确性必需"降级为"体验建议"。

- **运维面起步（企业级推进第 5 项）**。此前"运维面基本是空的"是评估里剩下最大的一块空白——
  尤其一个具体洞：**Run 进入 `WAITING_APPROVAL` 之后不会自己动**（没超时 / 没重试 / 没通知），
  线上没人盯就一直挂着。本轮补上三件事：
  - **挂起告警**：新增运行时的 `stuck_awaiting_human()`——找出"等人工处理超过阈值"的 Run。
    入口两条：CLI `warden stuck --older-than-min 60`（**有输出即需人管，退出码 3**，
    可直接接 cron / 监控）与 `GET /alerts/stuck?older_than_min=60`（认证模式下按归属收敛，
    与其它列表接口同口径）。⚠️ 口径如实标注：等待时长用 Run 的**最后活动时间**近似、只会**低估**——
    "报警了"可信、"没报警"不等于没挂久；要精确需在进入等待时单独记时间戳（未做）。
  - **备份 / 恢复**：新增备份模块 + CLI `warden backup` / `warden restore`。
    SQLite 用标准库的**在线备份 API** 做一致性快照（不推荐 `cp`：可能拷到"半个事务"的中间态），
    备份后**立刻做完整性校验**；恢复是破坏性操作，所以目标库存在时**默认拒绝、必须显式 `--force`**，
    且恢复前先校验备份确实是健康的 SQLite 库。**并写了自动化演练测试**：造数据 → 备份 →
    破坏原库 → 恢复 → 逐项验数据（这才是"备份能用"的可信证据，只测"文件生成了"没有意义）。
    PostgreSQL 走 `pg_dump` / `pg_restore`（本机没装该客户端，所以**没进代码**，写进了运维手册）。
  - **运维手册** `docs/operations.md`：巡检清单、告警接法、备份节奏、**恢复操作顺序**、
    升级 / 回滚步骤（含"新版能读老库、老版读不了新库"的 schema 兼容说明）、多副本检查清单、
    密钥轮换注意（换凭证密钥会导致存量密文解不开）、以及一张"已知边界"表。
  - 新增 **24 条测试**：备份恢复 11 条（含恢复演练、不覆盖已有备份、坏备份当场识破、
    备份期间写入仍一致）、挂起告警 13 条（含阈值边界、拿不到时间戳宁可报出来、归属过滤、
    CLI 退出码 3、HTTP 端点）。
  - **仍未做（如实标注）**：挂起时长的精确时间戳、告警规则库、SLO / 错误预算、链路追踪、
    灰度发布与自动回滚。手册第八节把边界列全了。

- **把两处"如实标注的剩余窗口"也关掉了**（上一轮明确写进文档的两个缺口）：
  - **Run 锁的 TTL 窗口 → 用后台心跳续租关掉**。锁是租约式的（好处：持有者崩了不必人工解锁），
    代价是**单次驱动若比 TTL 还长**，租约中途过期、别的副本就能接管——于是又变成并发驱动。
    新增 `RunLease`（`runtime/locking.py`）：取锁后起一个守护线程，每 **TTL/3** 续一次租；
    `stop()` 停心跳并释放，幂等。CPU 开销可忽略（默认 600s TTL → 每 200s 一次续租）。
    - **续租失败会被察觉而不是静默**：`lost` 置真 + 打警告 + 可选回调，并**停止续租**
      （不假装还持有）——"我们可能已经不是在独占驱动"必须报出来。
    - 流式路径要用**手动 `start()` / `stop()`**：锁在端点里取（抢不到就地 423），
      但要活到 SSE 流结束才释放；worker 用 `with` 即可。
    - 新增 7 条测试，含一条**对照组**（没有心跳时确实会被接管，证明问题真实存在）
      与一条**端到端**（worker 驱动 0.9s 而 TTL 只有 0.6s，另一个副本全程抢不到）。
  - **挂起时长从"近似"变"精确"**。此前告警用 Run 的最后活动时间算"等了多久"，
    若该 run 在等待期间被别的操作碰过就会被刷新、从而**低估**（该报的挂起被漏掉）。
    现在 `pending_approvals` 增加 `created_at`（进入等待审批那一刻），告警**优先**用它；
    `WAITING_INTERACTION` 这类没有待审批记录的仍退回近似，并通过 `StuckRun.source`
    （`approval` / `last_activity` / `unknown`）**如实标明这个数是怎么来的**，
    CLI 文本与 HTTP 响应都会带上，人不必猜。老库走 `ALTER TABLE ADD COLUMN` 迁移，
    历史行 `created_at` 为 NULL → 自动退回近似（行为不变）。
    - 新增 5 条测试 + 1 条真库测试（后者顺带覆盖了"表已存在时补列"的迁移路径）。
- **CI 加真实 PostgreSQL service**（`.github/workflows/ci.yml`）：起 `postgres:16-alpine`
  （trust 认证 + `warden` 库 + 健康检查），并用 `WARDEN_TEST_PG_*` 环境变量指过去。
  这样"存储可换 PostgreSQL"从**声称支持**变成**在 CI 里被验证**——此前 PG 集成测试是被
  `skipif` 跳过的，等于那段代码从未在自动化环境跑过。
  **已在推送后确认（这一步原先标注为"无法本地验证"）**：CI 首跑 663 passed / 5 skipped 全绿，
  但"绿"本身**不能证明 PG 测试真跑了**（skipif 的副作用：service 连不上也会静默跳过、照样绿）。
  所以补了两处：pytest 加 `-rs`（跳过原因进日志）、并新增一步**断言**
  （单独跑 PG 两个测试文件，输出里出现 `skipped` 就 exit 1；本地已双向验证该断言有效）。
  第二次 CI 的结果给出直接证据——5 条跳过的原因分别是 MCP 连不上（3，CI 未构建 ts 客户端）、
  runner 容器不允许建命名空间（1）、前端未构建（1），**没有一条是 PG**；
  而断言步骤输出 **`16 passed`**：PG 的 16 条测试在 CI 里全部执行、零跳过。

- **合规与性能三件（企业级推进第 6 项）**：
  - **审计链（防篡改）**。"审计"如果谁都能改就只是普通日志。落盘的每条记录现在带链哈希
    （`prev_hash` → `hash`，覆盖记录内容 + 前驱 + 行号），于是改字段、删中间行、重排都会断链。
    - **用 HMAC-SHA256（密钥 `WARDEN_AUDIT_KEY`）而不是裸 SHA256**：裸哈希链只能防"手改一行"，
      攻击者可以改完再把整条链重算一遍、看起来依然自洽。测试里专门留了一条**对照**：
      无密钥时确实挡不住"重算整链"，配上密钥后伪造的链立刻露馅——这就是"为什么必须配密钥"的证据。
    - 入口 `warden audit-verify`（被动过则**退出码 4**，可接巡检）；老库走 `ALTER TABLE` 补列，
      加链之前的历史行会被**如实报告为"无法证明未改动"**，不假装完整。
  - **凭证密钥轮换**。此前换 `WARDEN_CREDENTIAL_KEY` 等于把存量密文全废掉。现在
    `CredentialCipher` 支持挂**历史密钥**（只用于解密兜底，加密永远用当前密钥），
    配套 `rotate_credentials()` 把存量密文逐个重加密到新密钥，入口 `warden rotate-credentials`
    （有解不开的则**退出码 5**——不能假装成功）。流程：新密钥配主密钥 + 旧密钥进
    `WARDEN_CREDENTIAL_OLD_KEYS` → 跑轮换 → 摘掉旧密钥。轮换**幂等**；解不开的凭证
    **原样保留**并记进报告（那些密文还是旧密钥，摘早了就永久损失）。
    - 过程中修掉自己设计上的一个真问题：`needs_rotation()` 原本在"两把密钥都解不开"时返回
      False → **把"数据有问题"伪装成"无需轮换"而静默跳过**。改成抛 `InvalidToken`
      （测试先写、代码后改，正是那条测试抓出来的）。
  - **低延迟事件总线（LISTEN/NOTIFY）**。新增 `PostgresNotifyEventBus`：`WARDEN_EVENT_BUS=notify`
    时用 LISTEN/NOTIFY 把订阅从"睡满轮询间隔"变成"变化即醒"。
    - **通知只是提示，正确性仍靠落表 + 读表**：事件先写 `run_events`（durable）再 NOTIFY；
      订阅端醒来一律回表读增量；通知丢了（例如发出时对面还没 LISTEN）只会让这一次退回轮询，
      **不丢事件、不乱序**（有测试专门模拟"通知丢失"）。
    - 实测（真 PG）：从发布到被唤醒 **47ms**，同场景纯轮询 110ms（轮询最坏是整整一个间隔 250ms）。
    - 只有 Postgres 有 LISTEN，且需要"另开一条连接"（`PostgresStore.new_connection()`）；
      不满足时**回落为轮询并告警**，不假装用了通知。
  - 新增 43 条测试：审计链 18 条（含篡改检测与"无密钥挡不住重算"的对照）、密钥轮换 15 条、
    通知总线 10 条（含通知丢失不丢事件、真实多副本唤醒、延迟低于轮询间隔）。

### 尚未实现（路线图）

- **真实搜索 provider 仍未实现**：`web.search` 依旧是离线 mock。检索 API（Tavily / Brave /
  阿里云 IQS 等）都要第三方 key；这次拿到的 DeepSeek key 是**对话模型**的，不提供搜索接口，
  补不了这一项。接口（`WebSearchProvider`）、URL 策略、出站闸门都已就位，补一个类即可接入。
- **抓取仍是去标签的粗提取**：没有 Readability 那类正文抽取，也拿不到 JS 渲染的 SPA 页面
  （需要 headless 浏览器）。
- **出站配额没有按调用者/按 Run 细分**：当前是进程级（全局 + host + 日配额），
  没有"每个用户每天最多 N 次"这种维度——多租户下若要按人计量需另加 key 维度。

### 验证

- `pytest` → **654 passed, 1 skipped**（新增 20 + 6 + 12 + 8 + 16 + 9 + 24 条；PG 在跑时）
- `mypy --strict` → 87 个源文件零错误；`ruff` → 全绿
- 真实 DeepSeek 端到端实测（chat / 工具调用 / 流式 / SDK 会话恢复）通过，用的是临时环境变量，
  未写入任何文件

---

## 2026-09-21（第七批：凭证密文与租约落库）

### 已提交（`b69ed78`，与第三～六批合并为一条）

- **凭证的"加密"终于兑现**。此前 `CredentialCipher` 的 AES-GCM 是真的，但加解密的密文与租约
  都活在 `CredentialBroker` 进程内的一个 dict 里——**进程一退凭证就没了**，`register` 过的
  key 全丢，加密等于白做（README 却把"凭证 AES-GCM 加密"列为纵深防御能力）。
  现在新增 `credential/vault.py`，把密文与租约交给存储层：
  - `CredentialVault` 协议 + `InMemoryCredentialVault`（默认，行为与历史一致）；
  - `SqliteStore` / `PostgresStore` **结构化满足**该协议（方法名签名一致即算，存储层不反向
    import credential 模块，依赖方向保持单向）；新增 `credentials` / `credential_leases` 两张表。
  - `build_app` 默认 `default_broker(vault=as_vault(store))`：用 SQLite/PostgreSQL 时**自动落库**，
    内存版存储则静默退回进程内（不会因"少实现一个接口"而崩）。
- **明文不落盘**。租约只落**元数据**（`name / issued_at / expires_at`），取租约时按 name 回查
  密文现解——所以"租约跨重启存活"和"明文不写库"两件事可以同时成立。测试直接读 `.db`
  文件字节断言明文不出现。
- **租约过期惰性清理**：`get()` 命中过期即删该行，`issue()` 顺手清掉同作用域的历史过期租约，
  避免表只增不减。
- **按调用者身份隔离**（`scope`）。⚠️ 这里对上一批的建议做了一处**修正**：原计划"按 tenant 隔离"，
  但本项目的 `tenant_id`（`WARDEN_TENANT`，默认 `local`）是**整租户共用**的，同租户的 alice 与 bob
  会互相读到对方导入的 key——隔离形同虚设。故改用 `caller.user_id`（= `principal_id`，来自凭证）
  作为作用域；**启动配置的 key 记为部署级**（`DEPLOYMENT_SCOPE`），全体调用者共用。
  查询 key 时先看自己作用域、再回落部署级。
- `CredentialBroker` 新增 `encrypted_fields()` 供审计/测试核对"库里没有明文"；既有测试里
  对私有属性 `_secrets` 的访问改为该公开方法（断言意图不变）。

### 尚未实现（路线图）

- **租约 TTL 仍未与模型生命周期绑定**：模型实例构造后自身持有密钥字符串，租约过期不会让
  已构造的模型失效。租约的价值在"保管与审计"，不在"运行时收回"（沿用上一批口径）。
- **没有 KMS / 密钥轮换**：密钥材料是单个环境变量，轮换需重新加密存量密文（未做迁移工具）。
  跨副本必须共用同一把密钥，否则副本互相解不开。
- **模型切换本身仍是全局的**：key 已按用户隔离，但 `/models/select` 改的是进程级"当前模型"，
  一个用户切换会影响所有用户——这是既有设计，本轮未改（要按用户分模型需另存每用户的当前模型）。
- **`credential_leases` 表会随租约发放增长**：靠惰性清理收敛，没有独立的后台清理任务。

### 验证

- `pytest` → **546 passed, 2 skipped**（新增 14 条 `tests/test_credential_persistence.py`）
- `mypy --strict` → 85 个源文件零错误；`ruff` → 全绿
- 测试覆盖：换实例读同一库凭证仍在、换实例租约仍有效、库文件里翻不到明文、
  过期租约读取时被清理、发新租约时清历史过期项、凭证删除后租约失效、
  不同 scope 互不可见/互不覆盖、`as_vault` 装配、HTTP 端到端
  （导入的 key 跨重启仍在；同租户 A/B 两用户互不可见、B 无法借用 A 的 key）

---

## 2026-09-20（第六批：真实联网抓取工具）

### 已提交

- **新增 `HttpFetchProvider`：项目里第一个真正会发网络请求的能力**。
  此前 `web.search` / `web.fetch` 的接口与 URL 安全策略都齐了，但**只有离线 mock**——
  `web.fetch` 默认对任何 URL 都返回"页面不存在"，等于没有联网能力。
  现在 `WARDEN_WEB_FETCH=1` 即可让 `web.fetch` 真发 HTTP 抓网页、并把 HTML 粗提取成可读正文。
  - **默认仍是离线 mock**（不发任何请求）："Agent 能不能出网"是应该由运维显式决定的能力，
    不该悄悄打开；也让测试保持全离线。
  - **安全上四道约束**（缺一道都能被绕过）：
    1. 工具层 DNS 校验（沿用既有 `WebUrlPolicy.check_for_network`）；
    2. **每一跳重定向都重新校验**——**关掉 httpx 的 `follow_redirects`**，自己逐跳解析。
       原因：公网域名返回 `302 Location: http://169.254.169.254/`（云元数据端点）是
       SSRF 的经典绕过，自动跟随重定向的实现会直接把内网请求打出去。这是本次最关键的一处。
    3. 只吃文本类响应（`text/*` / json / xml），二进制直接拒（不把图片塞进模型上下文）；
    4. 超时 / 最大响应体（默认 200KB，超了截断并标注）/ 最大跳转次数都有界。
  - `transport` 与 `resolver` 可注入 → 测试全程离线、确定（含重定向绕过的回归用例）。
  - `providers_from_env()` 负责开关；`/capabilities` 新增 `features.web_fetch`，
    启动日志在开启时会**明确告警"Agent 可主动访问公网"**。

- **新增 20 条测试**（`tests/test_web_fetch.py`）：HTML 正文提取、实体还原、script/style 剔除、
  文本/二进制分流、404 与网络异常转错误（不抛）、响应体截断、**非公网地址在发请求前就被拒
  （且断言一个请求都没发）**、**重定向到内网地址被拦且内网一次都没被请求**、
  **重定向到"解析为内网"的域名被拦（DNS rebinding）**、相对跳转解析、跳转次数上限、
  环境开关、能力清单暴露实现。
  另用**真实网络**验证过一次端到端：`https://example.com` 抓取成功并提取出正文；
  `169.254.169.254` 被拒。

### 尚未实现（路线图）

- **真实搜索 provider 没有实现**：`web.search` 仍是离线 mock（没配条目就返回空）。
  真实搜索 API（Tavily / Brave / 阿里云 IQS 等）都需要第三方 key，
  接口（`WebSearchProvider`）与安全策略都已备好，补一个类即可接入——
  但没有 key 就无法验证，所以没有硬塞一段未经验证的代码进来。
- 抓取没做正文抽取算法（Readability 那类），只有去标签的粗提取；也没处理 JS 渲染页面
  （拿不到 SPA 的内容，需要 headless 浏览器）。
- 出网没有全局限速/配额（只有单次请求的超时与响应体上限）；高频抓取需要另加。

### 验证

- `pytest` → **532 passed, 2 skipped**（共 534 项；无 node 的机器是 531 passed / 3 skipped）
- `mypy --strict` → 84 个源文件零错误；`ruff` → 全绿
- 真实网络冒烟：`https://example.com` → status 200 + 正文；
  `http://169.254.169.254/latest/meta-data/` → `[拒绝] 不是公网地址`

---

## 2026-09-20（第五批：RAG 接线 + 记忆落盘）

### 已提交

- **RAG 接进产品路径**。此前 `rag/` 只被 `demo_e2e.py` 与 `rag/eval.py` 引用——
  `build_agent` / `build_app` / `run_server` **没有任何地方构造 `VectorStore`**，
  `rag/__init__.py` 甚至是 0 字节。所以向量库、检索、来源引用、检索质量评测全都在，
  但模型手里的工具清单里**没有 `knowledge.search`**：又一处"实现了没接线"
  （与前面修过的凭证、恢复同类；这一处是第四轮遗漏的）。
  - 新增 `rag/loader.py`：`build_knowledge(source)` 支持三种来源——
    `VectorStore` 实例（原样使用）/ `True`（内置离线语料）/ 目录路径（索引 `.md`/`.txt`）。
  - `augment_catalog` 新增 `knowledge` 参数并注册 `make_knowledge_tool`；
    `build_agent` / `build_app` 同步透传；`run_server` 新增 `WARDEN_KNOWLEDGE=1|<目录>`。
  - `rag/__init__.py` 补齐导出（此前为空文件）。
  - **默认嵌入器仍是离线词频哈希（词面匹配，不是语义检索）**——这一点在启动日志里
    明确打出嵌入器名，避免"在词频嵌入下宣称语义检索"。真语义走 `WARDEN_EMBED_*`。
  - 新增 12 条测试（`tests/test_rag_wiring.py`）：三种来源、目录索引跳过非文本/空文件、
    注入库原样使用、`/capabilities` 可见、**端到端验证模型真能调 `knowledge.search`
    并在观察里拿到带来源的资料**、环境开关解析。

- **记忆落盘**。`MemoryRepository` 协议此前只有 `InMemoryMemoryStore` 一个实现，
  而 `augment_catalog` 固定用它——后果是 `USER` 作用域（"跨会话的用户级记忆"）
  在语义上成立、在实现上落空：进程一退就没了。
  - 新增 `SqliteMemoryStore`（`memory/store.py`）：实现同一套接口（save/find/find_ref/
    latest/search），语义与内存版对齐（含"latest 优先有效项"）；审计轨迹、冲突字段、
    过期时间一并落盘。
  - `augment_catalog` 新增 `memory_repository`；`build_agent` / `build_app` 透传；
    **`run_server` 默认改用 `SqliteMemoryStore`**，并记 `extra["memory_persistent"]`
    标明是哪种实现。
  - 新增 10 条测试（`tests/test_memory_persistence.py`）：存储语义、**换新实例读同一库
    数据仍在**（模拟重启）、候选流落盘、审计落盘、HTTP `/memory/user` 能读到落盘的记忆。

- **文档**：README 的 RAG / 记忆两行状态改准（并补上"词面匹配不是语义检索""召回是关键词
  重叠"两条诚实说明）；`.env.example` 加 `WARDEN_KNOWLEDGE` 与 `WARDEN_EMBED_*`；
  `docs/deployment-boundaries.md` 补记忆在多副本下的注意项。

- **检索评测报告改为"逐条排名在前"**（`rag/eval.py`）：聚合数字会掩盖"靠运气命中"——
  实测那条改说法的问法（"我想请几天假出去玩，有什么规定"，期望"15 天"）**正确片段相似度为
  0.000、排第 3**，只因 7 块库里 k=3 一次返回 3 块（覆盖 43%）才被顺带捞回；
  而 top-1 是完全无关的"出差交通"。现在报告会打印每条问法的首现排名，并在
  **k 覆盖率 ≥25% 时明确标注 `recall@k` 被小库抬高、不代表检索器强**（新增 `k_coverage` 属性）。
  拿了"recall@3 100%"去对外说会被一句"你库多大"戳穿，这个提示是防这个的。

- **修正一处与代码不符的注释**：`rag/knowledge.py` 模块 docstring 原写"检索用余弦相似度，
  纯 numpy 实现"——但代码是纯 Python（`math.sqrt` + 列表推导），pyproject 里也没有 numpy。
  已改为与实现一致，并补上真实描述（暴力全扫 O(N)、点积即余弦因向量已归一化、
  进程内不落盘、4096 维 Python float 单条约 130KB）。

### 尚未实现（路线图）

- **默认 RAG 仍是词面匹配**：要跨过"换个说法就掉分"，需配真语义嵌入端点
  （代码路径已备好，只是默认不联网）。
- **向量库是进程内、全量扫描**：`VectorStore` 把 chunk 放在 list 里线性算余弦，
  没有持久化、没有 ANN 索引。大语料需要换真正的向量库（FAISS/pgvector 等），
  当前实现适合中小知识库与离线演示。
- **记忆召回是关键词重叠**，不是向量召回；`USER` 作用域虽已落盘，但多副本并发写
  同一 key 的冲突消解只做了版本/状态层面，没有分布式锁。

### 验证

- `pytest` → **512 passed, 2 skipped**（共 514 项；无 node 的机器是 511 passed / 3 skipped）
- `mypy --strict` → 84 个源文件零错误；`ruff` → 全绿
- 冒烟：`WARDEN_KNOWLEDGE=1` 起服务，`/capabilities` 报出
  `tools=['knowledge.search', 'memory.recall', 'memory.remember']`，
  `features.knowledge_embedder='offline-term-frequency'`、`memory_persistent=true`

---

## 2026-09-20（第四批：水平扩展 + 恢复工作进程）

### 已提交

- **支持多副本部署（水平扩展）**。此前幂等表、SSE 事件总线、限流计数都是**进程内 dict**，
  起第二个副本就会出现：同一 `Idempotency-Key` 打到不同副本重复执行、客户端连副本 A 而
  事件产生在副本 B 收不到、实际限额 ≈ 配置值 × 副本数。现在把这三样抽成可插拔实现
  （新增 `web/coordination.py`：`IdempotencyStore` / `EventBus` / `RateLimitStore` 三个协议）：
  - **单副本**（默认）：`InProcess*` 实现，行为与历史一致、零延迟；
  - **多副本**：`WARDEN_SHARED_STATE=1` → `Sql*` 实现，复用 `RunStore` 的共享表
    （新增 `idempotency` / `run_events` / `rate_limits` 三张表 + 对应接口方法，
    SQLite 与 PostgreSQL 都实现；Postgres 一并补齐了此前缺失的 `checkpoints` 支持）。
  - `SqlEventBus` 用轮询增量读事件表（默认 250ms），不引入 Redis 等新依赖；
    对延迟敏感可换 Redis/NATS，只要满足 `EventBus` 协议。
  - 新增 10 条测试（`tests/test_shared_state.py`）：**同一个 store 上起两个 app 实例**，
    验证幂等只执行一次、限流总额度不翻倍、事件能被另一副本订阅到；并配了
    `shared_state=False` 的**对照测试**，如实暴露"不共享会怎样"。

- **恢复工作进程：计划真正被执行**。此前 `RecoveryController` 只判断不执行，
  重启后一堆 run 实际没人管。现在：
  - `AgentSession.resume()`：从已存状态继续一次未完成的运行（不追加新用户输入）。
    **安全边界**：对 `WAITING_APPROVAL` / `WAITING_INTERACTION` / `SUSPENDED` 抛
    `RunNotResumable`——自动续跑等待审批的 Run 等于绕过人工闸门；对已完成/已取消的
    Run 也拒绝（否则是重复执行）；只有 `FAILED` 允许重试。
  - `RecoveryWorker`（新增 `runtime/worker.py`）：按计划续跑 / 重试 / 等人 / 跳过，
    单个 Run 失败不拖垮整轮；`run_once()` 跑一轮、`run_forever()` 守护形态。
  - **`attempts` 归位**：重试计数改由 `CheckpointManager` 持有并写进每个存档点
    （原来只在 `Checkpoint` 上占个字段，没人维护；会话重启后计数会归零，重试上限形同虚设）。
    现在 `resume()` 在 FAILED 分支 `attempts += 1` 并随存档点写回。
  - 入口：`warden recover --apply`（本地跑一轮）；默认 `warden recover` 仍只打印计划。
  - 新增 11 条测试（`tests/test_worker.py`）：崩溃后续跑、完成态拒绝、**等人工不自动续**、
    FAILED 重试且 attempts 递增、超上限跳过、单 run 失败隔离、CLI `--apply`。

- **`build_agent` 补上存档点**：SDK 路径此前也没传 `checkpoint_store`，同样不写 checkpoint。
  新增 `runtime/checkpoint.py` 的 `checkpoint_store_for(store)` 作为统一适配入口，
  SDK 面与产品面共用一份判断（不再各写一遍）。

- **文档**：`docs/deployment-boundaries.md` 从"多副本不支持"改写为"单副本/多副本两种形态"，
  列出协调状态对照表、多副本部署要点（PostgreSQL、共享开关、凭证密钥统一、会话粘性）；
  README 同步；`.env.example` 加 `WARDEN_SHARED_STATE`。

### 尚未实现（路线图）

- **会话粘性仍是部署侧责任**：`SessionRegistry` 是纯缓存（会从库重建，正确性没问题），
  但同一 `run_id` 被两个副本**同时**驱动会出现后写覆盖前写。要彻底解决需在存储层加
  Run 级分布式锁（或其上层的单会话单副本路由）。当前给的是"建议做粘性路由"，不是强制。
- **`SqlEventBus` 是轮询实现**：延迟约等于轮询间隔（默认 250ms）。低延迟场景应换
  Redis Pub/Sub 或 Postgres LISTEN/NOTIFY 实现（协议已备好，实现未做）。
- **工具稳定性层的熔断状态仍是进程内**：每个副本各自熔断，不具全局语义。

---

## 2026-09-19（第三批：补齐"实现了没接线"与多租户/扩缩容口径）

### 已提交

- **多租户从"按字段过滤"变成真边界**（本轮最大的一处安全修正）。此前 `user_id` 来自
  客户端查询参数（`?user_id=`），且 `RunOperationAuthorizer` 建的时候**没传回调 = 全放行** ——
  拿到一把 key 就能读写、删除他人会话、替他人审批高危操作，README 的"多账号 USER_ID 隔离"
  只是前端过滤器。现在：
  - **身份由凭证决定**：新增 `WARDEN_API_KEYS=alice:k1,bob:k2`（每个 key 绑定一个用户），
    认证模式下端点一律用 `caller.user_id`（= `principal_id`），**查询参数被忽略**。
    `TrustedCaller.user_id` 把这个约定显式化（`web/auth.py`）。
  - **按归属授权**：新增 `owner_authorizer(load_owner)`，对**已存在且已归属**的 Run，
    调用者与归属不符即 403。覆盖 chat / status / messages / events / runs / approve / reject
    （`_extract_run_id` 同步补上 `/runs/{id}`、`/messages/{id}`——少覆盖一条，归属校验在那条路上
    就等于没开）。
  - **列表类接口按调用者收敛**：`/runs`、`/approvals`、`/approvals/history`（`list_approval_history`
    四个实现统一加 `owner` 参数）、`/users`、`/audit`（按 `tenant_id`）。
  - **不改坏本地演示**：匿名开发模式（未开鉴权）保留 `?user_id=` 旧行为。
  - 新增 14 条测试（`tests/test_web_tenancy.py`）覆盖身份派生、读/写/删/审批四类越权、
    列表收敛、审计租户隔离，以及"匿名模式不受影响"的对照组。

- **跨 Run 恢复真正接线**（此前 `CheckpointManager` / `RecoveryController` / `SqliteCheckpointStore`
  **全仓只在测试里出现**）。根因比"缺入口"更深一层：**产品路径从不写 checkpoint**，
  `checkpoints` 表永远是空的，所以恢复控制器即使接上也是空转。现在：
  - `AgentSession` 在 `model_call` / `tool_exec` / `awaiting_approval` / `done` 四个点落存档点
    （非流式与流式两条路径都埋）；写失败降级为告警，不成为主路径单点。
  - 新增入口：`GET /recovery/plan`（认证模式下按归属过滤）与
    `warden recover [--db] [--owner] [--json]`（本地命令，只判断不执行）。
  - 新增 9 条测试（`tests/test_recovery_wiring.py`）：存档步骤序列、等待审批归入 `awaiting_human`、
    终态归入 `terminal`、端点与 CLI 输出。

- **凭证模块真正接线**（此前 `CredentialBroker` / `SecretRedactor` 全仓零引用，
  而 README 把"凭证 AES-GCM 加密 + 脱敏"列为防御能力）。现在：模型的 API Key 经
  `CredentialBroker` **加密保管 + 短租约**（不再明文躺在 `imported_keys` dict 里），
  `SecretRedactor` 对网关异常日志脱敏并挂到 `app.state`。
  新增 `default_broker()`：`WARDEN_CREDENTIAL_KEY` 给密钥材料，未配置则退化为**进程内临时密钥**
  并**明确告警**（不制造"已持久化加密"的假象——原来的 `derive_key_from_env` 会直接抛错，
  等于该模块永远无法在默认配置下启用）。

- **新增请求限流**（可用性这条线此前完全空白）：`web/ratelimit.py` 进程内固定窗口限流器，
  默认每调用者 60 秒 600 次（`WARDEN_RATE_LIMIT=次数/秒数`，`0` 关），429 + `Retry-After`，
  健康探针豁免，拒绝计入 `warden_rate_limited_total` 指标。新增 23 条测试。

- **口径文档**：新增 `docs/deployment-boundaries.md`——明确单节点前提、**进程内状态清单**
  （会话缓存 / SSE 总线 / 幂等表 / 限流桶 / 凭证密钥 / 熔断状态）及各自的多副本后果与扩展接缝，
  并说清"多租户仅在鉴权模式成立、匿名模式不是安全边界"。README 同步收敛相关表述。

- **仓库卫生**：删除根目录误操作留下的空目录 `pyproject.toml;C`。

- **文档对齐**：README 测试数 379 → **465**；工具稳定性层的"独立模块（未接入主链路）"是过时描述
  （上一批已接线），一并更正；架构图该虚线标注同步更新。

### 尚未实现（路线图）

- **水平扩展仍不支持**：幂等表、SSE 事件总线、`SessionRegistry`、限流计数都在进程内，
  多副本部署会让幂等/事件流/限额各自为政。接缝已在 `docs/deployment-boundaries.md` 列出
  （改共享存储 / Redis 总线 / 网关限流），本轮**未实现**，只把边界写清楚。
- **RecoveryController 仍只判断不执行**：没有内置常驻 worker 去真正续跑，
  计划由 `GET /recovery/plan` / `warden recover` 输出，执行交调用方。
- **凭证的租约 TTL 未与模型生命周期绑定**：模型实例构造后自身持有密钥，
  租约过期不会让已构造的模型失效（租约的价值在"保管与审计"，不在"运行时收回"）。

---

## 2026-09-19（第二批：认知能力接进产品路径）

### 已提交

- **阶段规划 / 意图路由 / 记忆按需取用 / 上下文裁剪，四项接进会话路径**（HTTP / CLI / 流式）。
  此前这四项只存在于 `AgentLoop`（demo / 评测 / 多 Agent 用），会话侧 `AgentSession` 另有一套
  循环、**一项都没有** —— 所以 README 里"会思考的认知循环"在**产品路径上并不成立**。
  - 修法不是把两套循环合并（会话侧还要状态机 / 审批挂起 / 流式，合并风险高），而是把这四步抽到
    **`loop/cognition.py`**，两套循环**共用同一份实现** —— 一套实现、两个调用方，行为不会再分叉。
    同时把 `AgentLoop` 的内联实现改为调用它（删掉本地重复的 `_summarize` / `_tokens` / `_KEEP_RECENT`）。
  - 产品入口的默认值按"是否多花一次模型调用"来定：
    **意图路由**（`ToolIntentRouter`，纯离线确定性）与**上下文裁剪**（纯收益）**默认开**；
    **阶段规划**（`ModelPlanner` 会多花一次模型调用）**默认关**，`WARDEN_PLANNER=1` 打开。
  - 记忆 / 计划是**每次请求临时注入**，**不写进持久化存档** —— 否则恢复会话时会反复叠加。
  - 新增 7 条测试（`tests/test_session_cognition.py`）：记忆注入、不相关记忆不注入、计划注入、
    **意图路由真的拦下误调**（工具执行次数为 0）、**对照组**（不接 intent 时同样的输入会执行，
    证明前一条确实有效）、上下文裁剪、注入不污染存档。

### 尚未实现（路线图）

- **RecoveryController 缺一个可执行入口**：只做"读 checkpoint 并分组"的判断，无 worker / 无 CLI。
- **凭证加密的密钥仍存进程内、未落库**（AES-GCM 是真的，但加解密的对象活在内存里，
  进程一退就没了 —— 落库才让"加密"这一步真正起作用）。

---

## 2026-09-19（把"实现了但没接线"接上）

### 已提交

- **工具稳定性层接入产品路径**（这是本轮最大的一处"说了没做到"）。此前 `exec_tool` 与
  `AgentSession` 都接受 `stability`，但 **`build_agent` / `build_app` 从没构造或传过它**，
  全仓只有一句注释提到它 —— 所以"每次工具调用必经的管卡"当时并不成立。
  现在：给 `build_agent` 与 `build_app` 加 `stability` 参数（`None`/`False`/`True`/
  `StabilityConfig`/执行器实例都接受），`run_server` **默认开启**（`WARDEN_STABILITY=0` 关）。
  解析逻辑放在 `tool/stability.py` 的 `build_stability_executor`，避免 SDK 面与产品面互相依赖。
  **重试是安全的**：稳定性层按 `ToolSpec.pure` 判定，非纯工具只对瞬时错误重试，
  不会把 `fs.delete` 这类操作重放。
  新增 5 条行为测试（`tests/test_agent_stability_wiring.py`），含**一条走 HTTP 的**，
  证明接的是产品路径而不只是 SDK 门面。
- **语义嵌入端点加 URL 守卫**。`openai_compatible_embedder` 此前会向配置的任意地址发请求 ——
  这是标准的 SSRF 面。现在：只允许 http/https；**校验解析后的每一个 IP**，拒绝环回 /
  私有 / 链路本地 / 保留 / 多播 / 未指定；构造时就校验一次（fail fast），每次请求前再校验
  一次（防 DNS rebinding）。**有意不支持"本机嵌入服务"**（Ollama / 本地 vLLM 走 localhost）
  ——允许环回就等于把这个接口变成 SSRF 原语；需要本地嵌入时请直接注入自己的 `Embedder`。
  新增 7 条**离线确定性**测试（用 monkeypatch 注入解析结果，不查真 DNS），含防重绑定与
  "多解析结果里有一个内网就整体拒绝"。
- **前端支持 Bearer 鉴权**。此前前端**完全不发 `Authorization` 头** —— 一旦设了
  `WARDEN_API_KEY`，控制台就整体不可用（这是真实缺口）。现在 `api.ts` 统一附加请求头
  （非流式与 SSE 两条路径都覆盖），key 存 localStorage，顶栏加了一个 `KEY` 输入框
  （password 类型遮显，回车/失焦即生效）。已重建前端并**截图确认渲染正常**。
- **React 控制台完成首次实际打开验证**：界面正常，且连的是真实数据（左侧 5 条历史会话带
  时间戳与消息数、能力列表、模型列表显示 DeepSeek 使用中、审批历史显示一条已批准的
  `fs.delete`）。此前"能构建进镜像但没点过界面"的存疑状态解除。

### 尚未实现（路线图）

- **RecoveryController 缺一个可执行入口**：`runtime/recovery.py` 只做"读 checkpoint 并分组为
  resume / retry / skip / await_human"的**判断**，没接 worker / 守护进程，也没有 CLI 命令。
- **规划 / 意图路由 / 记忆自动召回仍不在 HTTP / CLI 路径上**：会话侧 `AgentSession` 自带循环，
  与 `AgentLoop` 只共享 `exec_tool`；"会思考"那条路径目前由 demo、评测与多 Agent 使用。
- **凭证加密的密钥仍存进程内、未落库**。

---

## 2026-09-18

### 已提交

- **修复 dev 分支的静态检查红灯**:`mypy --strict` **11 → 0**、`ruff` **4 → 0**（此前 CI 只覆盖
  `master`,`dev` 上的红灯无人拦截）。
  - 删除 `web/server.py` 中**重复定义的 `create_run` 路由**（第 660 行的旧版本从未生效；
    同时消除 mypy `no-redef` 与 ruff `F811`）。
  - `runtime/session.py` 补齐 `ChatResponse` 导入（修 `F821` / `name-defined`）。
  - `web/search.py` 收口 IP 类型标注:`ipaddress._BaseAddress`（私有 API）→
    `IPv4Address | IPv6Address`,并为 `resolver` 参数补全注解（修 6 + 2 个 mypy 错）。
  - `store/postgres.py` 超长行折行（修 `E501`）。
- **补全 `InMemoryRunStore` 对 `RunStore` 接口的实现**。它此前**缺少
  `record_approval_decision` 与 `list_approval_history` 两个方法**,与自身 docstring
  「完全实现 RunStore 接口」不符——这正是 `agent.py` 那条 mypy 报错的根因
  （用 `cast` 只能掩盖真问题,不能解决它）。
  - 同时给 `list_runs` 补上 `owner` 参数（多用户过滤）,`RunStore` Protocol 也补上 `owner`。
    此前 `web/server.py` 调用 `list_runs(limit=50, owner=...)`,一旦换用内存存储会 `TypeError`。
- **CI 覆盖所有分支**:`push.branches` 由 `[master]` 改为 `["**"]`,避免工作分支的回归再次
  无人拦截。
- **README 与代码对齐**（只改陈述,不改行为）:
  - **工具稳定性层**:删去「是调用前后必经的管卡,不是可选的示例代码」,如实标注
    **独立模块、尚未接入调用链**（全仓只有 `loop/loop.py` 的注释提到接入方式）。
  - **架构层次说明**:删去「将规划执行委托给 `AgentLoop`」——会话侧自带循环,与 `AgentLoop`
    **共享 `exec_tool`** 但不走其规划 / 意图路径;架构图中该依赖改为虚线「接入点（未接线）」。
  - **凭证**:删去「落库加密」——密钥保存在进程内 `dict`,未落库。
  - **数字对齐**:测试 309 → **325 passed / 1 skipped**（共 326 项）;源文件数 74 → **77**
    （`mypy` 实测）。

### 尚未实现（路线图）

- **工具稳定性层接入主链路**（给工具执行器显式传入 `StableToolExecutor`）。
- **会话路径复用 `AgentLoop` 的规划 / 意图路径**（目前规划 / 意图 / 记忆自动召回只在
  demo / 评测 / 多 Agent 中生效）。
- 凭证加密**落库**（当前仅进程内）。
- `web/search.py` 的 SSRF 加固（私有段 / 元数据主机名 / 传统 IP 简写 / DNS rebinding 校验）
  **尚未接入真实联网 provider**（当前内置 provider 均离线）。

---

## 2026-09-18（第二批：加固落地）

### 已提交

- **鉴权改为 fail-closed（入口层）**。原来不设 `WARDEN_API_KEY` 就静默开放全部接口——
  而 `/approve`、`/reject` 是人工审批闸门的入口，接口无鉴权时调用方可以自己批准自己的
  高危操作，门禁形同虚设。现在：
  - `resolve_auth()`：没 key 又没显式 `WARDEN_ALLOW_ANON=1` → **拒绝启动**；
  - `ensure_listen_is_safe()`：对外监听（如 `0.0.0.0`）却不带鉴权 → 也拒绝启动；
  - 空字符串的 key 不算 key（避免 `.env` 里留个空值就悄悄裸奔）。
  `build_app(api_keys=None)` 的"显式开放"语义保留不变（那是应用层，入口层负责把关）。
- **修 Docker 部署连不上的问题**：容器内 uvicorn 原先绑 `127.0.0.1`，发布端口转发不到容器网卡。
  现在用 `WARDEN_HOST` 控制（容器里给 `0.0.0.0`），并因为"对外监听"与"无鉴权"不允许共存，
  由上面的 fail-closed 校验兜住。
- **容器加固**：`Dockerfile` 加**非 root 用户**与 `HEALTHCHECK`；`docker-compose.yml` 加
  `read_only` / `cap_drop: ALL` / `no-new-privileges` / 只发布到回环 / `${WARDEN_API_KEY:?}` 强制设 key。
  另外把 PostgreSQL 服务注释掉——它本来就没人连，起一个这样的服务比不起更误导（改用
  `PostgresStore` 时再打开）。新增 `WARDEN_DB_PATH` 以支持只读 rootfs。
- **执行沙箱：把"隔离"分档，并让它可以被验证**（`execution/sandbox.py`）。
  - 语义档（跨平台）：只读副本 + NetworkPolicy 正则——**它不是安全边界**，
    新增测试 `test_语义层拦不住_socket_一句话就绕过去` 用可执行证据说明这一点。
  - 内核档（Linux + `unshare -rn`）：子进程进入独立网络命名空间，没有任何网络栈；
    新增测试 `test_内核档下网络调用真的失败` 跑真实 socket 连接并断言失败
    （仅 Linux 执行，CI 的 ubuntu 上会真的跑）。
  - `resolve_isolation_tier()`：要求内核档而平台给不了时**报错，不静默降级**；
    `isolation_note()` 如实报出当前档位。前缀可用 `WARDEN_ISOLATION_PREFIX` 覆盖
    （想换 bwrap 等更强方案时用）。
- **评测升级为"循环能力评测"**（`evals/runner.py`）。原先 e2e 的判定是
  `bool(reply.text) and len(reply.text) > 0`（等于没测）。现在 6 例 → **10 例**，断言落在
  **轨迹与决策**上：单工具参数保真、多步顺序、失败自愈、意图门禁、防打转、工具异常不击穿、
  策略 DENY 不执行、迭代上限收口、tool_call_id 配对不变量。
  并在模块 docstring 里写明边界：**脚本模型测的是 harness 能力，不是模型能力**；
  提示注入那类必须用真实模型，拿脚本模型测只是剧场。
- **RAG 去玩具化**（`rag/`）。三件事：
  1. **定位到真因**：新增检索质量评测 `rag/eval.py`（标注问答集 → top-1 / recall@k / MRR），
     一测就发现"查'报销'检索不到报销那段"**不是语义问题，是哈希碰撞**。
     实测 dim=256 → top-1 14.3%；**dim 提到 4096 → top-1 85.7% / recall@3 100% / MRR 0.905**。
  2. **修 `source_id` 不唯一**：同一份文档下的多个条款会生成相同 source_id，引用无法区分；
     现在带全局块序号（`员工手册.pdf#2`）。
  3. **补真语义嵌入接口**：`openai_compatible_embedder()` + `embedder_from_env()`
     （配 3 个环境变量即切换），并把当前嵌入器**名字**打到 demo 与报告里——
     避免在词频嵌入下宣称"语义检索"。
     哈希算法 MD5 → SHA-256（此处只用于分桶，MD5 不算漏洞，但没必要给扫描器留告警）。
- **删除遗留物** `timeline_callback.py`（改 git 提交时间的脚本，无任何引用）。
- **测试**：325 → **379 项**；源码 77 → 79 个文件；评测集 26 → 30 例。

### 尚未实现（路线图）

- **工具稳定性层接入主链路**（给工具执行器显式传入 `StableToolExecutor`）。
- **会话路径复用 `AgentLoop` 的规划 / 意图路径**（目前只在 demo / 评测 / 多 Agent 中生效）。
- 凭证加密**落库**（当前仅进程内）。
- **模型能力评测**（`evals --mode real`）：用真实模型测提示注入抵抗、指令遵循、幻觉率——
  这些用脚本模型测不了。
- **检索升级到语义 + 重排**：现在有了评测集，接真语义嵌入后可直接对比数字；
  再往后是混合检索（BM25 + 向量）与 reranker。
- 内核档隔离的**文件系统**维度（当前只隔离网络；文件系统仍靠只读副本 + 容器边界）。

---

## 2026-09-18（第三批：真机实测发现并修掉两个真 bug）

这一批全部来自**在真实 Linux / 容器里跑一遍**——不跑就发现不了。

### 已提交

- **Dockerfile 根本构建不出来（已修）**。根因是层缓存写法踩坑：`pip install .` 那一层
  只 `COPY pyproject.toml`，而 `pyproject.toml` 里有 `readme = "README.md"`，
  hatchling 生成元数据时直接 `OSError: Readme file does not exist: README.md`。
  **也就是说容器路径从未真正跑通过**（这也解释了之前发现的 compose 端口绑定 bug 为什么没人察觉）。
  修法：连 `README.md` 一起拷；把"只装运行依赖"和"装本项目自身"拆成两步，
  层缓存的意图保住；新增 `ARG PIP_INDEX_URL`，国内网络可传 pip 镜像，且不把镜像地址烧进镜像。
- **隔离档探测改为「功能探测」而非「查文件是否存在」**。原来只用 `shutil.which("unshare")` 判断——
  但**"有这个二进制" ≠ "有权用它"**：在默认 Docker 容器里 `unshare -rn` 会
  `Operation not permitted`（实测）。只看路径就会汇报成"已隔离"，实际什么都没隔离。
  现在会真的试跑一次 `unshare -rn true`，失败则**如实降级到语义档**，并给出可操作提示
  （`需 --cap-add SYS_ADMIN`）。**宁可少报一档，不谎报一档。**
- **修掉一个测试的「假通过」**。内核档那条测试原本断言"stdout 里没有 CONNECTED"——
  可万一 `unshare` 因权限失败，探针压根没跑、stdout 为空，这种断言照样绿。
  现在要求：① 退出码为 0（隔离环境真的起来了）② 打印出明确的 `RESULT=BLOCKED` 标记。

### 实测记录（这些数字是跑出来的，不是推断）

| 环境 | 结论 |
|---|---|
| WSL2 / Linux 6.6.87 | `unshare -rn` 后 `ip -o link show` **只剩 lo**，eth0 消失 → 命名空间确实建立 |
| 容器（默认档） | `unshare -rn` → `Operation not permitted`；档位探测**如实报** semantic-only |
| 容器（`--cap-add SYS_ADMIN`） | `IFACES=eth0,lo` → `IFACES=lo`；连接 `ok` → `fail 101 (ENETUNREACH)` |

**一个重要结论**：内核档隔离与容器边界**默认互斥**——在容器里用内核档需要
`--cap-add SYS_ADMIN`，而那本身会削弱容器的隔离。所以**二选一**：
要么用容器当边界（推荐，`network_mode: none` + 只读 rootfs），
要么用内核档跑在宿主/CI 上。别以为两者能简单叠加。

### 构建与容器实测（全部通过）

重跑构建**成功**。此前那次 pip 拉包超时是**瞬时故障**：Docker Desktop 刚启动、
它自带的代理桥（`http.docker.internal:3128`）还没就绪。

> **更正**：本文件上一版把原因写成"疑似 Windows localhost 代理未镜像进 WSL NAT"，
> 那是**错的**。实查结果：宿主系统代理开着（`127.0.0.1:7897`），而 Docker Desktop
> 本身就提供代理桥，宿主代理能透进容器；容器内 DNS 解析到 `198.18.0.50`（代理的
> fake-ip 段），`https://pypi.org/simple/` 正常可达。所以既不是"梯子没关"，
> 也不是"代理没透进去"——只是时机问题。

在真实镜像上逐项验证：

| 检查项 | 结果 |
|---|---|
| 运行用户 | `uid=10001(warden)` —— 非 root ✅ |
| 不设 `WARDEN_API_KEY` 启动 | 拒绝启动、退出码 2、给出可操作提示 ✅ |
| `/health/live` | 200（公开，免认证）✅ |
| `/audit` 不带 key / 错 key | 401 / 401 ✅ |
| `/audit` 带正确 Bearer | 200 ✅ |
| `HEALTHCHECK` | `healthy` ✅ |

### 尚未实现（路线图）

- 内核档**只隔离网络**，不隔离 /proc 视图（`--mount-proc` 需要 PID namespace，
  会与"超时强杀"的进程管理语义冲突，故刻意不加）。

---

## 2026-09-04

### 已提交

- **初始化发布**:项目从 haifa-agent-py 独立重构并改名为 **warden-agent**。
  - 包名/目录:haifa_agent → warden_agent
  - 环境变量/库名/URN/HTTP 头:HAIFA_* → WARDEN_*(warden-agent-local.db、WARDEN_API_KEY、X-Warden-Api-Version 等)
  - MIT License、Docker、pyproject、.env.example 就绪
- **删除公开仓库里的复刻向学习笔记**(docs/ 下 4 份:演进路线、全文件详解、面试准备指南、项目完整流程)——这些是个人自用学习材料,保留在本地桌面,不进公开仓库。
- **全项目清除旧名/出处引用**(haifa / 原版 / 对标 Java),注释与品牌统一为 Warden。
- **测试**:常见 162 passed, 1 skipped(Postgres 集成测试无库自动跳过)。
- **运行验证**:HTTP 服务启动、/health、/chat(真实 DeepSeek)、审批门禁(WAITING_APPROVAL→reject)、离线假模型均实测通过。
- **T3 CLI 命令行入口(warden)完成**:
  - 子命令:`warden chat` / `stream` / `approvals` / `approve` / `reject` / `health` / `caps`
  - 复用已有 HTTP 端点;默认连 127.0.0.1:8000,可用 `WARDEN_BASE_URL` 覆盖
  - 实测:chat 触发审批 → approvals 看队列 → approve/reject 决策,全过程跑通
  - 修复两个真实 bug:① httpx 默认走系统代理导致 127.0.0.1 被代理转发返回 502(改 trust_env=False);② chat 子命令残留死代码引用了不存在的属性导致 AttributeError
  - 新增 `tests/test_cli.py`(4 项)。全量测试 **166 passed, 1 skipped**
  - 新增 `[project.scripts] warden = "warden_agent.cli:main"`(pyproject)
- **T5 SDK 化(README 三行接入)完成**:README 开头新增"三行接入"小节,展示 `pip install` + `build_agent()` 一行装配 + `chat()` 的库用法;`pip install -e .` 已验证可安装、可 import。
- **T2+T7 HTTP contract 完成**:
  - **统一 API 版本头**:所有 HTTP 响应统一注入 `X-Warden-Api-Version: 1.0`(中间件层统一加,不只 problem 响应)
  - **幂等 Idempotency-Key**:POST 请求带 `Idempotency-Key`,同 key 重复请求返回同一份缓存结果(不重跑);5xx / SSE 流式响应不缓存
  - **统一错误码契约**:`API_ERROR_CODES` 收敛 400/401/403/404/409/500/503,problem+json 响应含 `errorCode` 字段(如 `AUTHENTICATION_REQUIRED`)
  - **多 run 幂等(T7)**:`SessionRegistry` 按 run_id 复用会话,已 COMPLETED 的 run 不重复初始化/重跑,配合 Idempotency-Key 幂等
  - 新增 `tests/test_web_contract.py`(6 项)。全量测试 **172 passed, 1 skipped**
  - 过程中修复幂等缓存的实现(中间件拿到的 body 是异步流,需 `async for` 消费并重建响应)
- **T4 Coding Agent 雏形完成**:
  - 新增 `coding_agent/` 模块:`run_coding_task()` 给需求 → 读代码 → 出 unified diff → 走 `git.apply_patch` 门禁落地(不自动 commit/push)
  - 新增两个**受限代码浏览工具**:`code.list`(列目录) / `code.read`(读文件,64KB 上限),均做 workdir 边界校验(防 `..` 逃逸/绝对路径读取 workdir 之外)
  - 复用 `build_agent` + `git/`(make_git_tools、GitWorktreeCoordinator)已有能力,不重复造轮子
  - 接入 CLI:`warden coding "<需求>" --workdir <仓库>` 本地跑(不需 HTTP 服务)
  - 新增 `tests/test_coding_agent.py`(5 项,含路径穿越安全测试)。全量测试 **177 passed, 1 skipped**
- **T1 执行沙箱/隔离完成**:
  - 新增 `execution/sandbox.py`:`SandboxSpec` / `NetworkPolicy` / `SandboxedExecutionBroker`
  - **只读工作区**:把输入目录拷进临时工作区并设只读,命令跑在副本上,改不到宿主,跑完即弃
  - **网络策略 NetworkPolicy(默认禁网)**:默认拒绝 curl/wget/ping/nc/http/urllib 等疑似网络命令;`allow_network=True` 放行
  - **资源限制(内存/CPU/文件)**:在 `ExecutionBroker.execute` 造子进程时按平台应用真实限制——POSIX 用 `resource.setrlimit`(RLIMIT_AS 内存 / RLIMIT_CPU / RLIMIT_NOFILE),Windows 用 Job Object(pywin32,限进程内存/JOB_TIME/活动进程数),避免 fork 子进程逃逸
  - 诚实标注边界:这是"应用层资源限制"(跨平台可用),非内核级 cgroup / OS 沙箱(bubblewrap / Seatbelt)
  - 新增 `tests/test_sandbox.py`(9 项,含资源限制、路径穿越安全、make_limiter 触发)。全量测试 **186 passed, 1 skipped**
- **T6 跨 run 协调恢复完成**:
  - `SqliteStore.list_checkpoints()`:枚举所有 run 的存档点(抽出统一的 `_decode_checkpoint()` 解码器,供单查/枚举复用)
  - `SqliteCheckpointStore.list()`:接上底层枚举,为协调恢复提供"看全部存档"的能力
  - 新增 `runtime/recovery.py` 的 `RecoveryController`:读全部 checkpoint 按状态机分组——终态跳过 / RUNNING 等续跑 / FAILED 重试/判终态 / 等审批·交互·暂停的等人工
  - `Checkpoint` 新增 `attempts` 字段(默认 1,向后兼容),记录该 run 累计尝试圈数,`RecoveryController` 靠它决定"FAILED 是否还重试"(`max_attempts_per_run` 上限防无限循环)
  - 测试驱动修正:最初用"按 run_id 计 checkpoint 条数"估 Attempts 是错的(store 按 run 覆盖写,每 run 恒为 1 条),测试当场抓住,改为把 attempts 落在 Checkpoint 上
  - 新增 `tests/test_recovery.py`(7 项)。全量测试 **190 passed, 1 skipped**
- **T10 Web 前端产品化完成(React + TS + Tailwind)**:
  - 新增 `web/` 前端项目(Vite):组件化交互式控制台——`ChatView`(SSE 流式打字机 + 内联审批)、`ApprovalPanel`(审批队列)、`InfoPanel`(run 状态/能力/记忆/健康)
  - 对接后端全部接口:`POST /chat/stream/{run_id}`(SSE)、`/approvals`、`/approve|reject`、`/capabilities`、`/memory`、`/health`
  - FastAPI **单端口托管 SPA**:`server.py` 新增 `_find_spa_index()` 优先喂 `web/dist` 的 React 构建产物,并 `app.mount("/assets")` 托管静态 JS/CSS;未构建时自动回退旧版演示控制台
  - 开发模式:Vite(5173) 经 `vite.config.ts` 代理 `/chat`,`/events` 等到后端(8000),前后端分离热更新
  - Docker 多阶段构建:阶段1 node 构建前端 → 阶段2 Python 后端 + 放进 `/app/web/dist` 托管,单镜像部署
  - 修正两个路径 bug:`_repo_dir()` 向上 3 层定位仓库根(原多跳一层导致找不到 dist)、`..` 冗余;新增 `web/README.md`、更新 `.dockerignore`
  - 更新 `tests/test_web.py`(首页改返回构建产物 + 新增 `test_t10_react_spa_服务和资源`)。全量测试 **192 passed, 1 skipped**

## 2026-09-05

### 已提交

- **T8 可观测性(指标 + /metrics)完成**:
  - 新增 `core/metrics.py`:零第三方依赖、线程安全的进程内指标注册表,输出 Prometheus text 格式(Counter 计数器 / Gauge 瞬时值 / Histogram 耗时分布),`/metrics` 可被 Prometheus / Grafana 直接抓
  - `web/server.py` 网关中间件埋点:请求总数、耗时直方图、5xx 错误数;新增 `GET /metrics` 文本出口(不占用业务路由)
  - **收尾修复 3 个真 bug**(见私人文档「排查日志」):
    ① `histogram()` 原返回 None,调用方 `observe()` 直接 AttributeError → 改为返回 `Histogram` 调查柄,桶在注册时绑定
    ② `Gauge.inc/dec` 原"读-改-写"在无标签键上误写,`set→dec` 后键分裂、render 时 IndexError → 统一用锁内原子 `_add_gauge`
    ③ `observe` 分桶 `+Inf` 计数重复、`_sum` 恒为 0 → 修正桶边界 + 真实累加总和
  - **收尾过一遍质量门禁**:`metrics.py` + `server.py` 过 `mypy --strict` 与 `ruff`(全绿);`/approve|reject` 接入 `warden_approvals_total` 指标;清理掉"只声明未接线"的死指标(工具/策略/终态/活跃 run 度量的埋点留待后续在 loop 层做);顺手修 `_find_spa_index` 里一行既有 `open(..., "r")` 冗余模式参数
  - 新增 `tests/test_metrics.py`(3 项)。全量测试 **193 passed, 1 skipped**

- **planner/intent 接模型 + 工具自解释(loop 深度③⑤再上一档)**:
  - **loop 深度③ 阶段规划接模型**(`loop/planner.py`):任务复杂度由离线启发式判定为"复杂"后,
    阶段内容不再用固定模板,而是由**模型生成**(`plan_with_model` / `ModelPlanner`,走结构化输出要
    `{"steps":[{"title","goal"},...]}`);模型不可用 / 返回垃圾时自动降级回通用模板(离线可测)。
    复杂度判定仍用启发式(省一次调用、可三等),只有"复杂"才值得让模型细化阶段。
  - **loop 深度⑤ 意图判断接模型**(`loop/intent.py`):给 `ToolIntentRouter` 增加 `reasoner`——
    无触发信号时让**模型自己说明理由**,说合理就放行,否则维持提醒;不给 reasoner 则退回启发式,
    默认行为与离线测试行为不变。
  - **工具自解释增强(能力层"长"进系统)**:给 `ToolSpec` 增加 `triggers` 触发词元数据,
    新增 `tool/trigger.py` **从工具描述自动提取触发词**(英文词 + 中文相邻双字 + 过滤泛词),
    intent 路由与 skill 触发路由都从工具自身"长出"触发信号、不再手配映射表。
    `skill.trigger.pick` 暴露的工具也自带 `triggers` 元数据,让意图路由能识别"何时该触发技能路由"。
  - **skill 触发复用共享提取**(`skill/trigger.py`):匹配信号与意图路由同源(`tool.trigger.tokens`),
    英文词 + 中文双字 + 滤泛词,行为与原先等价。
  - 新增 `tests/test_tool_trigger.py`、`planner`/`intent` 增强测试;全量测试 **256 passed, 1 skipped**

- **能力层再上一档：技能版本化 + 多 Agent 共享记忆/容错降级/并行调度**:
  - **技能版本化**(`skill/skill.py`):`SkillCatalog` 由"别名→单绑定"升级为"别名→{版本→绑定}";
    `find(alias, version=None)` 不传版本默认取**最新版**(数字版本按数值比较,如 1.10 > 1.9);
    `load_skill` 同别名可登记多版本、不再静默覆盖;信任快照 `digest` 加入 version 使不同版本可区分;
    `load_skills_from_dir` 支持 `<alias>/<version>/SKILL.md` 版本目录约定。
  - **多 Agent 共享工作记忆**(`multiagent/supervisor.py`):新增 `MemoryScope.WORKSPACE`
    (一次协作工作区内共享);`share_memory(service, *agents, scope=WORKSPACE)` 把同一份
    MemoryService + WORKSPACE 注入各子 Agent,并挂 `memory.remember/recall` 工具,
    研究员写入→写手能读到(共享上下文、避免重复查)。
  - **多 Agent 容错降级**(`multiagent/supervisor.py`):`wrap_agent_as_tool` 增 `retries` /
    `fallback` / `degrade`:子 Agent 失败 → 重试 → 换备用专员 → 降级交接单,不再向主管层崩溃。
  - **多 Agent 并行/串行分派**(新增 `multiagent/dispatch.py`):确定性 `Dispatcher`,线程池真并行
    `run_parallel`(离线可测真并行耗时)或按依赖串行 `run_sequential`,不靠模型脑补。
  - 新增 `tests`:技能版本化(5)、WORKSPACE 作用域(3)、多 Agent 容错/共享记忆/分派(6);
    全量测试 **270 passed, 1 skipped**

- **工具调用全链路稳定性层**(新增 `tool/stability.py` + 接入 loop):
  - **超时护栏**:给工具调用设硬时限,卡死的工具不再同步卡死整个 loop(工作线程 + deadline,
    `future.result(timeout)`,到点返回超时信号并交还控制权)。
  - **指数退避重试**:瞬时故障(超时/ConnectionError/OSError)按 `backoff_base*2^(n-1)` 指数退避
    自动重试(上限 `backoff_max`),扛限流/抖动;pure(无副作用)工具额外可重试。
  - **统一降级兜底**:重试耗尽配了 `fallback` → 返回带 `[降级]` 标记的兜底结果;否则返回错误串。
  - **熔断保护(circuit breaker)**:连续失败达 `circuit_threshold` 次 → 短路期内直接返回 `[熔断]`
    信号、不调不重试(不再空转打上游);冷却 `circuit_cooldown` 后**半开试探**一次,成功即关闭、
    失败再打开;按工具名分桶隔离(一个工具挂了不连累别的)。默认 `threshold=0` 关闭。
  - **对齐项目约定**:结果带标志(非异常抛出,同 execution/broker)、复用 `ToolSpec.pure` 可重试信号、
    `StabilityConfig()` 默认全关(向后兼容,不配不改行为);`AgentLoop(..., stability=...)` 可选接入,
    loop 深度① 下游零改动。
  - 新增 `tests/test_tool_stability.py`(18 项:退避递增/重试成功/非瞬时不重试/pure 语义/超时不卡死/
    降级/默认关闭/loop 集成/熔断触发/熔断跳重试/半开恢复/半开失败再熔断/隔离)。全量测试
    **288 passed, 1 skipped**

- **去重复/收敛堆砌(审查驱动的一轮重构)**:
  - **统一 `PolicyDenied`** 到 `policy/policy.py`(loop/session 共用一份,消除跨模块 catch 不到隐患)。
  - **分词器收敛 4→1**:`loop._tokens`/`intent._words` 委托到 `tool.trigger.tokens()`(英文词+中文双字+滤泛词),
    intent 顺带补上中文识别(原版只匹配英文)。
  - **删冗余 `demo_full.py`**(是 demo_e2e 的严格子集、无测试);README/test_architecture 同步移除。
  - **两套主循环共享"工具执行"**:抽模块级 `exec_tool`(稳定性:超时/退避/降级/熔断 + 错误转字符串的单一来源),
    `AgentLoop._safe_execute` 与 `AgentSession._execute/stream` 都走它——**HTTP/流式/CLI 产品路径首次吃到深层工具稳定性**,
    工具失败改为喂回模型自纠(不再直接崩),保留 tool_call_id 配对/审批/持久化/流式。
  - **诚实标注"独立能力/未接入主链"**:凭证加密、Checkpoint·恢复、受控执行·沙箱是已写好但未接产品主链的能力,
    不再冒充"已实现",也不删除(留给以后接线),README 补说明。
  - 全量测试 **287 passed, 2 skipped**

- **傻瓜式接入任意模型(配置文件面板式)**:
  - `create_model` 新增 `custom` 通用 provider(`model/deepseek.py`):从 `WARDEN_API_KEY` /
    `WARDEN_BASE_URL` / `WARDEN_MODEL` **零改源码**接入任意 OpenAI 兼容端点(自建网关 / Ollama /
    硅基流动 / 任意私服);也支持 `create_model('custom', base_url=..., model=..., api_key=...)`
    显式传参;缺 base_url 时给清晰提示。
  - 内置四家(deepseek/openai/zhipu/bailian)仍各自读对应 `*_API_KEY`,不破坏。
  - `.env.example` 加 custom 配法 + README 新增「接入你自己的模型(傻瓜式)」小节。
  - 新增 `tests/test_deepseek.py` custom 用例(读环境变量/显式传参/缺 base_url 报错/未知厂商)。
    全量测试 **292 passed, 1 skipped**

### 未实现(路线图,见 README)

- T9 分层/契约测试(接口契约一致性、分层边界)
