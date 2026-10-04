"""告警规则库的**求值**守卫（第五十九批）。

`tests/test_alert_rules.py` 是静态守卫：名字唯一、有 severity/说明、表达式引用的指标真实存在。
静态检查查的是"名字对不对"，查不出"表达式到底会不会响"。第五十九批实测到的那条缺陷，
正是这样活下来的：

    `WardenHighLatencyP95` 的注释写着「p95 延迟 > 2s」，而延迟直方图的最高有限桶是 1.0。
    分位数落进 +Inf 桶时 `histogram_quantile` 返回**最高有限桶的上界**（1.0，不是 +Inf），
    于是 `1.0 > 2` 恒为假 —— **这条告警永远不会响**（实测：哪怕 10% 的请求超过 1s、20% 超过 10s）。

本文件做三件事：
  1. 静态：每条告警都要有「该响」和「不该响」两个用例。
     只写"该响"挡不住"永远响"；只写"不该响"挡不住"永远不响"（上面那条就是后者）。
     带 `for:` 的还要有一个"更早的时刻不响"的用例，否则 `for` 写错也没人发现。
  2. 静态：直方图分位数的**阈值必须小于最高有限桶**（把上面那类缺陷钉死，无需 docker）。
  3. 真值：把 `*.rules.test.yml` / `*.slo.test.yml` 交给 promtool 真求值一遍。

第 3 步怎么手工跑（本地无需网络，用已有的镜像）：

    docker run --rm --entrypoint promtool -v "$PWD/deploy/observability:/cfg" \\
      prom/prometheus:v2.55.0 test rules /cfg/alerts/warden.rules.test.yml

**已知限制**：第 3 步在本机/CI 没有 `prom/prometheus:v2.55.0` 镜像时会跳过（测试不联网拉镜像）。
第 1、2 步那种静态守卫在任何环境下都会跑，覆盖的是同一类缺陷；但"表达式语义真的对"这件事，
只有第 3 步能证。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
ALERT_DIR = REPO / "deploy" / "observability" / "alerts"
SERVER_PY = REPO / "src" / "warden_agent" / "web" / "server.py"
PROM_IMAGE = "prom/prometheus:v2.55.0"

RULE_FILES = ("warden.rules.yml", "warden.slo.yml")
TEST_FILES = ("warden.rules.test.yml", "warden.slo.test.yml")


def _load(name: str) -> dict:
    with (ALERT_DIR / name).open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _rules(doc: dict) -> list[dict]:
    out: list[dict] = []
    for group in doc.get("groups", []):
        out.extend(group.get("rules", []))
    return out


def _secs(text: str) -> int:
    m = re.fullmatch(r"\s*(\d+)([smhd])\s*", str(text))
    assert m, f"读不懂的时长：{text!r}"
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def _alerts() -> dict[str, dict]:
    """规则文件里每条告警的原始定义（去重：SLO 与主规则文件都可能有告警）。"""
    out: dict[str, dict] = {}
    for name in RULE_FILES:
        for rule in _rules(_load(name)):
            if "alert" in rule:
                out[str(rule["alert"])] = rule
    return out


def _cases() -> list[tuple[str, str, int, list]]:
    """测试文件里的每条断言： (测试名, 告警名, 求值时刻秒, 期望的告警列表)。"""
    out: list[tuple[str, str, int, list]] = []
    for name in TEST_FILES:
        for test in _load(name).get("tests", []):
            for case in test.get("alert_rule_test", []):
                out.append((
                    str(test.get("name", "?")),
                    str(case.get("alertname")),
                    _secs(case.get("eval_time", "0m")),
                    list(case.get("exp_alerts") or []),
                ))
    return out


def test_每条告警都有该响与不该响两个用例() -> None:
    alerts = _alerts()
    assert alerts, "规则文件里没读到任何告警——守卫本身失效了"

    firing: dict[str, list[int]] = {}
    silent: dict[str, list[int]] = {}
    for test_name, alertname, at, exp in _cases():
        (firing if exp else silent).setdefault(alertname, []).append(at)

    problems = []
    for name in sorted(alerts):
        if not firing.get(name):
            problems.append(f"{name}：没有任何「该响」的用例（等于没人验证过它会响）")
        if not silent.get(name):
            problems.append(f"{name}：没有任何「不该响」的用例（等于没人验证过它不会乱响）")
    assert not problems, "告警求值用例不全：\n  " + "\n  ".join(problems)


def test_测试文件里的告警名都在规则文件里() -> None:
    known = set(_alerts())
    unknown = sorted({alertname for _, alertname, _, _ in _cases()} - known)
    assert not unknown, f"测试文件里引用了规则文件里不存在的告警名：{unknown}"


def test_带for的告警要有还没到点的用例() -> None:
    """`for:` 的意思是「连续这么久都成立才响」。没有"更早的时刻不响"的用例，
    把 for 写成 1s 或删掉 for 都不会红——而这正是「配了 for 却没生效」的坏法。"""
    firing: dict[str, list[int]] = {}
    silent: dict[str, list[int]] = {}
    for _, alertname, at, exp in _cases():
        (firing if exp else silent).setdefault(alertname, []).append(at)

    problems = []
    for name, rule in sorted(_alerts().items()):
        if not rule.get("for"):
            continue
        f, s = firing.get(name, []), silent.get(name, [])
        if not any(s_at < f_at for s_at in s for f_at in f):
            problems.append(
                f"{name}（for: {rule['for']}）：没有比「该响」更早的「不响」用例"
                f"（该响时刻={sorted(f)}，不响时刻={sorted(s)}）"
            )
    assert not problems, "for 没有被真正验证：\n  " + "\n  ".join(problems)


def test_记录规则都有精确值用例() -> None:
    """SLO 记录规则的算术（全好=1、全坏=0）必须被真的算过一遍。"""
    recording = {
        str(r["record"]) for name in RULE_FILES for r in _rules(_load(name)) if "record" in r
    }
    assert recording, "没有读到任何 record 规则——守卫本身失效了"

    covered: set[str] = set()
    for name in TEST_FILES:
        for test in _load(name).get("tests", []):
            for case in test.get("promql_expr_test", []):
                covered.add(str(case.get("expr")))

    missing = sorted(recording - covered)
    assert not missing, f"这些记录规则没有任何精确值用例（算术写错也没人发现）：{missing}"


def test_直方图分位数阈值小于最高有限桶() -> None:
    """分位数落进 +Inf 桶时返回的是**最高有限桶的上界**：阈值不小于最高桶 ⇒ 恒不成立。

    第五十九批就是这一条（阈值 2s，最高桶 1.0）——而它当时以「配好了告警」的样子活了很久。
    """
    m = re.search(
        r'"warden_http_request_duration_seconds",\s*"[^"]*",\s*\[([^\]]+)\]',
        SERVER_PY.read_text(encoding="utf-8"),
    )
    assert m, "没在 web/server.py 里找到 HTTP 延迟直方图的桶定义——守卫需要跟上代码结构"
    buckets = sorted(float(x) for x in m.group(1).split(","))
    top = buckets[-1]

    checked = 0
    for name, rule in sorted(_alerts().items()):
        expr = str(rule.get("expr", ""))
        if "histogram_quantile" not in expr:
            continue
        thresholds = [float(x) for x in re.findall(r"[<>]=?\s*(\d+(?:\.\d+)?)", expr)]
        assert thresholds, f"{name} 里有 histogram_quantile 但没读到比较阈值：{expr!r}"
        for value in thresholds:
            checked += 1
            assert value < top, (
                f"{name} 的阈值 {value} 不小于最高有限桶 {top}：分位数被封顶在 {top}，"
                f"`> {value}` 永远不成立（请调低阈值或把桶扩到 {value} 之上）"
            )
    assert checked >= 1, "没有检查到任何 histogram_quantile 阈值——守卫本身失效了"


def _promtool_available() -> bool:
    try:
        proc = subprocess.run(
            ["docker", "images", "-q", PROM_IMAGE],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and bool((proc.stdout or "").strip())


@pytest.mark.parametrize("test_file", TEST_FILES)
def test_promtool真值求值通过(test_file: str) -> None:
    if not _promtool_available():
        pytest.skip(f"本机没有 {PROM_IMAGE} 镜像（测试不联网拉镜像）；静态守卫仍然有效")

    mount = str(ALERT_DIR.parent).replace("\\", "/")
    proc = subprocess.run(
        [
            "docker", "run", "--rm", "--entrypoint", "promtool", "-v", f"{mount}:/cfg",
            PROM_IMAGE, "test", "rules", f"/cfg/alerts/{test_file}",
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300, check=False,
    )
    # 失败消息里必须自己也不烽：proc.stdout/stderr 在读取线程解码失败时会是 None，
    # 直接下标就会变成 TypeError，把真正的 promtool 差异盖掉（这条曾经真的发生过）。
    assert proc.returncode == 0 and "SUCCESS" in (proc.stdout or ""), (
        f"promtool 求值失败（{test_file}），returncode={proc.returncode}：\n"
        f"--- stdout 尾 ---\n{(proc.stdout or "")[-4000:]}\n"
        f"--- stderr 尾 ---\n{(proc.stderr or "")[-1000:]}"
    )
