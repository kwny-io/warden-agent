#!/usr/bin/env python3
"""负载测试：给一个**可重复**的压测脚本，把"能扛多少"变成测出来的数字。

为什么单独一个脚本而不是塞进测试：压测**不是**单元测试——它依赖目标机器、要跑几十秒到几分钟、
结果随硬件变化。放在 `scripts/` 里，谁都能拿去在**自己的目标环境**上跑一遍。

只用标准库（`urllib` + `concurrent.futures`），不依赖项目依赖，所以在任何机器上都能跑。

用法：
    # 先起服务（本机演示用匿名模式；生产请带 --key）
    WARDEN_ALLOW_ANON=1 python -m warden_agent.web.run_server

    python scripts/load_test.py --requests 200 --concurrency 16

    # 带鉴权
    python scripts/load_test.py --key "$WARDEN_API_KEY"

    # 把错误率超过 1% 当作失败（可接发布前门禁）
    python scripts/load_test.py --requests 500 --concurrency 32 --max-error-rate 0.01

**信任边界（为什么这里不做"内网地址拦截"）**：目标地址来自**运行者自己在命令行给的** `--base`，
和 `ab` / `hey` / `k6` 一样——它是本机运维工具，不是服务端请求路径。压测的典型目标**就是**
本机或内网的实例，所以在这里禁私网/环回等于让工具失去用途。
这里做的是本工具真正需要的约束：
  - **只允许 http/https**（挡掉 file:// 之类的误用与手滑）；
  - **不跟随重定向**（压测不该悄悄把负载打到别处去）；
  - 连接失败/超时都计入错误率并被报出来（不静默）。

⚠️ 结果怎么读：这是一条**链路级**数字（HTTP 进来到最终回答），它包含模型耗时。
   跑离线假模型时数字反映的是"服务框架 + 状态机 + 存储"的开销；
   接真模型时会被模型延迟主导——所以**报数字时必须说清用的是哪个模型**。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

_ALLOWED_SCHEMES = ("http", "https")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """重定向一律拒绝：压测的目标必须是**你指定的那个地址**，不能被 3xx 引到别处。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise urllib.error.HTTPError(req.full_url, code, f"拒绝跟随重定向到 {newurl}", headers, fp)


_OPENER = urllib.request.build_opener(_NoRedirect)


def _normalize_base(base: str) -> str:
    """校验并规范化目标地址：**只允许 http/https**，并拒绝缺主机名的写法。

    这一步挡的是手滑与误用（`localhost:8000`、`file:///etc/passwd`）。
    **不**拦截私网/环回地址——那正是压测的目标（见模块顶部的信任边界说明）。
    """
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise ValueError(
            f"目标地址只允许 http/https，实际 {parsed.scheme!r}：{base}"
            "（要测本机请写全，如 http://127.0.0.1:8000）"
        )
    if not parsed.hostname:
        raise ValueError(f"目标地址缺少主机名：{base}")
    return base.rstrip("/")


def _one_request(
    base: str, key: str, run_id: str, text: str, timeout: float
) -> tuple[float, int, str]:
    """发一次 /chat，返回 (耗时秒, 状态码, 错误信息)。"""
    body = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/chat/{run_id}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    started = time.perf_counter()
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            resp.read()
            return time.perf_counter() - started, resp.status, ""
    except urllib.error.HTTPError as e:
        return time.perf_counter() - started, e.code, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001 - 超时/连接失败都要计入错误率
        return time.perf_counter() - started, 0, f"{type(e).__name__}: {e}"


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Warden Agent 负载测试（标准库实现，可重复）")
    p.add_argument("--base", default="http://127.0.0.1:8000", help="服务地址（只允许 http/https）")
    p.add_argument("--key", default="", help="鉴权 key（对应 Authorization: Bearer）")
    p.add_argument("--requests", type=int, default=100, help="总请求数（默认 100）")
    p.add_argument("--concurrency", type=int, default=8, help="并发数（默认 8）")
    p.add_argument("--text", default="你好", help="发送的文本（默认「你好」）")
    p.add_argument("--timeout", type=float, default=30.0, help="单请求超时秒数（默认 30）")
    p.add_argument(
        "--max-error-rate", type=float, default=1.0,
        help="错误率上限（默认 1.0 = 不判定；例如 0.01 表示超过 1% 就退出码 1）",
    )
    p.add_argument("--prefix", default="load", help="run_id 前缀（默认 load，便于事后清理）")
    args = p.parse_args(argv)

    if args.requests <= 0 or args.concurrency <= 0:
        print("requests/concurrency 必须为正整数", file=sys.stderr)
        return 2
    try:
        base = _normalize_base(args.base)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    # 预检：能不能连上（连不上就别浪费一轮压测）
    try:
        with _OPENER.open(f"{base}/health/live", timeout=5):
            pass
    except Exception as e:  # noqa: BLE001
        print(f"连不上 {base}/health/live：{e}", file=sys.stderr)
        return 2

    print(f"压测 {base}：{args.requests} 请求 / 并发 {args.concurrency}"
          + ("（带鉴权）" if args.key else ""))
    latencies: list[float] = []
    errors: list[str] = []
    wall_start = time.perf_counter()

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [
            pool.submit(
                _one_request, base, args.key,
                f"{args.prefix}-{i}-{int(time.time() * 1000) % 1_000_000}", args.text, args.timeout,
            )
            for i in range(args.requests)
        ]
        for fut in futures:
            elapsed, status, err = fut.result()
            if status == 200 and not err:
                latencies.append(elapsed)
            else:
                errors.append(err or f"HTTP {status}")

    wall = time.perf_counter() - wall_start
    ok = len(latencies)
    error_rate = len(errors) / args.requests if args.requests else 0.0

    print("-" * 62)
    print(f"成功 {ok} / 失败 {len(errors)}（错误率 {error_rate:.2%}）")
    print(f"墙钟 {wall:.2f}s｜吞吐 {ok / wall:.1f} req/s")
    if latencies:
        print(f"延迟 p50 {statistics.median(latencies) * 1000:.0f}ms"
              f"｜p95 {_percentile(latencies, 0.95) * 1000:.0f}ms"
              f"｜p99 {_percentile(latencies, 0.99) * 1000:.0f}ms"
              f"｜max {max(latencies) * 1000:.0f}ms")
    if errors:
        print("失败样本（最多 5 条）：")
        for e in errors[:5]:
            print(f"  - {e}")
    print("[警告] 这是链路级数字，包含**模型耗时**——报的时候要说清用的是哪个模型"
          "（离线假模型 ≈ 框架+状态机+存储开销；真模型会被模型延迟主导）")

    if error_rate > args.max_error_rate:
        print(f"❌ 错误率 {error_rate:.2%} 超过上限 {args.max_error_rate:.2%}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
