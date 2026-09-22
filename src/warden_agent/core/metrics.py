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


def _pair_labels(key: tuple[str, ...], values: tuple[str, ...]) -> Key:
    """把「指标键（含标签**名**）」与「本次的标签**值**」交叉拼成 name/value 交错的键。

    为什么需要它（**这是修过的一个真 bug**）：`key` 的形状是
    `("counter", 指标名, 标签名1, 标签名2, ...)`（名字在 key 里），而调用方传的是**值**。
    早先的实现直接 `k = key + values`，于是键变成
    `(..., "method", "path", "GET", "/metrics")`——**名字挤在一起、值也挤在一起**；
    而渲染时按"相邻两个一组 = name/value"来读，就输出成了
    `{method="path",GET="/metrics"}`：**标签名和值错位**。
    单标签指标恰好蒙对（`path="..."`），**两个及以上标签全错**
    （`method`/`path` 那种），于是任何 `sum by (path)` 的查询都查不到数据、
    而且**不报错**——只是永远为空。
    """
    names = key[2:]                      # key 里 `指标类型 + 指标名` 之后全是标签名
    if not values:
        return key
    names = names[: len(values)]         # 只给部分标签值时，按下标取前几个名字
    flat: list[str] = []
    for name, value in zip(names, values, strict=True):
        flat.extend((name, value))
    # **替换**掉 key 里那一段"挤在一起的标签名"，换成 name/value 交错的形态
    # （渲染时按"相邻两个一组"读，所以键里不能留原始的名字段）
    return key[:2] + tuple(flat)


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
        # 直方图**总观测数**：__count__<name> -> {labels: n}
        # 为什么单独记：桶里存的是"每个有限桶的累计数"，而 +Inf 与 _count 需要**总量**。
        # 早先把 +Inf 当成"兜底桶"在 _observe 里每观测一次就 +1，渲染时又做了一次累计求和
        # ——**同一笔观测被算了两次**（`+Inf` 和 `_count` 都会偏大，`histogram_quantile`
        # 与 SLO 的延迟占比随之失真，而且不报错）。
        self._hist_count: dict[str, dict[Key, int]] = defaultdict(
            lambda: defaultdict(int)
        )
        # 每个直方图声明的桶边界：__buckets_decl__<name> -> (le, ...)
        # 渲染时要把**所有**声明的桶都输出（含计数为 0 的），否则下游按桶算分位数会失真。
        self._hist_buckets: dict[str, tuple[float, ...]] = {}
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
        self._hist_buckets[name] = b
        # 预建两本分桶账，保证 render 前 key 已存在
        self._hist["__buckets___" + name] = defaultdict(int)
        self._hist_sum["__sum___" + name] = defaultdict(float)
        return Histogram(self, name, b)

    # ---- 内部写入（都在锁内做"读-改-写"，避免键分裂）----
    def _add_counter(self, key: tuple[str, ...], labels: tuple[str, ...],
                     amount: int) -> None:
        k = _pair_labels(key, labels)
        with self._lock:
            self._counters[k] += amount

    def _set_gauge(self, key: tuple[str, ...], labels: tuple[str, ...],
                   value: float) -> None:
        k = _pair_labels(key, labels)
        with self._lock:
            self._gauges[k] = value

    def _add_gauge(self, key: tuple[str, ...], labels: tuple[str, ...],
                   amount: float) -> None:
        """Gauge 原子的读-改-写：inc/dec 都在同一把锁里基于原键累加。"""
        k = _pair_labels(key, labels)
        with self._lock:
            self._gauges[k] += amount

    def _observe(self, name: str, buckets: tuple[float, ...], value: float,
                 labels: tuple[str, ...] = ()) -> None:
        """把一次观测计入直方图各桶 + 累计 _count / _sum。"""
        with self._lock:
            bucket_map = self._hist["__buckets___" + name]
            # 有限桶存**累计**语义：value <= le 的桶都 +1（这正是 Prometheus 的 le 语义）
            for le in buckets:
                if value <= le:
                    bucket_map[(le, labels)] += 1
            # 总观测数单独记 → 渲染时给 +Inf 与 _count（**不再**在桶里重复加一次）
            self._hist_count["__count___" + name][labels] += 1
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
                        self._hist_buckets.get(name, ()),
                        self._hist["__buckets___" + name],
                        self._hist_count["__count___" + name],
                        self._hist_sum["__sum___" + name],
                    ))
        return "\n".join(lines) + "\n"


def _fmt_metric(name: str, labels: Key, value: float) -> str:
    if labels:
        joined = ",".join(f'{labels[i]}="{labels[i + 1]}"'
                          for i in range(0, len(labels), 2))
        return f"{name}{{{joined}}} {_num(value)}"
    return f"{name} {_num(value)}"


def _fmt_histogram(name: str, buckets: tuple[float, ...],
                   bucket_map: dict[tuple[object, Key], int],
                   count_map: dict[Key, int],
                   sum_map: dict[Key, float]) -> list[str]:
    """直方图输出：`_bucket{le=...}`（累计语义）+ `_count` + `_sum`。

    两个**必须守住的点**（都是修过的真 bug）：
      1. 桶里存的已经是**累计**值（`value <= le` 的观测都算进去了），
         所以渲染时**不能再累加一次**——早先累加过一次，导致 `le` 越大的桶越虚高。
      2. `+Inf` 与 `_count` 必须用**总观测数**（`count_map`），而不是"所有桶的和"：
         早先把 +Inf 当兜底桶每笔 +1、渲染又累加，**同一笔观测被算两次**。
      3. **所有声明的桶都要输出**（计数为 0 也要）：Prometheus 按桶算分位数时，
         缺失的桶会被跳过，分位数随之上偏——那种错不报错、只是数不对。
    """
    out: list[str] = []
    label_sets: set[Key] = {labels for (_le, labels) in bucket_map} | set(count_map)
    if not label_sets:
        label_sets.add(())
    for labels in sorted(label_sets, key=str):
        for le in buckets:
            out.append(_fmt_metric(
                name + "_bucket", _merge_le(labels, le),
                float(bucket_map.get((le, labels), 0)),
            ))
        total = count_map.get(labels, 0)
        out.append(_fmt_metric(name + "_bucket", _merge_le(labels, _INF), float(total)))
        out.append(_fmt_metric(name + "_count", labels, float(total)))
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
