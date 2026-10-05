"""清单与清单之间的**对接点**必须互相对得上——写错一个字母，K8s 不会报错。

为什么需要这个文件（已有的两个守卫都在别的层面）：
  - `tests/test_k8s_manifests.py` 查的是"我们自己提的那些要求"（标签在不在、探针配没配、
    ServiceAccount 存不存在、Secret 键对不对）；
  - `tests/test_crd_schemas.py` 查的是**字段合法性**（官方 schema）。

  两者都在"**单个文件自己的形状**"这一层。而清单之间是靠**字符串**对接的：

    `Service.spec.selector`        选中 Pod 的标签
    `Service.ports[].targetPort`   指向容器的端口**名**
    `Ingress ...backend.service`   用 Service 的**名字** + **端口名**指过去
    `PDB.spec.selector`            自己一份选择器
    `NetworkPolicy.spec.podSelector` 自己一份选择器
    `ServiceMonitor.spec.selector` 又一份 + `endpoints[].port` 是 Service 端口名
    `HPA.spec.scaleTargetRef`      指向 Deployment 的**名字**
    `envFrom.configMapRef/secretRef`、`persistentVolumeClaim.claimName`  各自指向对象名

  这些字符串错一个字母，K8s **不会报错**：Pod 照样全 Ready、`kubectl apply` 照样成功、
  也没有任何告警——因为选择器的作用是"选中谁"，**选不中就是空集，而空集完全合法**。
  结果是：流量断了（Service 没有端点）/ PDB 形同虚设（drain 时照删）/
  网络策略管不到这个 Pod / 告警抓不到（ServiceMonitor 选不中）。

第六十二批的两条取证：
  1. 本次改动前，全仓**没有一条断言**读过 `spec.selector` / `ports[].targetPort` /
     `scaleTargetRef` 这些字符串（本次只加了这个文件）。另外两处 grep 命中都在别的层面：
     `tests/test_observability_config.py` 查的是 Operator 的 **release 标签选择**层
     （`ruleSelector` / `serviceMonitorSelector` 能不能选中），
     `tests/test_crd_schemas.py` 只在注释里提过一次 namespaceSelector。
     ——也就是说"选择器指向谁"这层，之前只靠人眼看；
  2. 真集群上一次偶然现场（本次改动过程中我自己的手误）：一个把
     `app.kubernetes.io/name` 写成 `app.kubernetes.io/names` 的 PDB 被 `kubectl apply`
     **成功接受**，`kubectl get pdb` 只是把 `expectedPods`/`currentHealthy` 显示成空
     ——没有 warning、没有事件、没有任何"我没选中任何 Pod"的提示。
     （该对象在 `default` 命名空间，已删除；生产 `warden` 命名空间一个字节没动，有输出为证。）

判据怎么来的：**不把字符串抄第二遍**。Pod 标签、容器端口名、Deployment 名、命名空间名
只在事实源（`deployment.yaml` / `namespace.yaml`）里读一次，其它清单只能**引用**它们。
于是"改 Pod 标签名"这类改动会在同一次运行里让所有引用它的清单一起变红。

诚实边界（别把结论说大）：
  - 本文件是**静态**判据（不连集群、不需要 Docker、不联网）。它证的是
    "这些字符串互相对得上"，**不证明**集群里真的通了——那需要真集群演练
    （`scripts/k8s_rollback_drill.sh` 那条路）。
  - 变异测试（`[tool.mutmut]`）只改 Python 源码，**看不见 YAML** ⇒ 这条守卫没有变异
    门禁兜底。所以"它会红吗"是手工量出来的（CHANGELOG 第六十二批第二节）：

    | 手工变异 | 旧三个守卫 | 本文件 |
    | --- | --- | --- |
    | M1 `Service.selector` 少一个字母 | 没拦住 | 拦住 |
    | M2 Service 端口名 `http`→`web` | 没拦住 | 拦住 |
    | M3 `targetPort: http`→`htp` | 没拦住 | 拦住 |
    | M4 `PDB.selector` 少一个字母 | 没拦住 | 拦住 |
    | M5 `PDB.minAvailable: 1`→`3`（≥replicas 2） | 没拦住 | 拦住 |
    | M6 `HPA.scaleTargetRef.name` 改名 | 没拦住 | 拦住 |
    | M7 `NetworkPolicy.podSelector` 少一个字母 | 没拦住 | 拦住 |
    | M8 `ServiceMonitor.endpoints[].port` 改名 | 没拦住 | 拦住 |
    | M9 `envFrom.configMapRef.name` 改名 | 没拦住 | 拦住 |
    | M10 备份 CronJob `claimName` 改名 | 没拦住 | 拦住 |
    | M11 盘上多一个带选择器的清单 | 没拦住 | 拦住（范围守卫） |

    （旧三个守卫 = `test_k8s_manifests.py` + `test_crd_schemas.py` +
    `test_observability_config.py`；每个变异都是字节级替换、跑完立即从原始字节还原，
    并用 sha256 验证是逐字节还原。）

    M11 的探针必须**带上本仓的标签约定**才算公平：一个连
    `app.kubernetes.io/name: warden-agent` 都没有的新清单，会先被 `test_k8s_manifests.py`
    里「每个清单都要带这个名字标签」那条抓住——它抓的是**标签**，不是对接点。

    同时说清它的两个弱点：
    1. 本文件读的是"我们自己写的清单"，所以"该有的清单**根本不存在**"（比如谁把
       `pdb.yaml` 删了）只有 `test_k8s_manifests.py` 的显式存在性断言能抓——两者不互相替代；
    2. 它只比字符串，不比**语义**：把 `minAvailable` 从 1 改成 2（等于 replicas、仍然卡 drain）
       这一条只能靠上面的范围断言（`minAvailable < replicas`）抓，不是靠对账抓。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[1]
_K8S = _REPO / "deploy" / "k8s"
_OBS = _REPO / "deploy" / "observability"


def _docs(directory: Path) -> list[tuple[str, dict[str, Any]]]:
    """目录下所有清单文档（多文档文件按顺序展开）。"""
    out: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(directory.glob("*.yaml")):
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if doc:
                out.append((path.name, doc))
    return out


def _one(directory: Path, filename: str, kind: str) -> dict[str, Any]:
    """取一个明确的文档；取不到就直接报错——"事实源不见了"必须是红，不是跳过。"""
    for name, doc in _docs(directory):
        if name == filename and doc.get("kind") == kind:
            return doc
    raise AssertionError(f"{directory.name}/{filename} 里没有 {kind}：事实源不见了")


# ── 事实源：只在下面这几行读一次，其余断言全部从这里推出去 ─────────────────────────
_DEPLOY = _one(_K8S, "deployment.yaml", "Deployment")
_DEPLOY_NAME: str = _DEPLOY["metadata"]["name"]
_REPLICAS: int = _DEPLOY["spec"]["replicas"]
_POD_TEMPLATE: dict[str, Any] = _DEPLOY["spec"]["template"]
_POD_LABELS: dict[str, str] = _POD_TEMPLATE["metadata"]["labels"]
_CONTAINER_SPECS: list[dict[str, Any]] = _POD_TEMPLATE["spec"]["containers"]

# 容器端口的**名字**（Ingress / Service.targetPort / ServiceMonitor 都按名字对接）
_CONTAINER_PORTS: tuple[str, ...] = tuple(
    port["name"] for c in _CONTAINER_SPECS for port in c.get("ports", []) if "name" in port
)

_SVC = _one(_K8S, "service.yaml", "Service")
_SVC_PORTS: tuple[str, ...] = tuple(p["name"] for p in _SVC["spec"]["ports"] if "name" in p)
_SVC_PORT_NUMBERS: tuple[int, ...] = tuple(p["port"] for p in _SVC["spec"]["ports"] if "port" in p)

_NS_NAME: str = next(
    doc["metadata"]["name"] for _, doc in _docs(_K8S) if doc.get("kind") == "Namespace"
)

# 目录里所有对象的 (kind, name)，给"引用的名字必须存在"用
_KIND_NAMES: dict[str, set[str]] = {}
for _, _doc in _docs(_K8S):
    _KIND_NAMES.setdefault(_doc.get("kind", "?"), set()).add(_doc["metadata"]["name"])


def _体检() -> None:
    """守卫自己的体检。

    第六十批的教训：**守卫自己也会撒谎**——解析失败时返回空集合，断言在空集合上"通过"，
    于是"什么都没查"和"全都对"长得一模一样。所以每次断言前先证明事实源真的读到了。
    """
    assert _POD_LABELS, "没解析到 Pod 模板标签——守卫坏了（不是清单坏了）"
    assert _CONTAINER_PORTS, "没解析到容器端口名——守卫坏了：端口名是四处对接的锚点"
    assert _SVC_PORTS, "没解析到 Service 端口名——守卫坏了"
    assert _SVC_PORT_NUMBERS, "没解析到 Service 端口号——守卫坏了"
    assert _NS_NAME, "没解析到命名空间名——守卫坏了"
    assert _REPLICAS >= 1, f"replicas 解析成 {_REPLICAS!r}——守卫坏了"


def _选择器必须选中Pod标签(who: str, selector: dict[str, Any]) -> None:
    """`selector.matchLabels` 必须是 Pod 模板标签的子集，且不许为空。"""
    assert selector, (
        f"{who} 的选择器是空的。空选择器在 K8s 里合法（等于「什么都选中」或「什么都不选中」），"
        "但语义完全不是你想的那个——所以这里直接判红"
    )
    matched_by = selector.get("matchLabels", selector)
    assert matched_by, f"{who} 的选择器 {selector!r} 里没有 matchLabels"
    missing = {k: v for k, v in matched_by.items() if _POD_LABELS.get(k) != v}
    assert not missing, (
        f"{who} 的选择器 {missing!r} 选不中任何 Pod（Pod 模板标签是 {_POD_LABELS!r}）。"
        "K8s 不会因为'选不中'而报错——Pod 照样 Ready、apply 照样成功，"
        "但这个对象会静默失效"
    )


def test_Deployment自己的选择器必须选中自己的Pod模板() -> None:
    """`spec.selector` 与 `spec.template.metadata.labels` 对不上 = Deployment 直接不可用。

    这一条 API server 会拒（selector 是不可变字段），但拒的方式是**发不出去**；
    而模板标签被改名（比如为了做灰度）时，选择器会悄悄只选中一部分 Pod。
    """
    _体检()
    _选择器必须选中Pod标签(
        "deployment.yaml 的 Deployment.spec.selector", _DEPLOY["spec"]["selector"]
    )


def test_Service的选择器必须选中Pod的标签() -> None:
    """选不中 ⇒ EndpointSlice 里一个就绪地址都没有 ⇒ 整条流量路径断掉，而 Pod 全绿。"""
    _体检()
    _选择器必须选中Pod标签("service.yaml 的 Service.spec.selector", _SVC["spec"]["selector"])


def test_Service的每个端口都必须有名字且targetPort指向容器端口名() -> None:
    """端口**名字**是两条对接的锚点：Ingress 按名字指 Service、Service 按名字指容器。

    没名字的端口没法被 `port: {name: ...}` 引用——而这一条在 `kubectl apply` 时不会报错，
    只有 Ingress 真正转发时才 503。
    """
    _体检()
    for i, port in enumerate(_SVC["spec"]["ports"]):
        who = f"service.yaml 的第 {i} 个端口 {port!r}"
        assert port.get("name"), f"{who} 没有名字：Ingress 与 ServiceMonitor 只能按名字引用它"
        target = port.get("targetPort", port.get("port"))
        if isinstance(target, str):
            assert target in _CONTAINER_PORTS, (
                f"{who} 的 targetPort={target!r} 不是任何容器的端口名（"
                f"容器端口名有 {_CONTAINER_PORTS!r}）：K8s 接受它，但转发时会连不上后端"
            )
        else:
            declared = {
                port.get("containerPort") for c in _CONTAINER_SPECS for port in c.get("ports", [])
            }
            assert target in declared, (
                f"{who} 的 targetPort={target!r} 不是任何容器声明的 containerPort"
                f"（可选值 {sorted(declared)}）"
            )


def test_Ingress必须指向存在的Service与存在的端口名() -> None:
    """Ingress 的 backend 是一对字符串（Service 名 + 端口名），错一个就是 503 + 没有端点。"""
    _体检()
    ingress = _one(_K8S, "ingress.yaml", "Ingress")
    backends: list[dict[str, Any]] = []
    if "defaultBackend" in ingress["spec"]:
        backends.append(ingress["spec"]["defaultBackend"])
    for rule in ingress["spec"].get("rules", []):
        for path in (rule.get("http") or {}).get("paths", []):
            backends.append(path["backend"])
    assert backends, "ingress.yaml 里没有任何 backend——守卫坏了，或者清单被改空了"
    for backend in backends:
        svc_ref = backend["service"]
        assert svc_ref["name"] in _KIND_NAMES.get("Service", set()), (
            f"Ingress 指向 Service {svc_ref['name']!r}，但 deploy/k8s 里没有这个名字"
        )
        port = svc_ref["port"]
        if "name" in port:
            assert port["name"] in _SVC_PORTS, (
                f"Ingress 用端口名 {port['name']!r} 指 Service，但 Service 的端口名是 "
                f"{_SVC_PORTS!r}——名字对不上时 Ingress 会 503，而两端各自都不报错"
            )
        else:
            assert port["number"] in _SVC_PORT_NUMBERS, (
                f"Ingress 用端口号 {port['number']!r} 指 Service，但 Service 暴露的是 "
                f"{_SVC_PORT_NUMBERS!r}"
            )


def test_PDB的选择器必须选中Pod的标签且不许把驱逐全挡住() -> None:
    """两个真实失败模式：选择器选不中（PDB 形同虚设）+ minAvailable ≥ replicas（drain 卡死）。"""
    _体检()
    pdb = _one(_K8S, "pdb.yaml", "PodDisruptionBudget")
    _选择器必须选中Pod标签("pdb.yaml 的 PodDisruptionBudget.spec.selector", pdb["spec"]["selector"])
    min_available = pdb["spec"].get("minAvailable")
    assert min_available is not None, "pdb.yaml 没写 minAvailable（本次只用这一种写法）"
    assert min_available < _REPLICAS, (
        f"minAvailable={min_available} ≥ replicas={_REPLICAS}："
        "任何一次自愿驱逐都会被拒——`kubectl drain` 会**永远卡住**，"
        "而现场表现是「节点维护做不下去」，不是「PDB 配错了」"
    )


def test_NetworkPolicy必须管到Pod且两个方向都纳管() -> None:
    """`podSelector` 选不中 ⇒ 这份"默认全拒 + 白名单"的策略管的是一个空集合。

    那比"没有策略"更危险：**看文件的人以为默认全拒已经生效了**。
    """
    _体检()
    np = _one(_K8S, "networkpolicy.yaml", "NetworkPolicy")
    _选择器必须选中Pod标签(
        "networkpolicy.yaml 的 NetworkPolicy.spec.podSelector", np["spec"]["podSelector"]
    )
    assert set(np["spec"]["policyTypes"]) == {"Ingress", "Egress"}, (
        f"policyTypes={np['spec']['policyTypes']!r}：只写一个方向时，另一个方向是**全放行**，"
        "而不是「没策略」——声明与行为不一样"
    )


def test_HPA必须指向存在的Deployment且副本数区间自洽() -> None:
    """`scaleTargetRef.name` 错一个字母 ⇒ HPA 找不到目标（这次 K8s 会报错，但只在 status 里）；
    `replicas` 落在 [minReplicas, maxReplicas] 之外 ⇒ 一起步就被 HPA 立刻拉走。
    """
    _体检()
    hpa = _one(_K8S, "hpa.yaml", "HorizontalPodAutoscaler")
    target = hpa["spec"]["scaleTargetRef"]
    assert target["kind"] == _DEPLOY["kind"], f"HPA 指的不是 Deployment：{target!r}"
    assert target["name"] == _DEPLOY_NAME, (
        f"HPA 指向 {target['name']!r}，而 Deployment 叫 {_DEPLOY_NAME!r}"
    )
    assert target["name"] in _KIND_NAMES.get("Deployment", set()), (
        f"HPA 指向的 Deployment {target['name']!r} 在 deploy/k8s 里不存在"
    )
    lo, hi = hpa["spec"]["minReplicas"], hpa["spec"]["maxReplicas"]
    assert lo <= _REPLICAS <= hi, (
        f"deployment.replicas={_REPLICAS} 不在 HPA 的 [{lo}, {hi}] 区间里："
        "发布脚本刚铺开的副本数会被 HPA 立刻改掉（制造一次没人改过的「漂移」）"
    )


def test_ServiceMonitor的选择器端口名与命名空间必须三处自洽() -> None:
    """Prometheus Operator 的三个字符串：选中谁（selector）、抓哪个端口（endpoints[].port）、
    去哪个命名空间找（namespaceSelector）。

    三处任何一处错，表现都是**同一个**：`up{job=...} == 0`，看起来像"服务挂了"，
    于是真实告警被淹没（这正是 `test_k8s_manifests.py` 里 D-B4 那条的同一族）。
    """
    _体检()
    sm = _one(_OBS, "servicemonitor.yaml", "ServiceMonitor")
    _选择器必须选中Pod标签(
        "servicemonitor.yaml 的 ServiceMonitor.spec.selector", sm["spec"]["selector"]
    )
    assert sm["spec"]["endpoints"], "ServiceMonitor 没有 endpoints——守卫坏了，或清单被改空"
    for i, ep in enumerate(sm["spec"]["endpoints"]):
        assert ep.get("port") in _SVC_PORTS, (
            f"Servicemonitor 第 {i} 个 endpoint 的 port={ep.get('port')!r} 不是 Service 的端口名 "
            f"{_SVC_PORTS!r}：Prometheus 会持续抓不到，而两端各自都不报错"
        )
    own_ns = sm["metadata"].get("namespace")
    assert own_ns, "servicemonitor.yaml 没写 metadata.namespace"
    assert own_ns == _NS_NAME, (
        f"ServiceMonitor 自己在 {own_ns!r}，而 deploy/k8s 声明的命名空间是 {_NS_NAME!r}"
    )
    match_names = (sm["spec"].get("namespaceSelector") or {}).get("matchNames") or []
    assert own_ns in match_names, (
        f"ServiceMonitor 的 namespaceSelector.matchNames={match_names!r} 里没有它自己所在的 "
        f"{own_ns!r}（Namespace 上的 kubernetes.io/metadata.name 标签就是它自己的名字）"
        "——选择器会选到空集合"
    )


def test_工作负载引用的ConfigMap_Secret_PVC_都必须存在() -> None:
    """`envFrom` / `claimName` 是名字对接；写错了 Pod 起不来（这次会报错，但是**发布时**才报）。"""
    _体检()
    cm_names = _KIND_NAMES.get("ConfigMap", set())
    secret_names = _KIND_NAMES.get("Secret", set())
    pvc_names = _KIND_NAMES.get("PersistentVolumeClaim", set())
    assert cm_names and secret_names and pvc_names, "守卫坏了：没解析到 ConfigMap/Secret/PVC"

    seen_refs = 0
    for fname, doc in _docs(_K8S):
        if doc.get("kind") not in ("Deployment", "Job", "CronJob"):
            continue
        for container in _walk_containers(doc):
            for src in container.get("envFrom", []):
                seen_refs += 1
                if "configMapRef" in src:
                    name = src["configMapRef"]["name"]
                    assert name in cm_names, f"{fname} 引用的 ConfigMap {name!r} 不存在"
                if "secretRef" in src:
                    name = src["secretRef"]["name"]
                    assert name in secret_names, f"{fname} 引用的 Secret {name!r} 不存在"
        for volume in _walk_volumes(doc):
            if "persistentVolumeClaim" in volume:
                seen_refs += 1
                name = volume["persistentVolumeClaim"]["claimName"]
                assert name in pvc_names, f"{fname} 引用的 PVC {name!r} 不存在"
    assert seen_refs, "一个 envFrom/claimName 都没扫到——守卫坏了（不是清单没引用）"


def _walk_containers(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """把 Deployment / Job / CronJob 三种形状里的容器取出来（CronJob 多两层）。"""
    spec = doc["spec"]
    if doc["kind"] == "CronJob":
        spec = spec["jobTemplate"]["spec"]
    return list(spec["template"]["spec"]["containers"])


def _walk_volumes(doc: dict[str, Any]) -> list[dict[str, Any]]:
    spec = doc["spec"]
    if doc["kind"] == "CronJob":
        spec = spec["jobTemplate"]["spec"]
    return list(spec["template"]["spec"].get("volumes", []))


# ── 范围守卫：新加一个"带对接点"的清单，必须被这条守卫管到 ─────────────────────────
_对接键 = (
    "selector",  # Deployment / Service / PDB / ServiceMonitor
    "podSelector",  # NetworkPolicy
    "scaleTargetRef",  # HPA
    "backend",  # Ingress
    "targetPort",  # Service
    "envFrom",  # Deployment / Job / CronJob
    "persistentVolumeClaim",  # CronJob
)


def _带对接点的清单() -> set[str]:
    """递归找"指向别处的字符串"，返回 `文件名:Kind` 集合。"""
    found: set[str] = set()
    for directory in (_K8S, _OBS):
        for fname, doc in _docs(directory):
            if _有对接键(doc):
                found.add(f"{directory.name}/{fname}:{doc.get('kind')}")
    return found


def _有对接键(node: Any) -> bool:
    if isinstance(node, dict):
        return any(k in _对接键 or _有对接键(v) for k, v in node.items())
    if isinstance(node, list):
        return any(_有对接键(v) for v in node)
    return False


# 冻结清单：这些文件里有"指向别处的字符串"，每一条都必须在上面的断言里有归属。
# 新增/改名清单时，这里会红——红的意思是"你得决定这条新对接由谁来守"，
# 而不是"把名字补上去让它变绿"。
_期望清单 = {
    "k8s/deployment.yaml:Deployment",
    "k8s/migrate-job.yaml:Job",
    "k8s/backup-cronjob.yaml:CronJob",
    "k8s/service.yaml:Service",
    "k8s/pdb.yaml:PodDisruptionBudget",
    "k8s/networkpolicy.yaml:NetworkPolicy",
    "k8s/hpa.yaml:HorizontalPodAutoscaler",
    "k8s/ingress.yaml:Ingress",
    "observability/servicemonitor.yaml:ServiceMonitor",
}


def test_范围守卫_每个带对接点的清单都要有人守() -> None:
    got = _带对接点的清单()
    assert got, "一个带对接点的清单都没扫到——守卫坏了（不是清单变干净了）"
    assert got == _期望清单, (
        "带对接点的清单变了。\n"
        f"只在清单里、不在期望里：{sorted(got - _期望清单)}\n"
        f"只在期望里、不在清单里（删了/改名了？）：{sorted(_期望清单 - got)}\n"
        "处理方式：为新清单补一条断言（说明它指向谁、错了会怎样），再更新 _期望清单。"
    )


@pytest.mark.parametrize("kind", ["Deployment", "PodDisruptionBudget", "Service"])
def test_关键对象的名字必须与Deployment一致(kind: str) -> None:
    """Service / PDB 与被它管的工作负载**同名**是本仓库的约定。

    分开写一条是因为：名字漂移（比如只改一处）不会报错，但会让 `kubectl -n warden get svc
    <旧名字>` 变成"没找到"，而人第一反应是"集群丢了"，不是"名字改了"。
    """
    _体检()
    names = _KIND_NAMES.get(kind, set())
    assert _DEPLOY_NAME in names, (
        f"{kind} 里没有叫 {_DEPLOY_NAME!r} 的对象（现有：{sorted(names)}）"
    )
