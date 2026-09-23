"""可观测性配置的静态守卫（SLO/告警/Alertmanager/Compose）。

为什么需要：这些 YAML 的坏法大多**不报语法错、也不报运行时错**，只是静静地
永远不触发、永远不发、永远抑制不了：
  - 规则里写 le="1" 而应用渲染的是 le="1.0" → 分子恒 0、燃烧率恒越限；
  - Alertmanager 不加 --config.expand-env → ${ALERT_WEBHOOK_URL} 当字面 URL，告警发不出去；
  - Prometheus 保留期短于 SLO 窗口 → rate[30d] 失真或为空，慢烧告警静默失效；
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


def test_prometheus保留期覆盖SLO最长窗口() -> None:
    """保留期 < SLO 窗口时 rate[30d] 只能看到部分样本 → 慢烧告警静静地失真/不触发。"""
    compose = _load(OBS / "docker-compose.yml")
    command = compose["services"]["prometheus"]["command"]
    days = [
        int(m.group(1))
        for entry in command
        if (m := re.search(r"--storage\.tsdb\.retention\.time=(\d+)d", str(entry)))
    ]
    assert days, "prometheus 命令应显式设置 --storage.tsdb.retention.time"
    assert max(days) >= 30, f"保留期 {max(days)}d 少于 SLO 最长窗口 30d"


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
