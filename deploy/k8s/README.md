# Kubernetes 参考部署（单副本 / 多副本）

这目录给的是**参考 manifests**：结构、探针、安全上下文、优雅停机、多副本前提都按本项目的
实际开关配好了，**但环境相关的值必须你替换**（下面列了清单）。

> ⚠️ **诚实边界（先看这条）**
> - 这些 manifests **通过了官方 schema 的严格校验**（kubeconform，命令见下），
>   也有一组**离线结构断言**在测试里守着（`tests/test_k8s_manifests.py`：探针齐全、
>   优雅停机时间 < `terminationGracePeriodSeconds`、非 root + rootfs 只读、不用 `:latest`、
>   多副本必配 PG + 共享状态……）。
> - 但**没有在真实集群上跑过**——"能不能起来、扩缩是否按预期"要靠你的集群验证。
>   这一点不写成"已验证"，见 `docs/operations.md` 的已知边界。
> - **审计与记忆跟随主存储**（2026-09-23 起，不再是 SQLite 独占）：配了
>   `WARDEN_PG_HOST` 时走 `PostgresAuditStore` / `PostgresMemoryStore`，多副本共享
>   **同一账本**与**同一份记忆**（审计链写入用 `pg_advisory_xact_lock` 跨副本串行）。
>   所以参考配置里是 `WARDEN_AUDIT=1`（见 `configmap.yaml`）；SQLite 后端只在单副本下用。
>   ⚠️ 但 `WARDEN_AUDIT_KEY` **必须**在 Secret 里配，否则链退化为无密钥哈希链
>   （能查出手改/删行，挡不住"改完重算整条链"）。核链用 `warden audit-verify --pg`。

## 文件

| 文件 | 作用 |
|---|---|
| `namespace.yaml` | 命名空间 `warden` + PSA `restricted` + ResourceQuota + LimitRange（**必须最先 apply**；文件顶部明确禁止 `kubectl create namespace`）。⚠️ 命名空间名字是**事实源**：`scripts/rollback.sh` 的默认命名空间从这里读（第五十四批起），改名就两边都改 |
| `serviceaccount.yaml` | ServiceAccount `warden-agent`（**必须 apply**：deployment / migrate-job / backup-cronjob 都写死了这个 SA 名，缺它 Pod 起不来） |
| `deployment.yaml` | 2 副本、滚动更新先起后停、探针、非 root + rootfs 只读、`/data` 与 `/tmp` 可写卷 |
| `configmap.yaml` | 非敏感配置：PG 连接、共享状态、事件总线、停机宽限、管理员名单 |
| `secret.example.yaml` | 密钥**示例**（值全是 `REPLACE_ME`），用前替换或改用外部密钥服务；含 `WARDEN_METRICS_BEARER`（供 ServiceMonitor 抓 `/metrics`，与用户 API key 分开。⚠️ 名字就是 `…_BEARER`：`deploy/observability/servicemonitor.yaml` 的 `bearerTokenSecret.key` 要的就是这个键，写成别的名字会让抓取 401） |
| `service.yaml` / `ingress.yaml` | ClusterIP + 入口（SSE 关缓冲、会话粘性） |
| `hpa.yaml` / `pdb.yaml` | 按 CPU 扩缩（2~6）、维护时至少留 1 个副本 |
| `networkpolicy.yaml` | **默认拒绝**（ingress+egress 双方向）+ 白名单：ingress-nginx 入站、DNS、PG、OTLP、外网 443；显式排除云元数据 `169.254.169.254/32` |
| `migrate-job.yaml` | 迁移 Job：复用应用镜像/ConfigMap/Secret，构造 `PostgresStore` 触发幂等 DDL；**必须先于** Deployment 成功 |
| `backup-pvc.yaml` | 备份落盘卷（`warden-backups`，RWO 20Gi） |
| `backup-cronjob.yaml` | 每日 `pg_dump -Fc` + 产物校验 + 保留**最新 14 份**；口令只从 Secret 注入（CronJob 见下） |

## 用之前要替换的（环境相关）

1. **镜像**：`deploy/k8s/deployment.yaml` 的 `image:` → 你的仓库与 tag**@sha256:digest**
   （**别用 `:latest`**）。清单与 `Dockerfile` 里的 digest 都是**全 0 占位符**，CI 负责替换：
   `docker buildx imagetools inspect <repo>:<tag> --format '{{.Manifest.Digest}}'`。未替换会 `manifest unknown`。
   `migrate-job.yaml` / `backup-cronjob.yaml` 用的是同一个占位符，别漏改。
2. **PostgreSQL**：`configmap.yaml` 的 `WARDEN_PG_HOST`；密码放 Secret 的 `WARDEN_PG_PASSWORD`。
   没现成的库就用云厂商托管 PG，或另起一个 StatefulSet（不在本目录范围）。
3. **鉴权**：`secret.example.yaml` 的 `WARDEN_API_KEYS`（`用户id:密钥` 逗号分隔）。
   **必须配**——对外监听 + 无鉴权时应用会 fail-closed 拒绝启动。
4. **管理员**：`configmap.yaml` 的 `WARDEN_ADMIN_PRINCIPALS`（不配就没有管理员）。
5. **域名/TLS**：`ingress.yaml` 的 host 与 `secretName`（以及 `ingressClassName` 按你的网关改）。
   ⚠️ 同一个 `configmap.yaml` 里的 `WARDEN_TRUSTED_PROXIES` 要填**代理自己的网段**
   （不是客户端网段；不知道就先 `kubectl -n ingress-nginx get pods -o wide` 看一眼 pod IP）。
   不填的后果不是「限流偏一点」，而是所有匿名调用者共用一个限流桶，且同一个
   `Idempotency-Key` 会**跳调用者串响应体**——见 `docs/deployment-boundaries.md`。
6. **容量**：`resources` 的 requests/limits 按你的实测调（**先跑 `scripts/load_test.py` 拿数字**，
   别照抄这里的 200m/1 核）。
7. **网络策略**：`networkpolicy.yaml` 里 `REPLACE_ME_PG_CIDR` → 你托管 PG 的网段；
   入口网关若不在 `ingress-nginx` 命名空间，改 `namespaceSelector` 的标签值。
   ⚠️ **NetworkPolicy 要生效，集群 CNI 必须支持它**（Calico/Cilium/Antrea 支持；
   裸 flannel 不执行策略——apply 成功但流量照样通，等于没配）。
8. **备份**：`backup-pvc.yaml` 的 `storage` 与 `storageClassName` 按库体量和你的存储类改。
9. **OTLP**：`configmap.yaml` 的 `OTEL_EXPORTER_OTLP_ENDPOINT` → 你的 collector 地址
   （默认指向 `observability` 命名空间的 `otel-collector`；不接后端就删掉这个 key，只打结构化日志）。

> ⚠️ **占位符不是「还没配」，它会让启动直接失败**（第五十三批改的行为）：
> `configmap.yaml`、`secret.example.yaml` 里所有 `REPLACE_ME…` 值，只要落到**已注册的环境变量**上，
> 启动校验（`core/settings.py::validate_env`）就判错并**拒绝启动**（不是警告、不是回退默认值）。
> 这么改是因为占位符是一种**看起来配了、其实没配**的错值：最贵的一例是
> `WARDEN_TRUSTED_PROXIES=REPLACE_ME-pod-cidr`——应用照常起，日志还打印「受信前置代理:
> REPLACE_ME-pod-cidr（只采信来自它们的 X-Forwarded-For）」= **声称信任已开**，
> 而 uvicorn 把非法 IP 当**字面量**、没有任何对端能匹配 ⇒ `X-Forwarded-For` 静默丢弃
> ⇒ 限流又塔成一个桶、同一个 `Idempotency-Key` 可跳调用者串响应体。
> 宁可让它起不来，也不让它看着是好的。

## 校验（不含真实集群）

```bash
# 1) 离线的结构断言（CI 里就跑这个，不需要集群/网络）
uv run --frozen pytest tests/test_k8s_manifests.py -q

# 2) 官方 schema 严格校验（需要联网取 schema；CI 里网络是通的）
docker run --rm -v "$PWD/deploy/k8s:/m:ro" ghcr.io/yannh/kubeconform:v0.6.7 \
  -summary -strict /m

# 3) 到集群上干跑（**只有这一步能证明它在你的集群里成立**）
kubectl -n warden apply --dry-run=server -f deploy/k8s/
```

## 部署 / 升级顺序（迁移先于 Deployment）

**为什么不能一把梭 `kubectl apply -f deploy/k8s/`**：本目录是纯参考清单，
**没有 Helm chart**（所以没有 `pre-upgrade` hook）、**也没有 `kustomization.yaml`**
（所以没有 `depends_on`/`sortOptions`）。一次性 apply 时，`deployment.yaml` 的滚动
可能在 `migrate-job.yaml` 跑完之前就发生——而 `deployment.yaml` 是 `replicas: 2`，
两个副本会**并发**跑迁移 DDL（原因见 `migrate-job.yaml` 顶部注释）。
所以顺序只能由**分步 apply + wait** 自己保证：

```bash
# 0) 命名空间与 ServiceAccount：**必须最先 apply**。
#    · 用 `apply -f namespace.yaml` 而**不是** `kubectl create namespace`：
#      namespace.yaml 里带了 PSA `restricted`、ResourceQuota、LimitRange，
#      `create` 建出来的裸命名空间会让这些约束全部失效（而文件顶部也明确写了别用 create）。
#    · serviceaccount.yaml 也**不能跳**：deployment.yaml / migrate-job.yaml /
#      backup-cronjob.yaml 都写死了 `serviceAccountName: warden-agent`，
#      SA 不存在时迁移 Job 会卡在 `serviceaccount "warden-agent" not found`，
#      下面的 `kubectl wait` 必然超时 → 按本文件自己的约定会 `exit 1` 停止发布。
kubectl apply -f deploy/k8s/namespace.yaml
kubectl apply -f deploy/k8s/serviceaccount.yaml

# 1) 无依赖的先落地（ConfigMap / 存储 / 网络 / 路由）
kubectl -n warden apply -f deploy/k8s/configmap.yaml
kubectl -n warden apply -f deploy/k8s/networkpolicy.yaml
kubectl -n warden apply -f deploy/k8s/backup-pvc.yaml
kubectl -n warden apply -f deploy/k8s/service.yaml
kubectl -n warden apply -f deploy/k8s/ingress.yaml
kubectl -n warden apply -f deploy/k8s/hpa.yaml
kubectl -n warden apply -f deploy/k8s/pdb.yaml

# 2) 密钥（值见 secret.example.yaml 顶部；别把明文写进 shell history）
kubectl -n warden create secret generic warden-secrets --from-literal=...

# 3) ⚠️ 迁移**先于**新版本 Deployment：跑完且成功，才允许滚动
kubectl -n warden apply -f deploy/k8s/migrate-job.yaml
kubectl -n warden wait --for=condition=complete job/warden-migrate --timeout=10m \
  || { kubectl -n warden logs job/warden-migrate; echo '迁移失败，停止发布'; exit 1; }

# 4) 迁移成功后再滚动应用
kubectl -n warden apply -f deploy/k8s/deployment.yaml
kubectl -n warden rollout status deploy/warden-agent

# 5) 备份（可选，随附）
kubectl -n warden apply -f deploy/k8s/backup-cronjob.yaml
```

顺序约束的两种"正统"替代（本目录未采用，仅说明为什么现在要手工保证）：
- **Helm hook**：把迁移做成 `pre-install,pre-upgrade` hook 的 Job，`helm upgrade` 会等它完成；
- **kustomize**：`sortOptions`/依赖编排，或把迁移放进 `preSync`（Argo CD）阶段。
  两者都需要把本目录变成 chart/overlay——目前刻意保持"纯 YAML 参考"，所以用上面的分步命令。

> 重复执行：Job 的 `spec` 不可变，**已完成的** `warden-migrate` 再次 apply 是 no-op（不会重跑）。
> 要强制重跑迁移：`kubectl -n warden delete job warden-migrate` 再 apply。
> （`ttlSecondsAfterFinished: 3600` 也会在一小时后把它删掉，下次 apply 自然重跑。）

升级用**镜像 tag@digest** 换新版滚动（`maxUnavailable: 0`，滚动期间不中断）。

## 回滚 / 恢复

### 回滚应用（代码问题）

```bash
kubectl -n warden rollout undo deploy/warden-agent
kubectl -n warden rollout status deploy/warden-agent
```

**一条命令的版本**：`scripts/rollback.sh`（= 上面两条 + 选版本 + 健康检查 + 退出码）。
它的命名空间**取自本目录的 `namespace.yaml`**，不需要你记：

```bash
scripts/rollback.sh                     # 回滚到上一个 revision
REVISION=3 scripts/rollback.sh          # 指定 revision
DRY_RUN=1 scripts/rollback.sh           # 只打印将要执行的命令
NAMESPACE=staging scripts/rollback.sh   # 显式覆盖命名空间
```

退出码就是契约：`0` 回滚完成且全部副本 ready；`2` **确实**没有更早的版本（什么都没做）；
`1` 用法/环境问题 —— **包括“连当前 Deployment 都没看到”**（这时要查命名空间/名字/context/权限，
不是“没有历史”）；`3` 已下发但健康检查没过，**需要人介入，不要自动再滚**。

> 为什么把“看不到 Deployment”与“没有更早的版本”分成两句话：第五十四批之前它们是同一句话。
> 脚本默认的命名空间当时是手抄的 `warden-agent`（清单里是 `warden`），于是脚本把 kubectl 的
> NotFound 吞掉、报出“没有可回滚的历史版本”—— 那是在出事现场为假的一句话，
> 而“回滚脚本不可用”的结论会让人放弃回滚。现在默认值来自清单，读不出来就停下来要你显式传值。

⚠️ **回滚不会回滚数据库结构**：项目迁移是"只增不删"的幂等补列/建表（见 `migrate-job.yaml`），
旧版本代码遇到新增列会忽略。所以"代码回滚"通常直接可用；
只有当你回了**一个不认识某列/表**的旧版本时，才需要走下面的备份恢复。

### 恢复备份（数据损坏 / 误删）

前置：**先让应用停止写库**，否则 `pg_restore --clean` 会与在线写入打架：

```bash
export NS=warden
kubectl -n $NS scale deploy/warden-agent --replicas=0
# 暂停备份（CronJob 的 scale 已废弃，用 suspend），避免它往同一个 RWO 卷写
kubectl -n $NS patch cronjob warden-pg-backup -p '{"spec":{"suspend":true}}'
```

1) 挂**同一个 PVC** 起一个临时 shell（备份卷是 RWO，务必确认没有别的 Pod 还在挂它）：

```bash
kubectl -n $NS apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: pg-restore
spec:
  restartPolicy: Never
  securityContext:
    runAsNonRoot: true
    runAsUser: 10001
    runAsGroup: 10001
    fsGroup: 10001
  containers:
    - name: pg
      image: postgres:16-alpine@sha256:0000000000000000000000000000000000000000000000000000000000000000
      command: ["sleep", "infinity"]
      envFrom:
        - configMapRef: { name: warden-config }
      env:
        - name: PGPASSWORD
          valueFrom:
            secretKeyRef: { name: warden-secrets, key: WARDEN_PG_PASSWORD }
      volumeMounts:
        - { name: backups, mountPath: /backups }
  volumes:
    - name: backups
      persistentVolumeClaim: { claimName: warden-backups }
EOF
kubectl -n $NS wait --for=condition=Ready pod/pg-restore --timeout=2m
```

2) 选一份 dump 并**先校验可读**（坏备份恢复出来更糟）：

```bash
kubectl -n $NS exec -it pg-restore -- sh -lc 'ls -lh /backups'
kubectl -n $NS exec -it pg-restore -- sh -lc 'pg_restore --list /backups/warden-<时间戳>.dump | head'
```

3) 恢复（`--clean --if-exists` 先删后建；`--no-owner` 避免角色名不一致报错）：

```bash
kubectl -n $NS exec -it pg-restore -- sh -lc \
  'pg_restore -h "$WARDEN_PG_HOST" -p "$WARDEN_PG_PORT" -U "$WARDEN_PG_USER" \
     -d "$WARDEN_PG_DB" --clean --if-exists --no-owner /backups/warden-<时间戳>.dump'
```

4) 核**审计链**（恢复后最容易"静默不一致"的地方）：

```bash
warden audit-verify --pg      # 期望链连续；否则审计链与数据已不一致，需从更新的 dump 重建
```

5) 清理并恢复服务：

```bash
kubectl -n $NS delete pod pg-restore
kubectl -n $NS patch cronjob warden-pg-backup -p '{"spec":{"suspend":false}}'
kubectl -n $NS scale deploy/warden-agent --replicas=2
kubectl -n $NS rollout status deploy/warden-agent
```

> ⚠️ **备份卷的保留是「最新 14 份」，不是「14 天」**（`backup-cronjob.yaml` 里按**文件名时间戳**
> `sort` 后只留最新 14 份，与 CLI `warden backup-prune --keep 14` 同一口径）。
> 两者只在"每天恰好成功一份"时才碰巧相等；备份批量重跑或失败时就不等。
> 代码**刻意不用** `find -mtime +14 -delete`：那是按 **mtime** 删、没有"最少保留份数"下限，
> 卷里出现被 touch / 被 `pg_restore` 写过的文件、或节点时钟漂移，就可能把唯一可用的备份删掉
> （删备份不可逆，比磁盘满危险得多）。发现数据损坏越晚，可恢复的窗口越窄；要更久请同步到
> 对象存储（见 `backup-pvc.yaml` 顶部）。
> 备份失败/静默失效的告警接线、**Operator 路径的告警规则**（`prometheusrule.yaml`）见
> `deploy/observability/README.md`。

灰度的**自动化门控**在 `scripts/canary_rollout.sh`（它只做"能不能放量"的判定，
权重切换在你网关侧），可观测性栈见 `deploy/observability/`。

## 配置项 → 来源（configmap / secret / 默认值）

清单里的每一条都来自 `src/warden_agent/core/settings.py` 的 `ENV_SPECS`（唯一事实源）。
下表把它逐项对齐到清单：**清单里没有的 = 用代码默认值**（"默认值"列给关键默认，
其余见 `ENV_SPECS`）。标 🔒 的是敏感值，只能放 Secret；标 "(示例注释)" 的是
`secret.example.yaml` 里**默认注释掉**的可选项（不用的保持注释，见该文件说明）。

| 配置项 | 来源 | 默认 / 参考值 |
|---|---|---|
| `WARDEN_API_KEYS` | secret 🔒 | 必填（多用户 `用户id:key` 逗号分隔） |
| `WARDEN_API_KEY` | secret 🔒 | 单用户模式 / 指标抓取 token（示例注释） |
| `WARDEN_ADMIN_PRINCIPALS` | configmap | 必填（不配 = 无管理员） |
| `WARDEN_VIEWER_PRINCIPALS` | configmap | `""` = 无人只读 |
| `WARDEN_TENANT` | configmap | `REPLACE_ME-tenant`（代码默认 `local`） |
| `WARDEN_HOST` / `PORT` | configmap | `0.0.0.0` / `8000` |
| `WARDEN_TRUSTED_PROXIES` | configmap | **必填**（入口是 ingress-nginx）：取值 = 代理/pod 网段，**不是客户端网段**；不配会塔桶 + 幂等跳调用者串响应（**新增**） |
| `WARDEN_DB_PATH` | configmap | `/data/warden.db`（多副本下仅临时文件用） |
| `WARDEN_PG_HOST` / `WARDEN_PG_PORT` / `WARDEN_PG_DB` / `WARDEN_PG_USER` | configmap | `5432` / `warden` / `postgres`(清单里 `warden`) |
| `WARDEN_PG_PASSWORD` | secret 🔒 | 必填 |
| `WARDEN_SHARED_STATE` | configmap | `1`（多副本必须） |
| `WARDEN_AUDIT` | configmap | `1` |
| `WARDEN_AUDIT_REQUIRE_KEY` | configmap | `1`（**新增**：开了审计又没配 `WARDEN_AUDIT_KEY` → 拒绝启动） |
| `WARDEN_AUDIT_KEY` | secret 🔒 | 必填（配合 `REQUIRE_KEY`） |
| `WARDEN_EVENT_BUS` | configmap | `notify`（仅 PG 有效，不满足回落 `poll`） |
| `WARDEN_EVENT_KEEP` | configmap | `500`（**新增**） |
| `WARDEN_SESSION_CACHE_MAX` | configmap | `1000`（**新增**） |
| `WARDEN_SHUTDOWN_GRACE_S` | configmap | `30`（须 < deployment 的 `terminationGracePeriodSeconds: 45`） |
| `WARDEN_RATE_LIMIT` | configmap | `600/60`（**新增**） |
| `WARDEN_OUTBOUND_LIMIT` / `WARDEN_OUTBOUND_HOST_LIMIT` | configmap | `120/60` / `20/60`（**新增**） |
| `WARDEN_OUTBOUND_MAX_CONCURRENCY` / `WARDEN_OUTBOUND_DAILY_QUOTA` | configmap | `8` / `0`（**新增**） |
| `WARDEN_MAX_REQUEST_BYTES` | configmap | `1048576`（**新增**） |
| `WARDEN_SSE_MAX_CONNECTIONS` | configmap | `100`（**新增**） |
| `WARDEN_MAINTENANCE_INTERVAL_S` | configmap | `300`（**新增**） |
| `WARDEN_IDEMPOTENCY_TTL_S` | configmap | `86400`（**新增**） |
| `WARDEN_TRACING` | configmap | `1` |
| `WARDEN_LOG_FORMAT` | configmap | `json`（**新增**；不配则按 stdout 是否终端自动选，容器里本来就是 json） |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | configmap | **新增**，占位指向 `observability/otel-collector:4318` |
| `OTEL_SERVICE_NAME` | configmap | `warden-agent`（**新增**） |
| `OTEL_EXPORTER_OTLP_HEADERS` | secret 🔒 | (示例注释) |
| `WARDEN_ALERT_WEBHOOK_URL` | configmap | `""` = 不推送（**新增**） |
| `WARDEN_ALERT_WEBHOOK_HEADERS` | secret 🔒 | (示例注释) |
| `WARDEN_CREDENTIAL_KEY` | secret 🔒 | 必填（不设 = 重启后解不开密文） |
| `WARDEN_CREDENTIAL_OLD_KEYS` | secret 🔒 | (示例注释) 轮换期旧密钥 |
| `WARDEN_KMS_PROVIDER` / `WARDEN_KMS_WRAPPED_KEY` / `WARDEN_VAULT_*` | secret 🔒 | 默认 `env`（未启用托管） |
| 模型 key（`DEEPSEEK_` / `OPENAI_` / `ZHIPU_` / `DASHSCOPE_` / `WARDEN_MODEL_API_KEY`） | secret 🔒 | (示例注释)；不配则用离线假模型 |
| `WARDEN_BASE_URL` / `WARDEN_MODEL` | secret / 默认 | custom 网关用；默认 `deepseek-chat` |
| `WARDEN_SEARCH_PROVIDER` / `WARDEN_SEARCH_ENDPOINT` / `WARDEN_SEARCH_API_KEY` | 未配 / secret 🔒 | 不配 = 离线 mock 搜索 |
| `WARDEN_EMBED_BASE_URL` / `WARDEN_EMBED_API_KEY` / `WARDEN_EMBED_MODEL` | 未配 / secret 🔒 | 不配 = 词频哈希（**非语义**）检索 |
| `WARDEN_STABILITY` | 默认 | `1`（清单未列出 = 用默认） |
| `WARDEN_PLANNER` / `WARDEN_MAX_CONTEXT_CHARS` / `WARDEN_KNOWLEDGE` / `WARDEN_WEB_FETCH` | 默认 | 关 / 空 |
| `GIT_WORKDIR` / `SKILLS_DIR` / `MCP_SERVER` / `WARDEN_MCP_TIMEOUT_S` | 默认 | 未启用（`git.apply_patch`/技能/MCP 关） |
| `WARDEN_MULTIAGENT` | 默认 | `0`（不注册子 Agent 卡；`1` = 注册 `research`/`write`，会多花模型调用） |
| `WARDEN_WEB_DIST` / `WARDEN_ISOLATION_PREFIX` | 默认 | 镜像内默认（前端目录 / 沙箱档探测） |
| `PGPASSWORD` | secret 🔒 | 仅 `backup-cronjob.yaml` 用（`pg_dump` 的 libpq 变量） |
| `WARDEN_SERVER_URL` | — | 仅 CLI 用，默认 `http://127.0.0.1:8000` |

> 拼错的 `WARDEN_*` 变量不会被静默忽略：应用启动时会比对 `ENV_SPECS` 并**告警**
> （`unknown_warden_variables()`），格式错误直接拒绝启动（`validate_env()`）。
> 所以"清单里写错一个名字"会在 Pod 日志里露出来，不会悄悄用默认值跑。

## 在本机的 kind 集群上真的用一遍（含回滚演练）

> 第五十八批的经验：**离线结构断言与假 kubectl 都有它们答不出的问题**。
> 假 kubectl 只能回答「你问的那个抽屉里有什么」，答不出「脚本自己会去哪个抽屉」——
> 第五十四批的 8 条回滚用例全绿，却漏掉了「默认命名空间写死 `warden-agent`、而清单里是 `warden`」。
> 所以下面两条命令值得真的跑一次（本机已经跑过，见 `CHANGELOG.md` 第五十八批）。

### 1) 先把 API 端点接回宿主机（如果你的集群容器没有发布 6443）

kind 容器一旦被重建/恢复，`docker port <node>` 可能就是空的，`~/.kube/config` 里写的
`https://127.0.0.1:58583` 会 `connection refused`。用转发容器把这条链路接回来：

```bash
# NODE_IP：节点的 docker 内网 IP
NODE_IP=$(docker inspect -f \
    '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' warden-control-plane)
docker run -d --name warden-api-forward --restart unless-stopped \
    -p 58583:58583 -e UPSTREAM=$NODE_IP:6443 \
    -v "$PWD/scripts/kind_api_forward.py:/fwd.py:ro" python:3.12-slim python /fwd.py
kubectl get nodes        # 应当直接通了
```

> 为什么不用 `--network container:<node>`：docker 不允许「发布端口 + 共享网络命名空间」同时用；
> 而且 6443 在节点里已被 kube-apiserver 占用。TLS 不受影响：kind 生成的 apiserver 证书
> 本就包含 `127.0.0.1` 这个 SAN。

### 2) 在真集群上按契约走一遍回滚（不是假 kubectl）

```bash
MSYS_NO_PATHCONV=1 bash scripts/k8s_rollback_drill.sh   # Git Bash 下需要这个环境变量
```

它把仓库里的 `deploy/k8s/` 与 `scripts/rollback.sh` 拷进节点，然后逐个用例断言
**退出码 + 关键输出 + 集群状态**，结束把工作负载恢复原样并清理演练命名空间：

| 用例 | 做什么 | 期望退出码 |
| --- | --- | --- |
| 1 | 造一个新 revision，**不传 `NAMESPACE`** 回滚（命名空间必须从清单读） | 0 |
| 2 | 只有 1 个 revision 的 Deployment | 2（确实没有历史） |
| 3 | 命名空间打错（同时检查输出里有没有 `NotFound` 原话） | 1 |
| 4 | 脚本被挪到没有清单的地方（不猜默认值） | 1 |
| 5 | `DRY_RUN=1`（并核对 revision 未变） | 0 |

可覆盖的变量：`K8S_NODE`（默认 `warden-control-plane`）、`DRILL_NS`（默认 `warden-drill`）、
`DEPLOYMENT`（默认 `warden-agent`）。

> 边界（照实说）：演练只看「Pod ready 且模板已还原」，**不看回滚后业务是否真的恢复**；
> 它是**手工触发**的，没有 CI 里的 kind 作业；换一套环境要先 `kind create cluster`。
