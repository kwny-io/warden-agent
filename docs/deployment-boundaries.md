# 部署边界与多租户口径

> 这份文档回答两个问题：**这套运行时能怎么部署（单副本 / 多副本）**，以及
> **"多租户"在这里到底意味着什么**。目的不是宣传，而是把边界写清楚。

## 一句话结论

Warden Agent 是一个**有状态的 Agent 运行时**，支持两种部署形态：

- **单副本（默认）**：协调状态（幂等 / 事件流 / 限流计数）在进程内，零配置、零延迟。
- **多副本（`WARDEN_SHARED_STATE=1` + PostgreSQL）**：协调状态放进共享存储，
  多个副本读写同一张表——幂等、SSE 事件流、限流额度都真正全局一致。

**多租户**：开启鉴权（`WARDEN_API_KEYS`）时，会话归属与越权拦截是**真的**——
身份由 API Key 决定，客户端无法自称；针对他人 Run 的读/写/删/审批一律 403。

## 多租户口径：身份从哪来

| 模式 | 触发条件 | 会话归属 | 越权拦截 |
|---|---|---|---|
| **多用户** | `WARDEN_API_KEYS=alice:k1,bob:k2` | 由 **key** 决定，`?user_id=` 被忽略 | 启用（按归属 403） |
| **单用户** | `WARDEN_API_KEY=k` | 固定身份 `WARDEN_API_USER`（默认 `demo-user`） | 启用（所有调用者同一身份，等价单租户） |
| **匿名开发** | `WARDEN_ALLOW_ANON=1` 且仅监听回环 | 由查询参数 `?user_id=` 决定 | **不启用** |

关键点：**只有 `bearer` 模式下"多租户"才成立**。匿名开发模式保留 `?user_id=` 是为了让
本地控制台能切换演示账号，它**不是安全边界**。

审计同样按租户隔离：`/audit` 只返回调用者所在 `tenant_id` 的记录。

## 协调状态：单副本 vs 多副本

三样"必须全局一致"的状态被抽成了可插拔实现（`web/coordination.py`）：

| 组件 | 单副本（默认） | 多副本（共享） | 不共享的后果 |
|---|---|---|---|
| 幂等表 | `InProcessIdempotencyStore` | `SqlIdempotencyStore` | 同一 `Idempotency-Key` 打到不同副本 → **重复执行** |
| 事件流 | `InProcessEventBus` | `SqlEventBus` | 客户端连副本 A、事件产生在 B → **收不到事件** |
| 限流计数 | `InProcessRateLimitStore` | `SqlRateLimitStore` | 实际总限额 ≈ 配置值 × 副本数 |

切换方式：设 `WARDEN_SHARED_STATE=1`，并把存储换成 `PostgresStore`（见下文）。
`SqlEventBus` 用**轮询增量**读事件表（默认间隔 250ms）——比 Pub/Sub 延迟略高，
但零新依赖且对 SQLite/PostgreSQL 通用；需要更低延迟可换 Redis/NATS 实现，
只要满足 `EventBus` 协议，上层一行不改。

## 进程内状态清单（扩容时逐项确认）

| 状态 | 位置 | 多副本后果 | 现状 |
|---|---|---|---|
| 幂等表 | `web/coordination.py` | 幂等失效 | ✅ 可切共享存储 |
| SSE 事件总线 | `web/coordination.py` | 丢事件 | ✅ 可切共享存储 |
| 限流计数桶 | `web/coordination.py` | 限额翻倍 | ✅ 可切共享存储 |
| 出站并发信号量 | `web/outbound.py` | 每副本各 N（不全局） | 如需全局并发上限，把"在飞请求数"也放共享存储 |
| 会话缓存 `SessionRegistry._sessions` | `web/server.py` | 各副本各自缓存（会从库重建，**正确但不省内存**） | 可保留（纯缓存） |
| 凭证密文与租约 | `credential/vault.py` | 已落库（`credentials` / `credential_leases` 表）→ 多副本共用一个库 | ✅ 可切共享存储；密钥材料仍需各副本统一 |
| 工具稳定性层熔断状态 | `tool/stability.py` | 每个副本各自熔断（不全局） | 如需全局语义，换外置熔断 |
| RAG 向量库 | `rag/knowledge.py` | 各副本各自索引（同一份文档索引结果一致，**不共享但等价**） | 索引成本 × 副本数；大语料建议外置向量库 |
| 记忆库 | `memory/store.py` | 默认已落盘（`SqliteMemoryStore`）→ 用 PostgreSQL 时需同库 | 多副本共用一个库；并发写同一 key 无分布式锁 |

**持久化本身是可共享的**：`RunStore`（SQLite / PostgreSQL 同接口）、审计表、存档点表、
记忆表都在数据库里。

## 多副本部署要点

1. **存储换 PostgreSQL**：`pip install "psycopg[binary]"`，用 `PostgresStore`
   （与 `SqliteStore` 同接口；`docker-compose.yml` 里的 `db` 服务是显式 opt-in）。
   ⚠️ SQLite 是**单机文件**，跨主机共享文件系统跑多副本不受支持——多副本请用 PostgreSQL。
2. **开共享协调状态**：`WARDEN_SHARED_STATE=1`。
3. **凭证密钥统一**：所有副本配同一个 `WARDEN_CREDENTIAL_KEY`。凭证密文与租约已经落库
   （`credentials` / `credential_leases` 表），所以多副本天然共享；但**密钥材料必须一致**，
   否则副本 A 加密的密文副本 B 解不开（会抛解密失败，不会静默给错值）。
4. **会话粘性（HTTP 路径仍建议；自动恢复已由锁兜住）**：同一 `run_id` 的并发请求尽量落到同一副本。
   会话缓存本身正确（会从库重建），但同一会话被两个副本同时驱动会出现"后写覆盖前写"。
   - **已兜住的部分**：无人值守的**跨 Run 恢复**（`RecoveryWorker`）在驱动每个 run 前先抢
     **Run 级租约锁**（`runtime/locking.py` + `run_locks` 表）——多副本下同一个 run 只会被一方驱动，
     抢不到的一方记为 `held_by_other` 并跳过。租约式（带 TTL）是刻意的：持有者崩了不必人工解锁，
     到期即可被别的副本接手。
   - **HTTP 路径也已接上**：`/chat/{run_id}`、`/chat/stream/{run_id}`、`/approve/{run_id}`、
     `/reject/{run_id}` 都会先抢同一把 Run 锁，**抢不到回 423**（不是 409——409 在本服务里
     表示"没有待审批的请求"），客户端稍后重试即可。流式请求整段 SSE 走完才释放。
   - **所以并发驱动的正确性已不再依赖粘性路由**（两个副本同时驱动同一 run 会被 423 挡住）。
     粘性仍**建议**保留：它能让请求少一次"抢不到就重试"的往返，体验与吞吐更好。
   - **TTL 窗口已由心跳续租关掉**：驱动期间有后台线程每 **TTL/3** 续一次租，所以
     "单次驱动比 TTL 还长"（模型+工具耗时久、SSE 流很长）**不会**中途被接管。
     只有在**进程卡死 / 心跳线程停摆**时，租约才会自然过期被别人接管——那种情况下
     `RunLease.lost` 会置真并打警告（不假装还持有），可据此接监控。
5. **限流**：共享计数已全局一致；也可把限流上提到网关，应用侧 `WARDEN_RATE_LIMIT=0` 关掉。

## 恢复语义

- **单 Run 恢复**：`AgentSession` 从 `RunStore` 读回状态与消息；存档点（`checkpoints` 表）
  记录"跑到第几轮、在哪一步"。
- **跨 Run 协调恢复**：`RecoveryController` 读全部存档点，分组为
  `resume / retry / await_human / terminal`。
- **真正执行**：`RecoveryWorker`（`runtime/worker.py`）按计划续跑——
  该续的续、该重试的重试（`attempts` 递增、超上限不再试）、**等人工的不碰**、终态的跳过。
  入口：`warden recover --apply`（跑一轮），或在自己的工作进程里用同一装配调
  `RecoveryWorker.run_forever()`。
- **安全边界**：`AgentSession.resume()` 对 `WAITING_APPROVAL` 等状态直接抛
  `RunNotResumable`——**自动续跑等待审批的 Run 等于绕过人工闸门**。

## 安全默认值清单

| 项 | 默认 | 说明 |
|---|---|---|
| 鉴权 | **fail-closed** | 无 key 又无 `WARDEN_ALLOW_ANON=1` → 拒绝启动 |
| 对外监听 | 拒绝无鉴权 | `WARDEN_HOST=0.0.0.0` + 无鉴权 → 拒绝启动 |
| 限流 | **开启**，600 次/60 秒/调用者 | `WARDEN_RATE_LIMIT=0` 关闭；格式 `次数/秒数` |
| 协调状态 | **进程内**（单副本） | 多副本必须 `WARDEN_SHARED_STATE=1` |
| RAG 知识库 | **关闭** | `WARDEN_KNOWLEDGE=1\|<目录>` 开启；默认**词频嵌入（词面匹配，非语义）**，要语义配 `WARDEN_EMBED_*` |
| 出网抓取 | **关闭** | `WARDEN_WEB_FETCH=1` 开启真实 HTTP 抓取；每跳重定向都过 URL 策略（拒内网/环回/云元数据） |
| 出站限速/配额 | **开启**（全局 120/60s、单 host 20/60s、并发 8、日配额不限） | `WARDEN_OUTBOUND_*` 可调；URL 策略先于闸门（被拒 URL 不占配额）；离线 provider 不占配额 |
| 记忆 | **落盘**（产品入口） | `run_server` 用 `SqliteMemoryStore`；`build_agent(memory=True)` 不传记忆库则进程内（重启即丢） |
| 凭证加密 | **密文落库**（`credentials` 表）；密钥材料未配 `WARDEN_CREDENTIAL_KEY` 时用进程内临时密钥 | 落库的是 AES-GCM 密文，明文不落盘；临时密钥下密文重启即解不开，启动时告警 |
| 凭证隔离 | 按**调用者身份**（`user_id`）分作用域 | 同租户多用户互不可见；启动配置的 key 记为部署级，全体共用 |
| 沙箱隔离 | 内核档需 `SYS_ADMIN`，拿不到就**如实降级** | 见 README |
| 容器 | 非 root + rootfs 只读 + `cap_drop: ALL` | 见 `Dockerfile` / `docker-compose.yml` |
