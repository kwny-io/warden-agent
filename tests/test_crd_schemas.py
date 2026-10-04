"""Prometheus Operator 的两个 CR 清单必须过**官方 schema**。

为什么要有这个文件（而不是只靠 CI 里的 kubeconform）：
  - `ci.yml` 的 kubeconform 步骤只挂了 `deploy/k8s` 一个目录（看那一步的 `-v` 参数），
    所以 `deploy/observability/*.yaml`（`PrometheusRule` / `ServiceMonitor`）
    **从来没有进过任何 schema 校验**；
  - 本地守卫查的是"我们的要求"——标签存在、`bearerTokenSecret` 指向的 Secret 键真实存在、
    规则正文与 `alerts/*.yml` 逐条一致（`test_k8s_manifests.py`、
    `test_observability_config.py`）——**它们不查字段合法性**；
  - 第六十一批实测：把 `servicemonitor.yaml` 的 `namespaceSelector.matchNames` 改成
    `matchName`（少一个 s），读 `deploy/` 的 4 个文件 **72 个用例全绿**。
    也就是说这类错在今天**每一道门禁上都看不见**。
  - 而代价是**不对称**的：K8s 的 CRD 是结构化 schema，未知字段会被**静默剪掉**
    ——`kubectl apply` 成功推不出"配置生效"，字段丢了以后行为退回默认值。
    这正是本仓库一直在清的那类"机制在、但没接上"。

为什么用 vendored schema + `jsonschema`，而不是本地也跑 kubeconform：
  - 本机的 Docker 守护进程配了 `127.0.0.1:7890` 代理而那个端口是死的（第六十一批实测：
    `docker pull ghcr.io/yannh/kubeconform:v0.6.7` → `connectex: No connection could be
    made because the target machine actively refused it`）⇒ kubeconform 在本地跑不起来。
  - vendored schema + 纯 Python 校验**不联网、不需要集群、不需要 Docker**，
    于是 CI 与本地是同一条判据（这也是本仓库对"守卫能不能真的跑"的一贯要求：
    跑不起来的守卫等于没有守卫）。

schema 的唯一事实源是上游，不是我们的注释；**按 commit 固定**：

    deploy/observability/schemas/prometheusrule_v1.json
      ← https://raw.githubusercontent.com/datreeio/CRDs-catalog/43e4407642d4c37683c88711f37caa6c9c20ca40/monitoring.coreos.com/prometheusrule_v1.json
    deploy/observability/schemas/servicemonitor_v1.json
      ← https://raw.githubusercontent.com/datreeio/CRDs-catalog/b8e5c58dd9a34bc48c4d28718d7321bd8df8c22a/monitoring.coreos.com/servicemonitor_v1.json

更新步骤（升级 Operator / 换 CRD 版本时）：
  1. `curl -sS "https://api.github.com/repos/datreeio/CRDs-catalog/commits?path=monitoring.coreos.com/<kind>_v1.json&per_page=1"`
     取最新 commit sha；
  2. 按 sha 下载到 `deploy/observability/schemas/<kind>_v1.json`（用 sha 而不是 `main`，
     这样复核的人拿到的是同一个字节序列）；
  3. 更新下面 `_SCHEMAS` 里的 `sha256` 与 `source`——**改不动 sha 就跑不过**，
     这是为了不让"悄悄换掉 schema"变成一次无声的放松。
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

_REPO = Path(__file__).resolve().parents[1]
_OBS = _REPO / "deploy" / "observability"


@dataclass(frozen=True)
class _CrSchema:
    """一个 CR 清单与它对应的官方 schema（按字节固定）。"""

    manifest: str
    schema: str
    sha256: str
    source: str


_SCHEMAS = (
    _CrSchema(
        manifest="prometheusrule.yaml",
        schema="prometheusrule_v1.json",
        sha256="9cd71d75b714b541a0be960b46bd961dbf1f0106071381cd88f54dfe80b8bd9d",
        source=(
            "datreeio/CRDs-catalog@43e4407642d4c37683c88711f37caa6c9c20ca40"
            "/monitoring.coreos.com/prometheusrule_v1.json"
        ),
    ),
    _CrSchema(
        manifest="servicemonitor.yaml",
        schema="servicemonitor_v1.json",
        sha256="8978f86e2a7cb281a9ca7bc30d857e0553666658dfe062078c51e99d3f20cd14",
        source=(
            "datreeio/CRDs-catalog@b8e5c58dd9a34bc48c4d28718d7321bd8df8c22a"
            "/monitoring.coreos.com/servicemonitor_v1.json"
        ),
    ),
)


def _load_manifest(name: str) -> dict[str, Any]:
    doc = yaml.safe_load((_OBS / name).read_text(encoding="utf-8"))
    assert isinstance(doc, dict), f"{name} 不是一个 YAML 映射"
    return doc


def _load_schema(cr: _CrSchema) -> dict[str, Any]:
    path = _OBS / "schemas" / cr.schema
    raw = path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    assert actual == cr.sha256, (
        f"{cr.schema} 的字节与评审时固定的版本不一致（有人换了 schema 却没更新这个测试）。\n"
        f"  期望 sha256: {cr.sha256}\n  实际 sha256: {actual}\n"
        f"  来源: {cr.source}\n"
        "如果这次换是**有意的**（比如升级了 Operator）：先自己确认上游那份 schema 说的确实是"
        "你想说的，再把 `_SCHEMAS` 里的 sha256 与来源一起改掉——这一步不能省，"
        "否则下次谁都不知道校验的到底是哪一份 schema。"
    )
    return json.loads(raw.decode("utf-8"))


@pytest.mark.parametrize("cr", _SCHEMAS, ids=lambda cr: cr.manifest)
def test_CR清单必须过官方schema(cr: _CrSchema) -> None:
    """字段合法性：未知字段、类型错、缺必填，都要在这里红。

    失败时的报错要能直接回答两个问题：**哪个字段**、**为什么这比看起来严重**。
    """
    schema = _load_schema(cr)
    manifest = _load_manifest(cr.manifest)
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(manifest), key=lambda e: list(e.path))
    if not errors:
        return
    lines = [f"{cr.manifest} 不符合官方 schema（{cr.source}）——共 {len(errors)} 处："]
    for err in errors[:10]:
        where = "/".join(str(p) for p in err.path) or "(根)"
        lines.append(f"  - {where}: {err.message}")
    lines.append(
        "为什么这不只是「字段写错」：CRD 是结构化 schema，未知字段**会被静默剪掉**，"
        "`kubectl apply` 照样成功、也没有任何告警——配置退回了默认值，"
        "表现就是「监控看着是绿的，出事没人知道」。"
    )
    pytest.fail("\n".join(lines))


def test_每个CR清单都有对应的schema() -> None:
    """**范围的守卫**：新加一个 CR 清单时，不能悄悄地没人校验它。

本批的起因就是"门禁挂了哪个目录，就是它的全部职责范围"：CI 的 kubeconform
只挂了 `deploy/k8s`，于是 `deploy/observability` 从来没被看过。
一个"只校验我列出来的几个文件"的测试，会在**新增文件**的那一刻重新变成
"没校验"——所以这里反过来查：**凡是盘上存在的 CR 清单，都必须在这个名单里**。
    """
    covered = {cr.manifest for cr in _SCHEMAS}
    seen: dict[str, str] = {}
    for directory in (_OBS, _REPO / "deploy" / "k8s"):
        for path in sorted(directory.glob("*.yaml")):
            for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
                if isinstance(doc, dict) and str(doc.get("apiVersion", "")).startswith(
                    "monitoring.coreos.com/"
                ):
                    seen[path.name] = str(doc.get("kind"))
    assert seen, "没扫到任何 monitoring.coreos.com 的 CR 清单 —— 这个扫描本身可能坏了"
    missing = {name: kind for name, kind in seen.items() if name not in covered}
    assert not missing, (
        "有 CR 清单没有进 schema 校验名单（新的没人管，就是这个仓库反复出现的那种漏）：\n"
        + "\n".join(f"  - {name}（{kind}）" for name, kind in sorted(missing.items()))
        + "\n请把上游 schema 按 commit 固定到 deploy/observability/schemas/，"
        "再加进 tests/test_crd_schemas.py 的 _SCHEMAS（含 sha256 与来源）。"
    )


def test_schema校验器不是安慰剂() -> None:
    """**守卫的守卫**：证明上面那条测试真的在查东西。

    三种"看起来在查、其实什么都没查"的假象，都要在这里排掉：
      1. schema 里没写 `additionalProperties: false` ⇒ 未知字段随便进，
         上面那条测试永远是绿的（而 `matchName` 这种错正是这么漏的）；
      2. schema 的 `spec.properties` 是空的 ⇒ 等于没校验；
      3. 空的/写错的实例也能过 ⇒ 校验器根本没在工作。

    所以这里做一次**反自我**：拿真清单复制一份、故意把已知字段改名，
    校验器**必须**报错；如果它不报错，说明这个文件给的是假结论。
    """
    for cr in _SCHEMAS:
        schema = _load_schema(cr)
        spec_props = schema["properties"]["spec"].get("properties") or {}
        assert spec_props, (
            f"{cr.schema} 的 spec.properties 是空的 —— 这样等于没有校验"
        )
        assert schema["properties"]["spec"].get("additionalProperties") is False, (
            f"{cr.schema} 没有把 spec 的 additionalProperties 设为 false："
            "未知字段会被放行，而这些 schema 存在的意义正是拦住未知字段"
            "（K8s 会静默剪掉它们，apply 成功 ≠ 生效）"
        )

        manifest = _load_manifest(cr.manifest)
        validator = jsonschema.Draft202012Validator(schema)

        # 反自我①：在 spec 下塞一个不存在的字段
        tampered = copy.deepcopy(manifest)
        tampered["spec"]["这个字段根本不存在"] = 1
        assert list(validator.iter_errors(tampered)), (
            f"{cr.manifest}：往 spec 里塞了不存在的字段却没被抓住 —— "
            "上面那条 schema 测试给的是假结论"
        )

        # 反自我②：把 spec 里一个**真实存在**的键改名（就是第六十一批那个真实缺陷的形状）
        renamed = copy.deepcopy(manifest)
        declared_and_used = [k for k in manifest["spec"] if k in spec_props]
        assert declared_and_used, (
            f"{cr.manifest} 的 spec 里没有任何一个键是 schema 声明过的 —— "
            "说明这几份 schema 描述的已经不是这份清单（换错版本了）"
        )
        key = "endpoints" if "endpoints" in declared_and_used else declared_and_used[0]
        renamed["spec"][key + "_typo"] = renamed["spec"].pop(key)
        assert list(validator.iter_errors(renamed)), (
            f"{cr.manifest}：把 spec.{key} 改名成 {key}_typo 竟然还能过 schema —— "
            "这些 schema 会静默接受未知字段，等于拦不住本批要拦的那类错"
        )
