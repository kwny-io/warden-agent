# 运维手册（Operations Runbook）

> 这份文档回答"**接手之后怎么把它跑住**"：平时看什么、出问题怎么查、备份怎么恢复、
> 怎么升级与回滚。它和 `deployment-boundaries.md`（能怎么部署、边界在哪）互补。
>
> 定位先说清楚：**本项目的运维面分两步补的**。第一阶段（2026-09 上旬）备齐备份/恢复、健康探针、
> 指标、挂起告警；第二阶段（2026-09-22）补齐了**链路追踪、告警规则库、SLO/错误预算、
> 灰度发布与健康门控回滚、凭证密钥的 KMS/HSM 托管**。下面每节都标了现状与边界。

---

## 一、平时要看什么（巡检清单）

| 看什么 | 怎么看 | 正常长什么样 |
|---|---|---|
| 进程活着 | `GET /health/live` | 200 |
| 依赖可达（DB） | `GET /health/ready` | 200；DB 断了返 503（**别只看 live**） |
| 当前能力与关键开关 | `GET /capabilities` | 尤其看 `features.run_lock`——**多副本下必须是 `SqlRunLock`**，`InProcessRunLock` 等于没锁 |
| 指标 | `GET /metrics`（Prometheus 文本） | 请求数/耗时分布/5xx/限流拒绝数/`warden_stuck_runs` |
| **挂太久没人管的会话** | `GET /alerts/stuck?older_than_min=60` | `count=0` |
| **审计有没有被动过** | `warden audit-verify`（被动过退出码 4） | `✅ 链完整：N 条记录` |
| **审计归档/取证** | `warden audit-export`（带链字段，链断则退出码 4） | 产物落在 `--out-dir`（默认 `./audit-exports/`） |
| **依赖/镜像有没有已知漏洞** | CI 的 `security`（pip-audit）与 `container`（trivy）job；本地可跑 `bash scripts/container_smoke.sh` | 都绿；trivy 只对**有修复**的 HIGH/CRITICAL 失败 |
| **镜像里到底装了什么** | CI 的 `sbom` job（syft / SPDX JSON，构建产物可下载）；交付镜像是否为本次源码构建 | SBOM 产物含 OS 包 + Python 依赖；镜像签名只对 `v*` tag（`image-signing` job，cosign keyless） |
| **schema 有没有"改了结构却忘了升版本"** | CI 的 `migration` job；本地 `uv run --frozen python scripts/check_migrations.py --backend sqlite` | 新库记录版本 == `_SCHEMA_VERSION`，且结构指纹与 `scripts/schema_snapshot.json` 一致 |
| 恢复计划 | `GET /recovery/plan` | 该续/该重试/等人工/终态四类 |
| **请求在链上的位置** | 响应头 `traceparent` / 日志里的 `trace_id=` | 与上游传入的 `trace_id` 一致（见「十、链路追踪」） |
| **告警规则是否加载** | Prometheus UI → Status/Rules | 两组规则都在（`deploy/observability/alerts/`） |

启动日志里会打印**生效配置**：协调状态（共享/进程内）、入站限流、**出站限速**、
Run 锁实现、认知能力开关。排查"为什么行为和预期不一致"时先看这几行。

---

## 二、告警：把"需要人管"变成可抓取的事实

### 谁需要人管

Run 进入 `WAITING_APPROVAL`（高危操作等审批）或 `WAITING_INTERACTION`（等用户回复）之后，
**不会自己动**：没有超时、没有重试、没有任何通知。线上没人盯，它就一直挂着。

### 怎么接告警

```bash
# 有输出即代表"有人需要处理"；退出码 3 便于 cron/监控判断
warden stuck --older-than-min 60          # 或 --json 给脚本消费
```

或抓 HTTP（认证模式下按归属收敛）：

```
GET /alerts/stuck?older_than_min=60   →  {"count": N, "runs": [...]}
```

**建议**：每 5 分钟跑一次，`count > 0` 就通知值班人。阈值按业务定（审批慢的业务别设 10 分钟）。

### ⚠️ 口径（别误读）

等待时长**优先**用"进入等待审批的时刻"（`pending_approvals.created_at`，**精确**）。
如果这条记录拿不到（例如 `WAITING_INTERACTION` 这种没有待审批记录的，或老库的历史行），
才退回用 Run 的**最后活动时间**近似——而后者会被等待期间的任何操作刷新，于是**低估**等待时长。

所以：**"它报警了"两种情况都可信；"它没报警"在退回近似的那种情况下不等于一定没挂久**。

每条告警都带 `source` 字段说明这个数是怎么来的，不必猜：

| source | 含义 | 可信度 |
|---|---|---|
| `approval` | 用进入等待审批的时刻算 | **精确** |
| `last_activity` | 用 Run 最后活动时间近似 | 只**低估**（CLI 文本会标注"实际可能更久"） |
| `unknown` | 两个时间戳都拿不到 | 保守报出来，不静默漏掉 |

---

## 三、备份

```bash
warden backup                       # 自动命名：<库名>.backup-<UTC时间戳>
warden backup /backup/warden.db     # 指定路径
```

- **不需要停服务**：用的是 SQLite 的**在线备份 API**，做的是一致性快照。
- **为什么不用 `cp`**：直接拷文件可能拷到"半个事务"的中间态（WAL/journal 未合并），
  拿回来的库可能是坏的。
- 备份做完会**立刻做完整性校验**（`PRAGMA integrity_check`），产物不可用就直接报错，
  不会留下一个"看起来有、其实坏了"的备份。
- **不会覆盖已存在的备份文件**（备份被悄悄覆盖是最气人的事故之一）。

**建议节奏**：数据库变更频繁就每小时一次；至少每天一次。保留策略按合规要求定。
备份文件**要放到别的地方**（另一个盘 / 对象存储）——和生产库同盘等于没备份。

### PostgreSQL 的备份

```bash
# 备份（pg_dump -Fc + 产物校验；默认不覆盖已有文件）
# 密码走 PGPASSWORD 环境变量（**不放命令行**，命令行会进 ps/history）
PGPASSWORD=... warden backup-pg --host db --dbname warden --user warden

# 恢复（标准工具；--clean --if-exists 会先清对象再导入）
pg_restore -h <host> -U <user> -d warden --clean --if-exists warden-20260101.dump
```

- **为什么要装 postgresql-client**：`backup-pg` 依赖 `pg_dump`/`pg_restore`。
  找不到时会**明确报错**（不会假装成功，更不会退化成 `cp` 数据目录——直接拷 PG 的数据目录是错的）。
- **产物校验**：备份完立刻用 `pg_restore --list` 列一遍内容，列不出来就报错——不会留下
  "看起来有、其实坏了"的备份（对应 SQLite 那边的 `PRAGMA integrity_check`）。

### 备份保留策略（别让备份把盘撑满）

```bash
warden backup --keep 7                    # 备份后只留最近 7 份
warden backup-pg --dbname warden --keep 7 # 同上（PG）
warden backup-prune --dir /backup --keep 7          # 只演练：列出要删什么，**不删**
warden backup-prune --dir /backup --keep 7 --yes    # 确认真删
```

删除**不可逆**，所以 `backup-prune` 默认只演练；`--keep` 不接受 0/负数（不提供"全删"这种危险解读）。
排序用文件名里的时间戳（`<库名>.backup-<UTC时间戳>`），拿不到才退回文件修改时间。

---

## 四、恢复

```bash
warden restore <备份文件>                       # 目标库不存在 → 直接恢复
warden restore <备份文件> --force               # 目标库已存在 → 显式确认覆盖
```

恢复是**破坏性操作**，所以设计成：

1. 目标库已存在时**默认拒绝**，必须显式 `--force`（少打一个参数就覆盖线上库太容易发生）；
2. 恢复前先校验备份**确实是健康的 SQLite 库**（坏备份必须当场被识破，不能等应用起来才炸）；
3. 恢复后再校验一次。

**操作顺序（重要）**：

```
1) 停服务            ← 目标库必须没有别的进程在用
2) warden restore <备份> --force
3) warden stuck / GET /health/ready 确认能起来、数据在
4) 启服务
```

**演练建议**：恢复路径**必须演练过**才算数。最小演练——
`warden backup` → 在测试库上 `warden restore` → 起服务 `GET /runs` 看会话还在。
本仓库有一条自动化演练测试（`tests/test_backup_restore.py`：造数据 → 备份 → 破坏原库 → 恢复 → 逐项验数据）。

---

## 五、升级与回滚

本项目提供**健康门控的灰度脚本**（`scripts/canary_rollout.sh`，见第十一节），但**没有内置网关/LB**
——真正切流量仍是部署侧的事。下面是手工流程与脚本的配合方式。

### 升级

1. **先备份**（见上）。
2. 拉新镜像 / 新代码。
3. **看 schema 变化**：本项目改动只做"加表 / 加列"（`CREATE TABLE IF NOT EXISTS`、
   `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`），所以**新版能读老库**；
   但**老版读不了新库**（新表它不认识——通常无害，除非回滚后又写了数据）。
4. 起新版 → 看启动日志（鉴权模式、协调状态、Run 锁、限流）→ 跑一遍巡检清单。
5. **多副本升级**：逐个副本滚动，先升一个观察，再升其余。注意同一 `run_id` 的粘性路由
   在滚动期间可能指向还在跑老版的副本——锁能挡住并发驱动（回 423），但用户会看到一次重试。

### 回滚

1. 停新版、换回老镜像。
2. **库一般不用回滚**（老版忽略新表/新列）；若新版写过老版不认识的数据，
   要么接受那部分数据对新版不可见，要么用升级前的备份恢复（**会丢升级后的数据**）。
3. 回滚后**再跑一遍巡检**——尤其确认 `run_lock` 与协调状态仍是你预期的实现。

### 停机行为（滚动升级/缩容会用到的）

- 收到 **SIGTERM** 后：uvicorn 先**停止收新连接**、把在飞的请求跑完（最多
  `WARDEN_SHUTDOWN_GRACE_S` 秒，默认 **30**），然后 app 的 lifespan **关闭资源**
  （事件总线 → 记忆库 → 主存储），最后进程退出。
- **为什么要有这个上限**：不设它时，一个卡住的请求能让停机无限期挂住——滚动升级时表现为
  "旧副本一直不退出"，运维只能强杀，而强杀就跳过了资源关闭。
- 日志里能确认这段：`启动完成：v…（停机时会关闭…）` / `停机：已关闭 …` / `停机完成`。
- 关停时**关一个失败不影响关其余的**（停机路径里最忌讳"第一个 close 抛了后面全不关"，
  连接就从这里漏出去）。

---

## 六、多副本部署检查清单
1. 存储用 **PostgreSQL**（SQLite 是单机文件，跨主机共享文件系统不支持）。
2. `WARDEN_SHARED_STATE=1` —— 幂等 / 事件流 / 限流计数 / **Run 锁**才会进共享存储。
3. 启动日志与 `/capabilities` 确认 `run_lock` 是 **`SqlRunLock`**。
   若是 `InProcessRunLock`，说明开关没生效或存储不支持，**多副本下等于没锁**。
4. 凭证密钥 `WARDEN_CREDENTIAL_KEY` **所有副本必须一致**（否则互相解不开对方的密文）。
5. **会话粘性建议保留**：并发驱动的正确性已由 Run 锁兜住（抢不到回 423），
   粘性只是少一次重试往返、体验更好。
6. **Run 锁带心跳续租**：驱动期间后台线程每 TTL/3 续一次租，所以"单次驱动比 TTL 还长"
   **不会**中途被接管。只有在**进程卡死 / 心跳线程停摆**时租约才会自然过期被接管——
   那种情况下 `RunLease.lost` 会置真并打警告（日志里搜"续租失败"），可据此接监控。

---

## 七、密钥与凭证

- 模型的 API Key 经 `CredentialBroker` **加密落库**（`credentials` / `credential_leases` 表），
  明文不落盘；按**调用者身份**隔离（同租户的 A、B 互不可见）。
- `WARDEN_CREDENTIAL_KEY` 是加密材料的默认来源（只从环境变量读）。
  - 不配 → 用"进程内临时密钥"并告警：**能加密，但重启后旧密文解不开**。
  - 换密钥 → 走下面的**轮换流程**（先挂旧密钥兜底、重加密、再摘掉）。
  - **要"托管"而不是"明文放 env"** → 配 `WARDEN_KMS_PROVIDER`（见第十二节）。
- 审计表**已是防篡改链**：每条记录带 HMAC 链哈希（改字段 / 删中间行 / 重排都会断链），
  巡检用 `warden audit-verify`。⚠️ **必须配 `WARDEN_AUDIT_KEY`**：不配则退化为不带密钥的
  哈希链（能查出手改/删行，但挡不住「改完重算整条链」）并告警。
- **密钥轮换**：新密钥配 `WARDEN_CREDENTIAL_KEY`、旧密钥进 `WARDEN_CREDENTIAL_OLD_KEYS` →
  `warden rotate-credentials`（幂等）→ 确认无误后**摘掉旧密钥**。直接换主密钥而不跑这一步，
  等于把存量凭证全废掉；轮换有解不开的会以退出码 5 报出来（那些密文原样保留，补回旧密钥还能救）。

---

## 八、已知边界（接手前先知道）

| 边界 | 影响 | 现状 |
|---|---|---|
| 挂起时长的**近似**来源（`WAITING_INTERACTION` / 老库行） | 只有这类会低估；`WAITING_APPROVAL` 已精确 | 已标明 `source`，未消除近似 |
| ~~Run 锁 TTL 窗口~~ | ~~超长驱动可能被接管~~ | **已由心跳续租关闭**；仅进程卡死时会丢锁（有告警） |
| ~~事件总线是轮询~~ | **已提供 `WARDEN_EVENT_BUS=notify`**（LISTEN/NOTIFY，仅 Postgres） | 实测发布→唤醒 47ms（轮询 110ms / 最坏 250ms）；通知只是提示，丢了不丢事件 |
| 熔断状态、出站并发上限**进程内** | 每副本各一份，不具全局语义 | 熔断按实例隔离是**刻意**的（全局熔断=同步失败）；出站全局/单host**速率**已可共享（`RateLimitStore`），仅**并发**是每进程 |
| ~~审计与记忆只有 SQLite 实现~~ | **已补（2026-09-23）**：PG 主存储 → `PostgresAuditStore` / `PostgresMemoryStore`（多副本共享）；审计链用 `pg_advisory_xact_lock` 跨副本串行 | 多副本不再需要关审计（K8s `WARDEN_AUDIT` 已改 1）；`warden audit-verify/export --pg` 可核多副本链 |
| ~~PostgreSQL 真库测试仅在本地~~ | **CI 里已实跑**（16 条零跳过，且有断言防静默跳过） | 已闭环 |
| ~~真实搜索 provider 未实现~~ | **已补**：`web.search` 可走真实联网搜索（`HttpSearchProvider`） | 设 `WARDEN_SEARCH_PROVIDER=tavily\|brave\|custom`（+ `WARDEN_SEARCH_API_KEY` / `WARDEN_SEARCH_ENDPOINT`）开启；不设或配不全则如实退回离线 mock 并告警；端点先过 URL 策略并计入出站配额 |
| 默认嵌入是**词频匹配** | 换个说法就掉分 | 配 `WARDEN_EMBED_*` 才是语义 |
| ~~抓取只有去标签粗提取~~ | **已补正文抽取（2026-09-22）**：`web/readability.py` 按块打分选正文，排掉导航/侧边栏/页脚 | 仍是启发式、不执行 JS（SPA 拿不到） |
| ~~向量库进程内全量扫描、不落盘~~ | **已补**：稀疏向量走倒排索引剪枝（**精确**，非近似）；向量**默认落盘**（主库同目录 `<主库名>-rag*.db`，重启按来源指纹复用、不重新嵌入）；稠密大规模语料 `auto` 会选 **IVF（真 ANN）** | 倒排是"更快的精确检索"不是近似；IVF 才是用召回换速度的 ANN（`nprobe>=nlist` 即精确）。纯 Python 实现，百万级/超高维仍应换 FAISS/pgvector |
| RBAC 只有两档角色 | 没有租户内细粒度角色（无组织/团队模型） | 见第十三节；管理员不读他人记忆 |
| 抓取只有去标签粗提取 | 拿不到 SPA / 正文抽取 | 需要 headless 浏览器 |
| ~~没有告警规则库 / SLO / 链路追踪 / 灰度~~ | **已补齐**（规则库+SLO、traceparent 链路、健康门控灰度） | 见第九～十一节；~~span 送后端仍需接 OTel exporter~~ **已补（2026-09-23）**：`core/otel.py` 零依赖 OTLP 导出，配 `OTEL_EXPORTER_OTLP_ENDPOINT` 即送收集器 |
| `warden_stuck_runs` 抓取时现算 | ~~每次抓取对存储做一次只读扫描~~ **已加 15s TTL 缓存**（与默认抓取间隔一致） | 刷新失败时保留上次值、**绝不写 0**（写 0 会把告警悄悄消掉） |
| **漏洞扫描的有效性取决于基镜像新鲜度** | 用陈旧缓存的基镜像扫会命中"其实已修"的 CVE（实测 13 条 → 拉新后 0 条） | 冒烟脚本构建已加 `--pull`（受限网络可 `SMOKE_NO_PULL=1`）；CI 每周定时跑 + dependabot 盯基镜像 |
| 进程内事件总线保留上限 | 超 `WARDEN_EVENT_KEEP`（默认 500）会丢最早事件 | 只影响进度展示；丢弃有警告、消费者能收到「存在缺口」提示 |
| 熔断/出站并发/`InProcessRunLock` 的**进程内语义** | 每副本各一份 | 已文档化（多副本须用共享实现） |
| **记忆按 `owner` 隔离**（2026-09-22 起） | 升级前写入的记忆 `owner` 为空串，**用户在 `/memory/{scope}` 与召回里看不到** | 空串 = 部署级共享；需要保留的旧数据应补写 `owner`（`UPDATE memories SET owner=? WHERE uid=?`） |
| 幂等并发时返回 **409**（`IDEMPOTENCY_IN_FLIGHT`） | 同 `Idempotency-Key` 的第二个并发请求不再重复执行，改为明确拒绝 | 客户端应按幂等语义重试（带 `Retry-After`） |
| ~~SSRF 的 DNS 二次解析 TOCTOU~~ | **已消除（2026-09-22）**：校验与"要连哪个 IP"出自**同一次解析**，请求被固定到该 IP（`Host` 头 + `sni_hostname` 保留原主机名） | 回归测试：解析器第一次给公网、之后给 `127.0.0.1`，断言连的是公网且**只解析一次** |
| **并发上升后 p95 明显变差** | 本机实测（离线假模型、单副本 SQLite）：并发 1 时 p50 0.83s，并发 8 时 p95 12.3s，像有串行化点 | **不是容量结论**，只是现象；要容量数字请在目标环境跑 `scripts/load_test.py` |
| **弃用过渡期的通知头未实现** | 客户端无法从响应里得知"你在用的版本已被弃用" | 至今只有 1.0 一个版本、未发生弃用；政策见 `docs/api-versioning.md`（主版本不受支持会明确 400） |
| 覆盖率门槛 **85%**（基线 87%） | 门槛贴着当前值会变成噪声门禁，故留 2 个点余量 | 低覆盖区主要是启动装配（`run_server.py`），靠端到端跑而不是单测 |

---

## 九、告警规则库与 SLO（错误预算）

规则与接线都在 `deploy/observability/`（含 README、Prometheus/Alertmanager 配置、一键起栈）。

- **告警规则库** `alerts/warden.rules.yml`：可用性（服务下线/就绪失败）、错误率（5xx 占比、
  预算快烧）、延迟（p95）、业务（**挂起 Run**、限流激增）。每条都写了"为什么响、先看哪"。
- **SLO/错误预算** `alerts/warden.slo.yml`：可用性 99.5%/30d、延迟 p95<1s 两条 SLI 的记录规则，
  以及**多窗口燃烧率**告警（快窗口发现快、慢窗口防误报）。
- **抓取要带 Bearer**：`/metrics` 与其他业务接口一样受鉴权保护；`prometheus.yml` 用
  `bearer_token_file` 从文件读 key（**别把 key 写进配置文件**）。
- **本地演练**：`deploy/observability/docker-compose.yml` 一键起 Prometheus+Alertmanager**+Grafana**，
  人为制造错误率/挂起 Run 看告警是否按预期触发。
- **看板**：`grafana/` 下是 provisioning（数据源 + 仪表盘），挂载即生效；
  「Warden Agent 概览」包含挂起 Run / 可用性 SLI / 5xx 占比 / 限流速率 / 请求速率 / 延迟 p50-p95-p99 /
  错误速率 / 审批决策。演练环境默认匿名只读，**生产别照搬**（请配 GF_AUTH_* 或接 SSO）。
- **守卫**：`tests/test_alert_rules.py` 断言规则与**仪表盘**引用的每个指标都真实存在、
  记录规则都被告警引用（防"改了指标名、规则/看板悄悄指向不存在的指标"——看板会变成空图且不报错）；
  CI 另用 `promtool` 校验规则语法。

**阈值是起点，不是真理**——先按默认跑，再按业务真实水位调。

---

## 十、链路追踪（traceparent）

**是什么**：W3C Trace Context。请求带 `traceparent: 00-<trace-id>-<span-id>-<flags>` 进来，
本服务**沿用同一个 `trace_id`**、生成自己的 `span_id`，并在调下游时把新的 `traceparent` 透传出去。
于是"这次请求经过了哪些步骤、我调下游那一次对应下游哪条日志"才连得起来。

- **开关**：`WARDEN_TRACING`（默认开；`0` 关闭，关闭后 `span()` 不碰上下文、开销接近零）。
- **怎么看**：响应头会回写 `traceparent`；日志里每个 span 一行，含
  `name= trace_id= span_id= parent_span_id= duration_ms=`。用 `trace_id` 即可串起一次请求的全部步骤。
- **出站透传**：`web.fetch` 等出站请求会带上当前 `traceparent`（下游若支持 W3C 即可接链）。
- **不合法的入站头**（版本错/全零 id/长度不对）一律**当作没有**、重新起链——不因上游脏头把链路带崩。
- **边界**：本项目**不引入 opentelemetry**（保持零重依赖）。这里做的是上下文传播 + 结构化 span 日志；
  要把 span 送进 Jaeger/Tempo，接一个 OTLP exporter 即可（"上下文不断"这个前提已由本层保证）。

---

## 十一、灰度发布与健康门控回滚

此前升级是"停旧、起新，坏了再换回来"，全靠人盯。现在有一条**自动化判据**：

```bash
# 起金丝雀 → 等 /health/ready → 校验鉴权仍 fail-closed → 通过才允许放量
scripts/canary_rollout.sh <新版镜像>            # 只做门控，不动旧版
scripts/canary_rollout.sh <新版镜像> --promote  # 门控通过后自动停旧容器
```

- **退出码 0 = 可以放量；非 0 = 别切**（旧版仍在服务，等于"自动不升级"）。
  金丝雀不健康时会打印它的日志并就地拆掉。
- **切流量是部署侧的事**：本项目没有内置网关/LB，脚本只做门控。拿到 0 之后按
  `5% → 25% → 100%` 放量，每个档位盯第九节的告警（错误率/延迟/预算燃烧）。
- **回滚**：异常时**不执行切流**，或把网关切回旧实例（旧实例未被销毁时零成本）。
  用 `--promote` 时旧容器会被删——若想保留即可回滚的旧实例，就别用 `--promote`。

---

## 十二、密钥托管（KMS/HSM 信封加密）

默认 `WARDEN_KMS_PROVIDER=env`（材料来自 `WARDEN_CREDENTIAL_KEY`）。要"托管"就换成：

- `aws-kms`：根密钥待在 AWS KMS 里，应用用 `kms:Decrypt` 解开被包装的 DEK。
- `vault-transit`：根密钥待在 Vault transit 引擎里，解密在 Vault 内完成，密钥不出 Vault。

两种都是**信封加密**：根密钥（KEK）永不出 KMS/HSM，被包装的数据密钥（DEK）随配置分发，
应用解开 DEK 后用它对凭证做 AES-GCM。轮换只换 DEK 并重新包装，KEK 不动。

**一次性准备**（部署侧执行，产物放进 `WARDEN_KMS_WRAPPED_KEY`）：

```bash
# AWS KMS：生成一个 DEK，拿回被包装的那份（明文那份用完即弃）
aws kms generate-data-key --key-id <kek-id> --key-spec AES_256 \
  --query 'CiphertextBlob' --output text        # 输出 base64 → WARDEN_KMS_WRAPPED_KEY

# Vault transit：先确保 transit 引擎里有一把 KEK（key），用它加密 DEK
vault write -format=json transit/encrypt/warden plaintext=$(head -c 32 /dev/urandom | base64) \
  | jq -r '.data.ciphertext'                     # vault:v1:... → WARDEN_KMS_WRAPPED_KEY
```

`vault-transit` 还需 `WARDEN_VAULT_ADDR` / `WARDEN_VAULT_TOKEN` / `WARDEN_VAULT_KEY_NAME`。
取值写错会**直接报错、不静默回落**——否则会"以为在托管、其实没托管"。

**边界**：本层保证"接入 KMS/HSM 的形状与逻辑"（解开被包装的 DEK、历史密钥兜底）。
仓库测试用桩替身验证逻辑，**不联真实云**；真用起来需要你有一个可用的 KMS/Vault。

---

## 十三、角色与权限（RBAC）

只有两档角色，**来自配置、不来自请求**：

```
WARDEN_ADMIN_PRINCIPALS=ops,oncall      # 逗号分隔的 principal id（= 用户 id）
```

| 能力 | 普通用户（默认） | 管理员 |
|---|---|---|
| `/audit` | 只看自己的 | **看租户全部** |
| `/approvals/history` | 只看自己名下 Run 的 | **看全部** |
| `/alerts/stuck` | 只看自己的 | **看全部**（值班要知道"整个系统有没有人卡着"） |
| `/recovery/plan` | 只看自己的 | **看全部** |
| `POST /models/select` | `scope=self`（只切自己的，默认） | 可 `scope=deployment`（切**部署默认**，影响所有没自选过的会话） |
| `/memory/{scope}` | 只看自己的 | **仍然只看自己的**（见下） |

**几条刻意的取舍**：

- **不配 `WARDEN_ADMIN_PRINCIPALS` ⇒ 没有人是管理员**（fail-closed）：
  "忘了配"绝不能变成"人人都是管理员"。
- **不支持通配符 `*`**：写 `*` 只会被当成一个名叫 `*` 的 principal，不是"所有人"——
  把权限开关做成"一不小心全网开放"是最糟的设计。
- **管理员不读别人的记忆**：运维要的是**系统状态**的全局视图（谁在乱调、有没有卡住），
  不是**用户数据**的全局视图；记忆里是用户内容，保持按 `owner` 隔离。
- **没有细粒度角色**：现在只有"API Key → principal"这一层身份，没有组织/团队模型；
  真需要租户内角色时再扩（扩的时候要连带把上面这张口径表补全）。
