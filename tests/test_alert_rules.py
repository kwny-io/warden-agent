"""告警规则库 / SLO 的静态守卫。

规则库最容易的坏法不是写错语法（promtool 能查），而是**悄悄指向一个不存在的指标**：
改代码时把某个指标改名，规则还在，Prometheus 只是永远不触发——"配了告警"变成一句空话，
没有任何东西会报错。这条测试把那种漂移变成红灯。

做法：解析 YAML，把表达式里出现的每个 `warden_*` 指标拎出来，逐个核对——
  - 要么是代码里真实定义的指标（扫 src 里的指标名字面量）；
  - 要么是同目录 SLO 文件里声明的记录规则（`warden:slo_...`）。
另外断言 SLO 的记录规则确实被后续告警引用（定义了却没人用 = 白定义）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
ALERT_DIR = REPO / "deploy" / "observability" / "alerts"
DASHBOARD = (
    REPO / "deploy" / "observability" / "grafana" / "dashboards" / "warden-overview.json"
)
SRC = REPO / "src" / "warden_agent"

# 指标名 token（含记录规则里的冒号形式）
_TOKEN_RE = re.compile(r"warden[a-zA-Z0-9_:]+")
# 应用源码里的指标名字面量
_METRIC_LITERAL_RE = re.compile(r"""["'](warden_[a-z0-9_]+)["']""")


def _load(name: str) -> dict:
    with (ALERT_DIR / name).open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _app_metrics() -> set[str]:
    """扫源码里出现过的 `warden_*` 指标名（这些是"真实存在的指标"）。"""
    found: set[str] = set()
    for path in SRC.rglob("*.py"):
        found.update(_METRIC_LITERAL_RE.findall(path.read_text(encoding="utf-8")))
    return found


def _rules(doc: dict) -> list[dict]:
    out: list[dict] = []
    for group in doc.get("groups", []):
        out.extend(group.get("rules", []))
    return out


def _expressions(doc: dict) -> list[str]:
    return [str(r["expr"]) for r in _rules(doc) if "expr" in r]


def _normalize(token: str) -> str:
    """把直方图的派生后缀还原成基础指标名。"""
    for suffix in ("_bucket", "_count", "_sum"):
        if token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def test_表达式引用的指标都真实存在() -> None:
    app = _app_metrics()
    assert "warden_http_requests_total" in app  # 自检：扫描确实扫到了东西

    slo = _load("warden.slo.yml")
    recording = {r["record"] for r in _rules(slo) if "record" in r}
    assert recording, "SLO 文件里应当有记录规则"

    referenced: set[str] = set()
    for doc_name in ("warden.rules.yml", "warden.slo.yml"):
        for expr in _expressions(_load(doc_name)):
            for token in _TOKEN_RE.findall(expr):
                referenced.add(token)

    unknown: list[str] = []
    for token in referenced:
        if token in recording:
            continue
        if _normalize(token) in app:
            continue
        unknown.append(token)
    assert not unknown, f"规则里引用了不存在的指标：{sorted(unknown)}"


def test_slo记录规则都被告警或其它规则用到() -> None:
    slo = _load("warden.slo.yml")
    recording = {r["record"] for r in _rules(slo) if "record" in r}
    referenced: set[str] = set()
    for expr in _expressions(slo):
        referenced.update(_TOKEN_RE.findall(expr))
    # 记录规则允许引用别的记录规则；但每一条最终都应被某条 alert 的表达式或其它记录规则用到
    unused = recording - referenced
    assert not unused, f"这些记录规则定义了却没人引用：{sorted(unused)}"


def test_grafana仪表盘引用的指标都真实存在() -> None:
    """仪表盘也会漂移：指标改名后，面板会**静静地变成空图**（不报错、没人发现）。

    这里对每个面板的 PromQL 做与告警规则同样的核对——引用的 `warden_*` 指标必须
    要么在代码里真实定义、要么是同目录 SLO 文件里声明的记录规则。
    """
    dashboard = DASHBOARD
    assert dashboard.exists(), f"仪表盘文件不见了：{dashboard}"
    with dashboard.open(encoding="utf-8") as fh:
        doc = json.load(fh)

    exprs: list[str] = []
    for panel in doc.get("panels", []):
        for target in panel.get("targets", []):
            expr = target.get("expr")
            if expr:
                exprs.append(str(expr))
    assert exprs, "仪表盘里应当有 PromQL 查询"

    app = _app_metrics()
    slo = _load("warden.slo.yml")
    recording = {r["record"] for r in _rules(slo) if "record" in r}
    referenced: set[str] = set()
    for expr in exprs:
        referenced.update(_TOKEN_RE.findall(expr))

    unknown = [
        token for token in referenced
        if token not in recording and _normalize(token) not in app
    ]
    assert not unknown, f"仪表盘引用了不存在的指标：{sorted(unknown)}"


def test_每条告警都有严重级别与说明() -> None:
    for doc_name in ("warden.rules.yml", "warden.slo.yml"):
        for rule in _rules(_load(doc_name)):
            if "alert" not in rule:
                continue
            labels = rule.get("labels", {})
            assert labels.get("severity") in {"critical", "warning", "info"}, rule["alert"]
            assert rule.get("annotations", {}).get("summary"), rule["alert"]
            assert rule.get("for"), f"{rule['alert']} 没有 for（瞬时抖动也会响）"


def test_告警名不重复() -> None:
    names: list[str] = []
    for doc_name in ("warden.rules.yml", "warden.slo.yml"):
        names.extend(r["alert"] for r in _rules(_load(doc_name)) if "alert" in r)
    assert len(names) == len(set(names)), f"告警名重复：{names}"
