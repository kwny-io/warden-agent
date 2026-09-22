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
> - **审计与记忆目前只有 SQLite 实现**：多副本下每个副本一份、彼此不一致
>   （审计链各自独立、记忆各自一套）。所以参考配置里 `WARDEN_AUDIT=0`；
>   要"多副本 + 审计/记忆"需要它们的 PG 实现（尚未做）。

## 文件

| 文件 | 作用 |
|---|---|
| `deployment.yaml` | 2 副本、滚动更新先起后停、探针、非 root + rootfs 只读、`/data` 与 `/tmp` 可写卷 |
| `configmap.yaml` | 非敏感配置：PG 连接、共享状态、事件总线、停机宽限、管理员名单 |
| `secret.example.yaml` | 密钥**示例**（值全是 `REPLACE_ME`），用前替换或改用外部密钥服务 |
| `service.yaml` / `ingress.yaml` | ClusterIP + 入口（SSE 关缓冲、会话粘性） |
| `hpa.yaml` / `pdb.yaml` | 按 CPU 扩缩（2~6）、维护时至少留 1 个副本 |

## 用之前要替换的（环境相关）

1. **镜像**：`deploy/k8s/deployment.yaml` 的 `image:` → 你的仓库与 tag（**别用 `:latest`**）。
2. **PostgreSQL**：`configmap.yaml` 的 `WARDEN_PG_HOST`；密码放 Secret 的 `WARDEN_PG_PASSWORD`。
   没现成的库就用云厂商托管 PG，或另起一个 StatefulSet（不在本目录范围）。
3. **鉴权**：`secret.example.yaml` 的 `WARDEN_API_KEYS`（`用户id:密钥` 逗号分隔）。
   **必须配**——对外监听 + 无鉴权时应用会 fail-closed 拒绝启动。
4. **管理员**：`configmap.yaml` 的 `WARDEN_ADMIN_PRINCIPALS`（不配就没有管理员）。
5. **域名/TLS**：`ingress.yaml` 的 host 与 `secretName`（以及 `ingressClassName` 按你的网关改）。
6. **容量**：`resources` 的 requests/limits 按你的实测调（**先跑 `scripts/load_test.py` 拿数字**，
   别照抄这里的 200m/1 核）。

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

## 部署顺序建议

```bash
kubectl create namespace warden
kubectl -n warden create secret generic warden-secrets --from-literal=...   # 见 secret.example.yaml 顶部
kubectl -n warden apply -f deploy/k8s/            # ConfigMap/Secret 之外的都在这
kubectl -n warden rollout status deploy/warden-agent
```

升级用**镜像 tag** 换新版滚动（`maxUnavailable: 0`，滚动期间不中断）；
回滚 `kubectl -n warden rollout undo deploy/warden-agent`。
灰度的**自动化门控**在 `scripts/canary_rollout.sh`（它只做"能不能放量"的判定，
权重切换在你网关侧），可观测性栈见 `deploy/observability/`。
