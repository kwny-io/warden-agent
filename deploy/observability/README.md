# 可观测性整栈（告警规则库 / SLO / 演练）

这一目录把"能看"升级成"能运营"：**告警规则库**（哪些失效模式必须有人管）、**SLO 与错误预算**
（可靠性承诺还剩多少余量）、以及把两者真正加载起来的 Prometheus/Alertmanager 接线。

| 文件 | 作用 |
|---|---|
| `alerts/warden.rules.yml` | 告警规则库：可用性 / 错误率 / 延迟 / 业务（挂起 Run、限流激增） |
| `alerts/warden.slo.yml` | SLO 记录规则（可用性、延迟）+ 多窗口燃烧率告警 |
| `prometheus.yml` | 抓取配置（**带 Bearer**，因为 `/metrics` 受鉴权保护）+ 规则加载 + 告警转发 |
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

## 口径与边界（别误读）

- **阈值是起点，不是真理**：先按默认跑，再按你业务的真实水位调。
- `WardenReadinessFailing` 依赖 **blackbox_exporter**（`probe_success`）。不用 blackbox 就删掉
  该规则和 `prometheus.yml` 里对应的 job。
- `warden_stuck_runs` 是**抓取时现算**（对存储做一次只读扫描）——多副本/重启都不会让它漂移，
  代价是每次抓取扫一次库。规模大了应改为物化视图 / 后台刷新。
- **`/metrics` 受鉴权保护**：任何持有 API Key 的调用者都能看到全局指标（含跨租户的聚合值）。
  它定位是**运维出口**；若要更严的隔离，应把指标口放到独立端口 + 网络策略，只给 Prometheus。
