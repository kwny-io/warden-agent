# Changelog

> 本文件按**时间戳**记录每次提交改动的内容,以及**尚未实现**的待办(未实现/路线图)。
> 每次提交都同步更新本文件,保持「git 历史 ↔ 文档」一致。

---

## 2026-09-21（第七批：凭证密文与租约落库）

### 本轮改动（在工作区，尚未 commit）

> ⚠️ 说明：本批与第三～六批的改动目前都还在工作区（`git log` 停在第二批 `e76db3e`）。
> 下面第三节标注的"已提交"是当时写下时的口吻，实际状态以 `git status` 为准。

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
