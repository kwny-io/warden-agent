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

from pathlib import Path

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
    assert cfg.get("WARDEN_AUDIT") == "0", "审计目前只有 SQLite 实现，多副本下应先关（见已知边界）"


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
