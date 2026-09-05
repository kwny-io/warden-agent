"""健康检查（Health）：告诉负载均衡/编排平台"这个服务还活着吗、能不能接流量"。

  - liveness（存活） ：进程还活着就直接 200，绝不依赖任何下游。挂了外围也照样该报 0。
  - readiness（就绪）：真正"能不能接流量"——会去 ping 底层存储。存储一断就 503，
                      负载均衡就不再把新请求打进来。

两者都不能泄敏感信息：只回字段名，不回连接串/密钥。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class SqlitePingable(Protocol):
    """健康检查只依赖存储的"能否 ping 通"，不关心具体实现。"""

    def ping(self) -> None: ...


@dataclass(frozen=True)
class HealthResult:
    status: str  # "ok" | "degraded"
    checks: dict[str, Any]


def liveness() -> HealthResult:
    """存活探针：进程活着即 ok，绝不调用任何下游。"""
    return HealthResult(status="ok", checks={"self": "ok"})


def readiness(*pings: SqlitePingable) -> HealthResult:
    """就绪探针：依次 ping 各依赖（目前主要是存储）。任何一个失败即整体 degraded。"""
    checks: dict[str, Any] = {}
    ok = True
    for dep in pings:
        name = type(dep).__name__.lower()
        try:
            dep.ping()
            checks[name] = "ok"
        except Exception:  # noqa: BLE001 - 探针吞掉细节，只回状态
            checks[name] = "unreachable"
            ok = False
    return HealthResult(status="ok" if ok else "degraded", checks=checks)
