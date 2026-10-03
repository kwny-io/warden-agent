"""可观测性配置的静态守卫（SLO/告警/Alertmanager/Compose）。

为什么需要：这些 YAML 的坏法大多**不报语法错、也不报运行时错**，只是静静地
永远不触发、永远不发、永远抑制不了：
  - 规则里写 le="1" 而应用渲染的是 le="1.0" → 分子恒 0、燃烧率恒越限；
  - Alertmanager 不加 --config.expand-env → ${ALERT_WEBHOOK_URL} 当字面 URL，告警发不出去；
  - Prometheus 保留期短于 SLO 窗口 → rate[30d] 失真或为空，慢烧告警静默失效；
  - **Operator 路径只有 ServiceMonitor、没有 PrometheusRule** → 指标抓到了、规则库也在仓库里，
    但 Operator 管理的 Prometheus 根本不读 prometheus.yml 的 rule_files ⇒ 一条 Warden 告警都
    不生效（且没有任何报错）；
  - PrometheusRule 的 `release` 标签不被 ruleSelector 选中 → 同上，且静态校验全绿；
  - inhibit 的 equal 要求聚合告警并不存在的 instance 标签 → 抑制永不命中。

本测试把每一条都变成红灯，避免"配了等于没配"。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from warden_agent.core.metrics import MetricsRegistry

REPO = Path(__file__).resolve().parents[1]
OBS = REPO / "deploy" / "observability"
ALERT_DIR = OBS / "alerts"
SERVER = REPO / "src" / "warden_agent" / "web" / "server.py"

# 规则表达式里的 le 桶选择器，例如 le="1.0"。
_LE_RE = re.compile(r'le="([^"]+)"')

# 规则表达式里的区间选择器，例如 [30d] / [5m]。
_WINDOW_RE = re.compile(r"\[(\d+)([dhms])\]")

_RULE_FILES = ("warden.rules.yml", "warden.slo.yml")


def _load(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _rules(doc: dict) -> list[dict]:
    out: list[dict] = []
    for group in doc.get("groups", []):
        out.extend(group.get("rules", []))
    return out


def _latency_buckets() -> list[float]:
    """从 server.py 的直方图声明里读出真实桶边界。

    为什么要从源码读而不是在本测试里硬编码：桶边界是**应用侧**的事。硬编码一份，
    改了 server.py 桶边界本测试仍会绿——那就拦不住漂移。这里只依赖"指标名 + 紧随其后的
    数字列表"这一形状，若 server.py 结构大变，本测试会显式失败提醒同步，而不是默默放过。
    """
    src = SERVER.read_text(encoding="utf-8")
    marker = src.find('"warden_http_request_duration_seconds"')
    assert marker != -1, "server.py 里找不到 warden_http_request_duration_seconds 的声明"
    match = re.search(r"\[([0-9][0-9.,\s]*)\]", src[marker:])
    assert match, "找不到直方图桶边界列表"
    return [float(p) for p in match.group(1).replace("\n", " ").split(",") if p.strip()]


def _rendered_le_labels() -> set[str]:
    """用应用真实的渲染路径产出 le 标签集合（含 +Inf）。

    走 MetricsRegistry.render() 而不是自己拼字符串，才能覆盖 _merge_le 用 str(float)
    渲染边界这一定义——这正是 le="1" 对不上 le="1.0" 的根因。
    """
    registry = MetricsRegistry()
    registry.histogram(
        "warden_http_request_duration_seconds", "test", _latency_buckets()
    ).observe(0.0)
    return set(_LE_RE.findall(registry.render()))


def _used_le_labels() -> set[str]:
    used: set[str] = set()
    for name in ("warden.slo.yml", "warden.rules.yml"):
        for rule in _rules(_load(ALERT_DIR / name)):
            used.update(_LE_RE.findall(str(rule.get("expr", ""))))
    return used


def test_le标签与应用渲染的直方图桶完全匹配() -> None:
    produced = _rendered_le_labels()
    assert "1.0" in produced, f"自检失败：应用未渲染出 1.0 桶，实际 {sorted(produced)}"

    used = _used_le_labels()
    assert used, "规则里应至少有一个 le= 桶选择器"

    unknown = used - produced
    assert not unknown, (
        f"规则使用了应用不会渲染的 le 标签：{sorted(unknown)}；"
        f"应用实际渲染：{sorted(produced)}（注意 1.0 不是 1）"
    )


def _all_rules() -> list[dict]:
    """alerts/ 下两个规则文件的全部规则（与 prometheusrule.yaml 应当逐条一致）。"""
    out: list[dict] = []
    for name in _RULE_FILES:
        out.extend(_rules(_load(ALERT_DIR / name)))
    return out


def _slo_groups() -> list[dict]:
    out: list[dict] = []
    for name in _RULE_FILES:
        out.extend(_load(ALERT_DIR / name)["groups"])
    return out


def _longest_window_days() -> float:
    """从规则表达式的区间选择器里**解析**最长窗口（天数）。

    为什么不写死 30：写死的话，谁把 rate[30d] 改成 rate[60d]，保留期守卫照样绿——
    守卫钉在字面量上、没钉在机制上，就退化成"文字承诺"了。
    """
    per_unit = {"d": 1.0, "h": 1 / 24, "m": 1 / 1440, "s": 1 / 86400}
    found = [
        int(value) * per_unit[unit]
        for rule in _all_rules()
        for value, unit in _WINDOW_RE.findall(str(rule.get("expr", "")))
    ]
    assert found, "规则表达式里应至少有一个区间选择器（如 [5m] / [30d]）"
    return max(found)


def _compose_retention_days() -> int:
    compose = _load(OBS / "docker-compose.yml")
    command = compose["services"]["prometheus"]["command"]
    days = [
        int(m.group(1))
        for entry in command
        if (m := re.search(r"--storage\.tsdb\.retention\.time=(\d+)d", str(entry)))
    ]
    assert days, "prometheus 命令应显式设置 --storage.tsdb.retention.time"
    return max(days)


def test_prometheus保留期覆盖SLO最长窗口() -> None:
    """保留期 < SLO 窗口时 rate[30d] 只能看到部分样本 → 慢烧告警静静地失真/不触发。"""
    longest = _longest_window_days()
    # 自检：本仓的可用性 SLO 就是"30 天预算"（见 warden.slo.yml 顶部），所以最长窗口必然 ≥30d。
    # 小于它就是解析没找到真窗口（守卫会变成空转），要显式炸出来。
    assert longest >= 30, f"解析出的最长窗口只有 {longest:g}d，与 SLO 的 30 天承诺不符"
    retention = _compose_retention_days()
    assert retention >= longest, (
        f"prometheus 保留期 {retention}d 少于规则里的最长窗口 {longest:g}d"
        " → rate 窗口只能看到部分样本，慢烧告警会静静地失真或不触发"
    )


def test_文档里的保留期与compose实值同源() -> None:
    """运维按文档核对保留期，文档里写出的每个数字都必须就是 compose 里的那个值。

    为什么不能只写「足够长」也不能各写各的：两份数字不一致（或文档根本不给数）= 运维照着核对
    会得出与事实相反的结论。

    实现上不做「文档里包含 35d」这种子串检查——它在长文档里很容易被别处的同一个数字蒙混过关
    （本测试自己的变异验证就抓出过这一点）。这里只认 `retention…<N>d` 这一种写法，
    并要求文档里**所有**这种写法的数字完全一致且等于 compose 实值。
    """
    retention = _compose_retention_days()
    text = (OBS / "README.md").read_text(encoding="utf-8")
    assert "storage.tsdb.retention.time" in text, (
        "README 应写明保留期这件事（以及 Operator 路径要自己设）"
    )
    stated = {int(m.group(1)) for m in re.finditer(r"retention[^\n]*?(\d+)\s*d", text)}
    assert stated, "README 没写出具体的保留期数字（只写「足够长」的话运维无法核对）"
    assert stated == {retention}, (
        f"README 里的保留期数字 {sorted(stated)} 与本仓实值 {retention}d 不一致"
        "（文档与代码必须同源：改一处就要改两处）"
    )


def test_Operator路径必须有PrometheusRule且与本地规则逐条一致() -> None:
    """Operator 的 Prometheus 不读 prometheus.yml 的 rule_files，规则只能来自 PrometheusRule。

    仓库此前只有 servicemonitor.yaml：抓取接上了、规则库也在仓库里，中间那一段从没接上
    ⇒ 指标有了、告警一条不生效，且没有任何报错。

    这里同时钉住"别抄一份然后分叉"：CR 的 spec.groups 必须与 alerts/*.yml 的 groups 深度相等。
    """
    cr = _load(OBS / "prometheusrule.yaml")
    assert cr["kind"] == "PrometheusRule"
    assert cr["apiVersion"].startswith("monitoring.coreos.com/")
    assert cr["metadata"]["name"] == "warden-agent"
    labels = cr["metadata"].get("labels", {})
    assert labels.get("app.kubernetes.io/name") == "warden-agent", labels
    # Operator 只按 ruleSelector 选规则：标签不匹配 = 文件在那儿、运行时一条不生效（=没接线）。
    assert labels.get("release"), (
        "PrometheusRule 必须带 release=<你的 prometheus release> 标签，否则 Operator 选不中它"
    )
    sm = _load(OBS / "servicemonitor.yaml")
    sm_release = sm["metadata"].get("labels", {}).get("release")
    assert sm_release == labels["release"], (
        f"PrometheusRule 的 release={labels['release']!r} 与 ServiceMonitor 的 "
        f"{sm_release!r} 不一致：同一个 release 才能被同一个 ruleSelector / "
        "serviceMonitorSelector 选中（否则一半生效、一半不生效）"
    )
    assert cr["spec"]["groups"] == _slo_groups(), (
        "PrometheusRule 的规则与 alerts/warden.rules.yml + alerts/warden.slo.yml 不一致："
        "规则正文的唯一事实源是那两个文件，改了一边就要同步另一边"
    )


def test_alertmanager开启环境变量展开() -> None:
    """不开 --config.expand-env 时 ${ALERT_WEBHOOK_URL} 不会展开，告警发不出去且不报错。"""
    compose = _load(OBS / "docker-compose.yml")
    command = compose["services"]["alertmanager"]["command"]
    assert any("--config.expand-env" in str(entry) for entry in command), (
        "alertmanager 未加 --config.expand-env"
    )


def test_inhibit规则不要求聚合告警缺失的instance标签() -> None:
    """错误率/延迟/SLO 燃烧告警都是 sum() 聚合的、没有 instance；equal 里要求它会永不命中。"""
    doc = _load(OBS / "alertmanager.yml")
    checked = 0
    for rule in doc.get("inhibit_rules", []):
        source = " ".join(str(x) for x in rule.get("source_matchers", []))
        if "WardenServiceDown" not in source:
            continue
        checked += 1
        assert "instance" not in rule.get("equal", []), (
            "WardenServiceDown 抑制规则要求 instance，但目标聚合告警没有该标签 → 抑制失效"
        )
    assert checked, "找不到针对 WardenServiceDown 的抑制规则"


def test_没有重复的单窗口错误预算快烧规则() -> None:
    """单窗口的 WardenErrorBudgetBurnFast 与 SLO 的双窗口 AvailabilityBudgetBurnFast 重复。"""
    names = [r["alert"] for r in _all_alerts()]
    assert names.count("WardenErrorBudgetBurnFast") == 0, "单窗口重复规则应已移除"
    assert "WardenAvailabilityBudgetBurnFast" in names, "真正的多窗口规则应保留"


def _all_alerts() -> list[dict]:
    out: list[dict] = []
    for name in ("warden.slo.yml", "warden.rules.yml"):
        out.extend(r for r in _rules(_load(ALERT_DIR / name)) if "alert" in r)
    return out
