# 可观测性整栈（告警规则库 / SLO / 演练）

这一目录把"能看"升级成"能运营"：**告警规则库**（哪些失效模式必须有人管）、**SLO 与错误预算**
（可靠性承诺还剩多少余量）、以及把两者真正加载起来的 Prometheus/Alertmanager 接线。

| 文件 | 作用 |
|---|---|
| `alerts/warden.rules.yml` | 告警规则库：可用性 / 错误率 / 延迟 / 业务（挂起 Run、限流激增） |
| `alerts/warden.slo.yml` | SLO 记录规则（可用性、延迟）+ 多窗口燃烧率告警 |
| `prometheus.yml` | 抓取配置（**带 Bearer**，因为 `/metrics` 受鉴权保护）+ 规则加载 + 告警转发 |
| `servicemonitor.yaml` | **Prometheus Operator** 版的抓取声明（装了 Operator 才用；与 `prometheus.yml` 二选一） |
| `prometheusrule.yaml` | **Prometheus Operator** 版的告警规则（`PrometheusRule` CR；只装 ServiceMonitor 是不够的，见下节） |
| `alertmanager.yml` | 告警路由（critical 立刻发、warning 打包发） |
| `docker-compose.yml` | 一键起栈，用于本地演练 |

## 快速演练

```bash
# 1) 抓取 token（内容 = 你的 WARDEN_API_KEY）。此文件含密钥，已在 .gitignore 里，别提交。
printf '%s' "$WARDEN_API_KEY" > deploy/observability/warden_token

# 2) 应用跑在本机 8000（或 compose 的 app，端口映射到 8000）

# 3) 起可观测性栈
cd deploy/observability
ALERT_WEBHOOK_URL=https://example.invalid/hook docker compose up -d

# 4) 打开 http://127.0.0.1:9090/targets 看抓取是否 UP；
#    http://127.0.0.1:9090/alerts 看规则触发情况。
```

## 静态校验（不依赖运行环境）

```bash
# Prometheus 官方工具校验规则语法（CI 里也是这么查的）
# --entrypoint promtool：prom/prometheus 镜像入口是 prometheus 本体，不能直接当命令后缀用
docker run --rm --entrypoint promtool \
  -v "$PWD/deploy/observability/alerts:/a:ro" prom/prometheus:v2.55.0 \
  check rules /a/warden.rules.yml /a/warden.slo.yml
```

仓库里另有一条测试 `tests/test_alert_rules.py`：解析这些 YAML，并断言**引用的每个
`warden_*` 指标都真实存在于代码里**、SLO 记录规则被其后的告警引用——防止"改了个指标名、
规则库悄悄指向一个不存在的指标"这类漂移。

`prometheusrule.yaml`（Operator 版）的规则正文不另做静态校验：
`tests/test_observability_config.py` 直接把它与上面两个文件**逐条深度比对**——两者不同就红，
所以 promtool 查过的那份就是它。

## 装了 Prometheus Operator：先把这两处接上（否则规则一条不生效）

静态路径（`prometheus.yml` + `docker-compose.yml`）三件事都齐：抓取、`rule_files` 加载规则、转发
Alertmanager。**Operator 路径不一样**：它管理的 Prometheus **不读任何 `prometheus.yml`**——
抓取来自 `ServiceMonitor`，规则来自 `PrometheusRule`，两者都是 CR，且都要被你的 Prometheus
**按标签选中**。所以 Operator 路径上还差这两件事：

1. **规则**：`kubectl apply -f deploy/observability/prometheusrule.yaml`
   （只 apply `servicemonitor.yaml` 的结果是：指标抓到了、告警一条不生效，而且**没有任何报错**）。
   两个 CR 的 `release` 标签必须能被你的 Prometheus 选中：
   `kubectl get prometheus -A -o jsonpath='{.items[*].spec.ruleSelector}'`；
   本仓两个文件都写成占位值 `release: prometheus`，换成你的 helm release 名（两处一起换，
   不一致会出现"一个生效、一个不生效"）。
2. **保留期**：Operator 路径的保留期在 Prometheus CR 里（**不在本仓**，因为通常由
   kube-prometheus-stack 的 Helm 生成）。核对：
   `kubectl get prometheus -A -o jsonpath='{.items[*].spec.retention}'`；
   要求只有一个：**≥ 规则里最长窗口**（`warden.slo.yml` 的 `rate[30d]`，即 30 天）；
   本项目口径写成 `spec.retention: 35d`（与本地 compose 的 `--storage.tsdb.retention.time=35d` 同一个数字，留 5d 余量）。
   太短了会怎样：慢烧告警（1h+30d 双窗口）只能看到保留期内的部分样本 → **比率失真或为空、静静地不生效**。

两条路径**别同时启用**（会重复抓取、告警双发）。

## 没有 Operator 时怎么用 static_configs 代替

`servicemonitor.yaml` 是 **Prometheus Operator 的 CRD**，只有集群里装了 Operator
（如 kube-prometheus-stack）才能 apply——否则 `kubectl apply` 会报
`no matches for kind "ServiceMonitor"`。**本地 / 小集群 / 手装 Prometheus 的替代做法**：
直接在 `prometheus.yml` 里写静态抓取目标（本仓已有的这条就是范例，可直接照抄）：

```yaml
scrape_configs:
  - job_name: warden-agent
    scheme: http
    metrics_path: /metrics
    # 因为 /metrics 受鉴权保护，必须带 Bearer；token 文件内容 = 一个有效 API Key
    bearer_token_file: /etc/prometheus/warden_token
    static_configs:
      - targets: ["warden-agent.warden.svc.cluster.local:80"]   # Service 的 80 -> 8000
        labels:
          service: warden-agent
```

生成 token 与挂载方式（同 `docker-compose.yml` 里的做法）：

```bash
printf '%s' "$WARDEN_API_KEY" > deploy/observability/warden_token   # 已 .gitignore，别提交
# 集群里建议把 token 放 Secret，再挂成文件（不要写明文进 prometheus.yml）
```

> 两条路径**别同时启用**（会重复抓取，`up` 计数与 SLO 都会翻倍）。
> ServiceMonitor 路需要额外两件事：(1) Service **必须有带名字的 port**（本仓 `service.yaml` 的
> `name: http`）；(2) ServiceMonitor 要带 `release: <你的 prometheus release>` 标签
> 才会被 Operator 选中（见 `servicemonitor.yaml` 注释）。
> **第三件事**：静态路径的 `rule_files` 在 Operator 路径上等效于 `prometheusrule.yaml`，
> 漏了它则只有指标、没有告警（见上节）。

## 备份失败 / 静默失效的告警（代理层，非 warden_* 指标）

`deploy/k8s/backup-cronjob.yaml` 的备份失败**不会**产生 `warden_*` 指标
（那是 kube-state-metrics 的事实），所以不能写进 `alerts/*.rules.yml`
（那条规则库有测试断言：引用的每个指标必须真实存在于代码里）。请另加两条：

- 备份 Job 失败：`kube_job_status_failed{job_name=~"warden-pg-backup.*"} > 0`
- **长期没成功过**（更关键的静默失效）：`time() - kube_cronjob_status_last_successful_time{cronjob="warden-pg-backup"} > 26*3600`

接线位置：你的 Prometheus 能抓到 kube-state-metrics 即可，路由沿用 `alertmanager.yml`
（severity 分档），不需要改本仓任何规则文件。

## 口径与边界（别误读）

- **阈值是起点，不是真理**：先按默认跑，再按你业务的真实水位调。
- `WardenReadinessFailing` 依赖 **blackbox_exporter**（`probe_success`）。不用 blackbox 就删掉
  该规则和 `prometheus.yml` 里对应的 job。
- `warden_stuck_runs` 是**抓取时现算**（对存储做一次只读扫描）——多副本/重启都不会让它漂移，
  代价是每次抓取扫一次库。规模大了应改为物化视图 / 后台刷新。
- **`/metrics` 受鉴权保护**：任何持有 API Key 的调用者都能看到全局指标（含跨租户的聚合值）。
  它定位是**运维出口**；若要更严的隔离，应把指标口放到独立端口 + 网络策略，只给 Prometheus。
