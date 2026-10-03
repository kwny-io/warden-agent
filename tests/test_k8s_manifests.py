"""K8s manifests 的**离线结构守卫**：把"review 时提的那些要求"变成会自己变红的断言。

为什么要有它（而不是只靠 kubeconform）：
  - `kubeconform` 校验的是**官方 schema**（字段合法性），它在 CI 里需要联网取 schema；
  - 但它**不管"我们的要求"**——比如"探针必须配"、"优雅停机时间必须小于
    `terminationGracePeriodSeconds`"、"不许用 `:latest`"、"rootfs 必须只读"。
    这些是 review 出来的结论，只有写成断言才不会在后续改动里悄悄丢。
  - 本测试**纯离线**（只解析 YAML），所以在任何环境都会跑。

官方 schema 校验的跑法写在 `deploy/k8s/README.md`（本地与 CI 各一条命令）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

K8S = Path(__file__).resolve().parents[1] / "deploy" / "k8s"


def _load_all() -> list[dict]:
    docs: list[dict] = []
    for path in sorted(K8S.glob("*.yaml")):
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if doc:
                docs.append(doc)
    return docs


def _by_kind(kind: str) -> dict:
    docs = [d for d in _load_all() if d.get("kind") == kind]
    assert docs, f"没找到 {kind}"
    return docs[0]


def test_每个资源都有基本字段() -> None:
    docs = _load_all()
    assert len(docs) >= 6, docs
    for d in docs:
        assert d.get("apiVersion"), d
        assert d.get("kind"), d
        assert d.get("metadata", {}).get("name"), d
        assert d["metadata"].get("labels", {}).get("app.kubernetes.io/name") == "warden-agent", d


def test_deployment_探针齐全且用对端点() -> None:
    c = _by_kind("Deployment")["spec"]["template"]["spec"]["containers"][0]
    live = c["livenessProbe"]["httpGet"]["path"]
    ready = c["readinessProbe"]["httpGet"]["path"]
    # 存活不查依赖（依赖问题归就绪）；就绪查存储
    assert live == "/health/live"
    assert ready == "/health/ready"


def test_优雅停机时间必须小于terminationGracePeriodSeconds() -> None:
    """否则 SIGTERM 到了、应用还在排空请求，就被 K8s 硬杀了（等于没有优雅停机）。"""
    deploy = _by_kind("Deployment")["spec"]["template"]["spec"]
    grace = int(deploy["terminationGracePeriodSeconds"])
    cfg = _by_kind("ConfigMap")["data"]
    app_grace = int(cfg["WARDEN_SHUTDOWN_GRACE_S"])
    assert app_grace < grace, f"应用排空 {app_grace}s 必须小于 K8s 给的 {grace}s"


def test_容器安全上下文_非root且只读rootfs且丢掉全部能力() -> None:
    pod_sc = _by_kind("Deployment")["spec"]["template"]["spec"]["securityContext"]
    c = _by_kind("Deployment")["spec"]["template"]["spec"]["containers"][0]
    c_sc = c["securityContext"]
    assert pod_sc["runAsNonRoot"] is True
    assert c_sc["allowPrivilegeEscalation"] is False
    assert c_sc["readOnlyRootFilesystem"] is True
    assert c_sc["capabilities"]["drop"] == ["ALL"]


def test_只读rootfs下必须有可写卷() -> None:
    """rootfs 只读 + 没有可写卷 = 应用写 SQLite/临时文件时直接崩。"""
    c = _by_kind("Deployment")["spec"]["template"]["spec"]["containers"][0]
    mounts = {m["mountPath"] for m in c["volumeMounts"]}
    assert "/data" in mounts and "/tmp" in mounts


def test_镜像不许用latest标签() -> None:
    """`:latest` 会让"回滚"变成"再拉一次、但不知道拉到什么"。"""
    image = _by_kind("Deployment")["spec"]["template"]["spec"]["containers"][0]["image"]
    assert not image.endswith(":latest")
    assert ":" in image.split("/")[-1], f"应当带明确 tag：{image}"


def test_多副本前提写在配置里_共享状态与PG() -> None:
    """多副本（replicas>1）却没有共享存储/共享协调状态 = 每个副本各算一份。"""
    replicas = _by_kind("Deployment")["spec"]["replicas"]
    if replicas <= 1:
        pytest.skip("单副本不需要这些开关")
    cfg = _by_kind("ConfigMap")["data"]
    assert cfg.get("WARDEN_PG_HOST"), "多副本必须配 PostgreSQL"
    assert cfg.get("WARDEN_SHARED_STATE") == "1", "多副本必须开共享协调状态"
    # 审计/记忆的后端跟着主存储走：PG 主存储 → PG 审计/记忆（多副本共享同一账本），
    # 所以多副本**必须**开审计（关掉等于多副本交付没有审计）。
    assert cfg.get("WARDEN_AUDIT") == "1", "PG 后端下审计应开启（多副本共享同一账本）"


def test_模糊重启策略_先起后停() -> None:
    strategy = _by_kind("Deployment")["spec"]["strategy"]["rollingUpdate"]
    assert strategy["maxUnavailable"] == 0, "滚动期间必须始终有副本在服务"
    assert strategy["maxSurge"] >= 1


def test_PDB至少留一个副本() -> None:
    assert _by_kind("PodDisruptionBudget")["spec"]["minAvailable"] >= 1


def test_HPA最小副本与部署一致() -> None:
    hpa = _by_kind("HorizontalPodAutoscaler")["spec"]
    assert hpa["minReplicas"] >= 2, "单副本的 HPA 等于没有高可用"
    assert hpa["maxReplicas"] > hpa["minReplicas"]


def test_ingress为SSE关了代理缓冲() -> None:
    """SSE 被网关缓冲攒批，前端的"打字机"就没了。"""
    ann = _by_kind("Ingress")["metadata"]["annotations"]
    assert ann["nginx.ingress.kubernetes.io/proxy-buffering"] == "off"


def test_密钥示例里没有真实凭据() -> None:
    """示例文件里的值必须是明显占位符——防止有人把真 key 填进去后一起提交。"""
    data = _by_kind("Secret")["stringData"]
    for key, value in data.items():
        assert "REPLACE_ME" in value, f"{key} 的值看起来不是占位符：{value!r}"


# —— 以下是 2026-09-27 交付审计（三路只读 reviewer）挑出来的"清单存在但清单跑不起来"类问题，
#    每条都对应一个**真实会炸的运行期后果**，所以固化成断言，避免下次改动又悄悄丢。——


# ServiceMonitor 不在 deploy/k8s 下，单独指路
_OBS = Path(__file__).resolve().parents[1] / "deploy" / "observability"


def _workloads() -> list[dict]:
    """所有会起 Pod 的资源（Deployment / Job / CronJob）。"""
    return [d for d in _load_all() if d.get("kind") in ("Deployment", "Job", "CronJob")]


def _pod_spec(wl: dict) -> Any:
    """取 Pod 模板的 spec：CronJob 比 Deployment/Job 多一层 jobTemplate。"""
    if wl["kind"] == "CronJob":
        return wl["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    return wl["spec"]["template"]["spec"]


def test_ServiceMonitor引用的Secret键必须真实存在() -> None:
    """引用一个默认不存在的 Secret key ⇒ 抓取 401 ⇒ `up{job=...}==0` 把 WardenServiceDown
    打成长期 critical，**真实告警反而被淹没**（比不监控更危险）。

    这一条就是审计里的 D-B4：ServiceMonitor 引用 `WARDEN_API_KEY`，而示例 Secret 只给
    `WARDEN_API_KEYS`（复数）——两个名字差一个字母，运行时才发现（而且是在 Prometheus 上发现）。
    """
    sm = yaml.safe_load((_OBS / "servicemonitor.yaml").read_text(encoding="utf-8"))
    ep = sm["spec"]["endpoints"][0]
    ref = ep["bearerTokenSecret"]
    assert ref["name"] == _by_kind("Secret")["metadata"]["name"], "引用的 Secret 名与示例不一致"
    keys = _by_kind("Secret")["stringData"]
    assert ref["key"] in keys, (
        f"ServiceMonitor 要的 Secret 键 {ref['key']!r} 不在 secret.example.yaml 里：{sorted(keys)}"
    )
    # /metrics 不在公开路径白名单（server.py:_is_public）——所以必须有 token，不能“以为公开”
    assert ep["path"] == "/metrics"


def test_所有工作负载引用的ServiceAccount必须存在() -> None:
    """SA 不存在 ⇒ Pod 卡在 `serviceaccount "warden-agent" not found`。

    审计里的 D-B1：deployment / migrate-job / backup-cronjob 都写死了
    `serviceAccountName: warden-agent`，但 `serviceaccount.yaml` 从未被创建（连文档都没提
    要 apply 它）⇒ **照文档走必然发布失败**。
    """
    names = {
        d["metadata"]["name"]
        for d in _load_all()
        if d.get("kind") == "ServiceAccount"
    }
    for wl in _workloads():
        sa = _pod_spec(wl).get("serviceAccountName")
        if sa is None:
            continue
        who = f"{wl['kind']}/{wl['metadata']['name']}"
        assert sa in names, f"{who} 引用了不存在的 ServiceAccount {sa!r}"


def test_部署文档必须包含命名空间与SA这两步() -> None:
    """D-B1：README 的分步顺序里缺 namespace.yaml / serviceaccount.yaml。

    为什么不能只靠"清单对不对"：这一步错了，`kubectl wait --for=condition=complete` 必然超时，
    按文档自己的约定 `exit 1` 停发布——**文档就是部署脚本**，所以文档也要测。
    """
    text = (K8S / "README.md").read_text(encoding="utf-8")
    assert "apply -f deploy/k8s/namespace.yaml" in text, (
        "部署顺序缺 namespace.yaml（PSA/配额/LimitRange 会一起失效）"
    )
    assert "apply -f deploy/k8s/serviceaccount.yaml" in text, "部署顺序缺 serviceaccount.yaml"
    for name in ("namespace.yaml", "serviceaccount.yaml"):
        assert f"| `{name}` |" in text, f"文件表缺 {name} 一行"


def test_清单里不许留全0占位digest() -> None:
    """`postgres:16-alpine@sha256:000…0` 能过任何语法校验，却让 Pod 调度时 `manifest unknown`
    ——备份会"看起来配了、其实从没产出过"（审计 D-B2）。"""
    bad = "sha256:" + "0" * 64
    for path in sorted(K8S.glob("*.yaml")) + sorted(_OBS.glob("*.yaml")):
        assert bad not in path.read_text(encoding="utf-8"), f"{path.name} 里还有全 0 占位 digest"


def test_备份保留策略不许按mtime删() -> None:
    """`find -mtime +14 -delete` 没有"最少保留份数"下限：卷里出现被 touch / 被 pg_restore
    写过的文件、或节点时钟漂移，就可能删掉唯一可用的备份（删备份不可逆，比不删危险）。"""
    cj = _by_kind("CronJob")
    script = "\n".join(_pod_spec(cj)["containers"][0]["args"])
    # 只看真命令，不看注释（注释里会提到被否掉的写法）
    dangerous = [
        ln.strip()
        for ln in script.splitlines()
        if not ln.strip().startswith("#") and "find" in ln and "-delete" in ln
    ]
    assert not dangerous, f"备份保留不许用 find -delete（按 mtime、无最少保留份数）：{dangerous}"
    assert "pruned:" in script or "backup-prune" in script, "保留策略应显式按时间戳保留固定份数"


def test_备份保留份数在文档与脚本里同源() -> None:
    """同一件事在两个文件里被写成两个数（或两种语义）= 运维按文档核对会得出相反结论。

    实际发生过：`deploy/k8s/README.md` 写"保留窗口是 **14 天**（`find ... -mtime +14 -delete`）"
    ——既把"14 **份**"说成了"14 **天**"，又把代码里**刻意否掉**的危险写法当成事实教给运维。
    所以这里同时钉两件事：份数同源 + 文档不得再教被否掉的写法。
    """
    cj = _by_kind("CronJob")
    script = "\n".join(_pod_spec(cj)["containers"][0]["args"])
    match = re.search(r"-gt\s+(\d+)", script)
    assert match, f"找不到保留份数的判定（形如 `-gt 14`）：{script[:200]}"
    keep = int(match.group(1))
    assert f"--keep {keep}" in script, (
        f"保留份数 {keep} 应与 CLI 口径 `warden backup-prune --keep N` 写明"
    )
    readme = (K8S / "README.md").read_text(encoding="utf-8")
    assert f"{keep} 份" in readme, f"部署文档应写明保留的是「最新 {keep} 份」（不是「{keep} 天」）"
    # 文档可以把被否掉的写法当**反面例子**讲，但不许把它当事实教给运维：
    # 提到 `-mtime` 的每一行都必须带否定语境（实测发生过：README 写"保留窗口是 14 天
    # （find ... -mtime +14 -delete）"，而代码早就不那么做了）。
    # ⚠️ 这是**文本级**守卫，不装成行为级：它拦的是"再把那个写法写成事实"，不是所有错法。
    for line in readme.splitlines():
        if "-mtime" in line:
            assert any(cue in line for cue in ("不用", "别用", "不是", "禁止", "刻意")), (
                "部署文档把 `-mtime ... -delete` 当成事实讲"
                f"（而不是「为什么不用它」）：{line.strip()}"
            )
    # 第五十三批补：上面那条只查 **带 `-mtime` 的行**，于是**同一句话的另一个写法漏网了**——
    # 文件表格里还写着 `backup-cronjob.yaml` 是「14 天保留」（真实口径是「最新 14 份」），
    # 而它既不含 `-mtime`、也含了 `14 份`（在别处）⇒ 旧守卫全绿。
    # 判据：`N 天` 这种口径**在部署文档里只允许出现在被否定的语境里**。
    for line in readme.splitlines():
        if f"{keep} 天" in line:
            assert any(cue in line for cue in ("不是", "不用", "别用", "禁止", "刻意")), (
                f"部署文档把保留口径写成「{keep} 天」（真实口径是「最新 {keep} 份」，"
                f"按文件名时间戳而不是 mtime）：{line.strip()}"
            )


# —— 第五十三批：部署模板里的占位符不是「还没配」 ——

def _placeholder_values(doc: dict) -> dict[str, str]:
    """模板里所有含占位符的 `键 -> 值`（ConfigMap 用 `data`，Secret 用 `stringData`）。"""
    data = doc.get("data") or doc.get("stringData") or {}
    return {
        k: v
        for k, v in data.items()
        if isinstance(v, str) and "replace" in v.lower()
    }


def test_模板里的占位符必须过不了启动校验() -> None:
    """留占位符是**有意**的（防有人把真值提交上来），但它必须过不去启动校验。

    真实失败模式（第五十三批实测，未改之前）：`configmap.yaml` 的
    `WARDEN_TRUSTED_PROXIES: "REPLACE_ME-pod-cidr"` **穿过** `validate_env()` ——
    它只按类型查格式，而 `proxies` 那类**故意**放行字面量（为了兼容 unix socket 名），
    于是应用照常启动，还打印「受信前置代理: REPLACE_ME-pod-cidr（只采信来自它们的
    X-Forwarded-For）」= **声称信任已开**；而 uvicorn 的 `_TrustedHosts` 把非法 IP
    当字面量、没任何对端能匹配 ⇒ `X-Forwarded-For` 静默丢弃 ⇒ 第三十九批刚修好的
    「限流按调用者分桶 / Idempotency-Key 不串响应体」在**照着本目录部署**时静默退回。

    这条守卫钉的是「**模板 ↔ 门禁**」这对关系：模板里新加一个占位符却没人接住，它会红。
    """
    from warden_agent.core.settings import validate_env

    checked = 0
    for kind in ("ConfigMap", "Secret"):
        for key, value in sorted(_placeholder_values(_by_kind(kind)).items()):
            errors = validate_env({key: value})
            assert errors, (
                f"{kind} 的 {key}={value!r} 能穿过启动校验："
                "占位符会被当成真实取值一路用下去（表现是「日志说配了、机制没生效」）"
            )
            assert "占位符" in errors[0], f"报错该说清是占位符：{errors[0]}"
            checked += 1
    assert checked >= 4, f"占位符守卫看起来是空跑的（只检查到 {checked} 个）"


def test_占位符键都在部署文档里被点名() -> None:
    """占位符必须是「照着文档就能填完」的：部署文档得点名每一个占位符键。

    反面例子（第五十三批实测）：`secret.example.yaml` 里的 `WARDEN_METRICS_BEARER`
    **在 `deploy/k8s/README.md` 里根本没出现**——README 写的是 `WARDEN_METRICS_TOKEN`
    （一个不存在于任何文件里的名字），而 `servicemonitor.yaml` 的 `bearerTokenSecret.key`
    要的是 `…_BEARER` ⇒ 照文档配就抓不到 `/metrics`（401）⇒ `up==0` 把 WardenServiceDown
    打成长期 critical，**真告警被淹没**。
    """
    readme = (K8S / "README.md").read_text(encoding="utf-8")
    missing: list[str] = []
    for kind in ("ConfigMap", "Secret"):
        for key in sorted(_placeholder_values(_by_kind(kind))):
            if key not in readme:
                missing.append(f"{kind}/{key}")
    assert not missing, f"这些占位符键没在 deploy/k8s/README.md 里被点名：{missing}"


def test_ServiceMonitor要的Secret键必须被部署文档点名() -> None:
    """yaml 那对已有守卫（引用的键必须真实存在），**「文档–yaml」这对之前没有**。

    所以 `servicemonitor.yaml:48` 要 `WARDEN_METRICS_BEARER`、而 README 教人写
    `WARDEN_METRICS_TOKEN` 这个错误名字，两边各自都不报错，只有真集群里才会因为
    401/`up==0` 表现出来（而且表现成「服务挂了」而不是「名字写错了」）。
    """
    sm = yaml.safe_load((_OBS / "servicemonitor.yaml").read_text(encoding="utf-8"))
    key = sm["spec"]["endpoints"][0]["bearerTokenSecret"]["key"]
    readme = (K8S / "README.md").read_text(encoding="utf-8")
    assert key in readme, (
        f"ServiceMonitor 要的 Secret 键 {key!r} 没在 deploy/k8s/README.md 里出现："
        "照文档配的人会写另一个名字，抓取 401 却很难看出是名字问题"
    )
