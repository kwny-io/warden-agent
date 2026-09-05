"""轻量可观测性：进程内指标注册表，输出 Prometheus text 格式。

T8 的目标：让一个 Agent 服务"可以被观察"——出问题了能回答"发生了什么、有多少、多慢"。
这里不引入 prometheus_client 重型依赖，而是用一个极简、线程安全的注册表自己实现
Prometheus text exposition 格式（`/metrics` 能被 Prometheus / Grafana 直接抓）。

提供三类指标：
  - Counter  计数器：只会增加，适合"一共发生了多少次"（请求数、工具调用数、审批数、DENY 数）
  - Gauge    瞬时值：可上可下，适合"当前有多少"（正在执行的 run 数、内存）
  - Histogram 分布桶：适合"请求耗时分布"（p50/p95 让下游按桶累计算，这里只管累加）

设计要点：
  - 无第三方依赖，纯标准库 + 一把全局锁，多线程安全（FastAPI 跑在线程池）。
  - 标签(labels)：用 (name, tuple(labels)) 作键，支持按 run / 工具 / 状态分类统计。
  - 输出遵循 Prometheus text format，方便被直接拉取。

实现约定（避免踩坑）：
  - Counter / Gauge 用「全键 = 指标 key + 标签扁平元组」定位一行；读写都在同一把锁内做，
    保证 inc/dec 的"读-改-写"原子，否则会在无标签键上误写（见 T8 排查日志）。
  - Histogram 注册返回一个调查柄，`observe(value)` 走桶边界 + _count + _sum 三份累加；
    只靠声明返回 None 会让调用方 `hist.observe(...)` 直接 AttributeError。
"""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Iterable

# +Inf 桶上界（Prometheus 直方图约定：比任何有限桶都大的"兜底桶"）
_INF = "+Inf"

# 指标键：由「指标类型 + 名字 + 标签名 + 标签值」扁平拼接而成
Key = tuple[str, ...]


class Counter:
    """单调递增计数器。inc() 每次 +1，可带 labels 分档统计。"""

    __slots__ = ("_registry", "_key")

    def __init__(self, registry: MetricsRegistry, key: tuple[str, ...]) -> None:
        self._registry = registry
        self._key = key

    def inc(self, amount: int = 1, labels: tuple[str, ...] = ()) -> None:
        self._registry._add_counter(self._key, labels, amount)


class Gauge:
    """瞬时值（可增可减）。set()/inc()/dec() 都作用于同一个带标签的键。"""

    __slots__ = ("_registry", "_key")

    def __init__(self, registry: MetricsRegistry, key: tuple[str, ...]) -> None:
        self._registry = registry
        self._key = key

    def set(self, value: float, labels: tuple[str, ...] = ()) -> None:
        self._registry._set_gauge(self._key, labels, value)

    def inc(self, amount: float = 1, labels: tuple[str, ...] = ()) -> None:
        self._registry._add_gauge(self._key, labels, amount)

    def dec(self, amount: float = 1, labels: tuple[str, ...] = ()) -> None:
        self._registry._add_gauge(self._key, labels, -amount)


class Histogram:
    """直方图调查柄：observe(value) 把一次观测计入桶分布。"""

    __slots__ = ("_registry", "_name", "_buckets")

    def __init__(self, registry: MetricsRegistry, name: str,
                 buckets: tuple[float, ...]) -> None:
        self._registry = registry
        self._name = name
        self._buckets = buckets

    def observe(self, value: float, labels: tuple[str, ...] = ()) -> None:
        self._registry._observe(self._name, self._buckets, value, labels)


class MetricsRegistry:
    """线程安全的内存指标注册表。示例：

        m = MetricsRegistry()
        http = m.counter("http_requests_total", "HTTP 请求总数", ["method", "path"])
        http.inc(labels=("POST", "/chat/x"))
        lat = m.histogram("http_request_duration_seconds", "耗时", [0.01, 0.05])
        lat.observe(0.03)
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[Key, float] = defaultdict(float)
        self._gauges: dict[Key, float] = defaultdict(float)
        # 每把直方图：__buckets__<name> -> {(le, labels): count}
        self._hist: dict[str, dict[tuple[object, Key], int]] = defaultdict(
            lambda: defaultdict(int)
        )
        # 直方图 sum 累加：__sum__<name> -> {labels: total}
        self._hist_sum: dict[str, dict[Key, float]] = defaultdict(
            lambda: defaultdict(float)
        )
        # 元信息：name -> (help, type)
        self._meta: dict[str, tuple[str, str]] = {}

    # ---- 定义 ----
    def counter(self, name: str, help_text: str, labels: Iterable[str] = ()) -> Counter:
        self._meta[name] = (help_text, "counter")
        return Counter(self, ("counter", name) + tuple(labels))

    def gauge(self, name: str, help_text: str, labels: Iterable[str] = ()) -> Gauge:
        self._meta[name] = (help_text, "gauge")
        return Gauge(self, ("gauge", name) + tuple(labels))

    def histogram(self, name: str, help_text: str,
                  buckets: Iterable[float] = (0.01, 0.05, 0.1, 0.5, 1.0)) -> Histogram:
        """注册并返回一个直方图调查柄。默认给一组常用的耗时桶。"""
        b = tuple(buckets)
        self._meta[name] = (help_text, "histogram")
        # 预建两本分桶账，保证 render 前 key 已存在
        self._hist["__buckets___" + name] = defaultdict(int)
        self._hist_sum["__sum___" + name] = defaultdict(float)
        return Histogram(self, name, b)

    # ---- 内部写入（都在锁内做"读-改-写"，避免键分裂）----
    def _add_counter(self, key: tuple[str, ...], labels: tuple[str, ...],
                     amount: int) -> None:
        k = key + labels
        with self._lock:
            self._counters[k] += amount

    def _set_gauge(self, key: tuple[str, ...], labels: tuple[str, ...],
                   value: float) -> None:
        k = key + labels
        with self._lock:
            self._gauges[k] = value

    def _add_gauge(self, key: tuple[str, ...], labels: tuple[str, ...],
                   amount: float) -> None:
        """Gauge 原子的读-改-写：inc/dec 都在同一把锁里基于原键累加。"""
        k = key + labels
        with self._lock:
            self._gauges[k] += amount

    def _observe(self, name: str, buckets: tuple[float, ...], value: float,
                 labels: tuple[str, ...] = ()) -> None:
        """把一次观测计入直方图各桶 + 累计 _count / _sum。"""
        with self._lock:
            bucket_map = self._hist["__buckets___" + name]
            for le in buckets:
                if value <= le:
                    bucket_map[(le, labels)] += 1
            # +Inf 是兜底桶：累加全部观测数（即使 value 超过所有有限桶也算这一笔）
            bucket_map[(_INF, labels)] += 1
            self._hist_sum["__sum___" + name][labels] += value

    # ---- 输出 ----
    def render(self) -> str:
        """渲染成 Prometheus text 格式。"""
        lines: list[str] = []
        with self._lock:
            for name, (help_text, typ) in self._meta.items():
                lines.append(f"# HELP {name} {help_text}")
                lines.append(f"# TYPE {name} {typ}")
                if typ == "counter":
                    for k, v in self._counters.items():
                        if k[0] == "counter" and k[1] == name:
                            lines.append(_fmt_metric(name, k[2:], v))
                elif typ == "gauge":
                    for k, v in self._gauges.items():
                        if k[0] == "gauge" and k[1] == name:
                            lines.append(_fmt_metric(name, k[2:], v))
                elif typ == "histogram":
                    lines.extend(_fmt_histogram(
                        name,
                        self._hist["__buckets___" + name],
                        self._hist_sum["__sum___" + name],
                    ))
        return "\n".join(lines) + "\n"


def _fmt_metric(name: str, labels: Key, value: float) -> str:
    if labels:
        joined = ",".join(f'{labels[i]}="{labels[i + 1]}"'
                          for i in range(0, len(labels), 2))
        return f"{name}{{{joined}}} {_num(value)}"
    return f"{name} {_num(value)}"


def _fmt_histogram(name: str, bucket_map: dict[tuple[object, Key], int],
                   sum_map: dict[Key, float]) -> list[str]:
    """直方图输出：每个 (le, labels) 组合一行 _bucket + _count + _sum。

    按标签集分组输出；每个标签集的桶按 le 升序做单调累计（+Inf 排最后兜底），
    _sum 用 sum_map 里真实累加的观测总和。
    """
    out: list[str] = []
    label_sets: set[Key] = {labels for (_le, labels) in bucket_map}
    if not label_sets:
        label_sets.add(())
    for labels in sorted(label_sets, key=str):
        present = [(le, bucket_map[(le, labels)])
                   for le in {le for (le, _lab) in bucket_map}
                   if (le, labels) in bucket_map]
        total = 0
        for le, cnt in sorted(present, key=lambda x: _le_sort_key(x[0])):
            total += cnt
            out.append(_fmt_metric(name + "_bucket", _merge_le(labels, le), total))
        # 循环结束时 total = 全部观测数（含 +Inf 兜底）
        out.append(_fmt_metric(name + "_count", labels, total))
        out.append(_fmt_metric(name + "_sum", labels, sum_map.get(labels, 0.0)))
    return out


def _le_sort_key(le: object) -> tuple[int, str]:
    """把 '+Inf' 排最后，其余按数值排。"""
    if le == _INF:
        return (1, "")
    return (0, str(le))


def _merge_le(labels: Key, le: object) -> Key:
    if labels:
        return labels + ("le", str(le))
    return ("le", str(le))


def _num(v: float) -> str:
    return f"{v:.0f}" if float(v).is_integer() else f"{v:g}"


# ---- 全局默认实例（供各模块共享）----
_registry = MetricsRegistry()


def metrics() -> MetricsRegistry:
    """取全局指标注册表。"""
    return _registry
