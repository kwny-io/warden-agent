"""多 Agent —— 独立并行/串行分派器（不依赖模型脑补，可离线条度精确测）。

背景（为什么需要它）：
  基础的主管模式靠"模型自己在 loop 里一个个调子工具"来决定顺序——那是**串行**的，
  而且顺序全凭模型口胡。可有些任务该**并行**（两个调研互相独立，同时查更快），
  有些必须**串行**（先查后写，写要依赖查的产出）。靠模型碰运气既慢又不可测。

出色做法（本模块落地的）：
  一个**确定性分派器（Dispatcher）**：你（代码）声明一组子 Agent 和它们的依赖关系，
  分派器负责：
    - `run_parallel(tasks)`  —— 线程池**真并行**跑多个独立子 Agent，同时收集每份交接单；
    - `run_sequential(chain)`—— **按依赖串行**跑，前一步的交接单可注入后一步的 topic。
  它不请模型"想"，纯代码路径，所以能离线精确测试（真不真并行一测耗时就知道）。

与主管 loop 的关系：
  主管（AgentLoop）仍在"动脑分工"；分派器只是把"确定的并行/串行执行"从模型的循环里
  抽出来、用确定语义跑。两者可组合：主管判断该并行的分支，交给本分派器并行执行。
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any

from warden_agent.loop.loop import AgentLoop


@dataclass
class DispatchedTask:
    """一个待派发的子任务：给哪个子 Agent、派什么 topic。"""

    name: str      # 结果里用来标识这份产出（如 "research-a"）
    agent: AgentLoop
    topic: str


@dataclass
class DispatchResult:
    """一次并行/串行分派的结果：每份交接单 + 完整产出。"""

    outputs: dict[str, str] = field(default_factory=dict)
    # 串行时记录执行顺序（便于断言/审计）
    order: list[str] = field(default_factory=list)
    # 每个子任务失败的具体原因（键=任务名）。一个子任务失败不会中止整批。
    errors: dict[str, str] = field(default_factory=dict)
    # 并行时超时仍未完成的任务名（已取消 / 已反映为未完成产出）。
    unfinished: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return "\n".join(f"[{k}] {v}" for k, v in self.outputs.items())


class Dispatcher:
    """确定性多 Agent 分派器：并行（线程池）或串行（依赖链）执行子 Agent。"""

    def __init__(self, *, max_workers: int = 8, timeout: float = 120.0) -> None:
        self.max_workers = max_workers
        self.timeout = timeout

    # ---- 并行：多个独立子任务同时跑 ----

    def run_parallel(self, tasks: list[DispatchedTask]) -> DispatchResult:
        """线程池并发执行一组独立子任务，全部完成后汇总每份产出。

        - 用 `ThreadPoolExecutor`（并发线程真并行，不是伪并行的 for 循环）。
        - 用 `concurrent.futures.wait(..., timeout=...)` 做**真正生效**的超时；
          超时后取消未开始的 future，并把"未完成"如实记入 `unfinished`/`errors`。
        - 逐个 future 单独收集结果/异常：**一个子任务失败不中止整批**。
        - 退出时 `shutdown(wait=False, cancel_futures=True)`，不等待被超时卡住的线程，
          否则超时设置形同虚设（with 块会在退出时 wait 住）。
        """
        result = DispatchResult()
        if not tasks:
            return result

        def _run(t: DispatchedTask) -> tuple[str, str]:
            return t.name, t.agent.run(t.topic).text

        executor = ThreadPoolExecutor(max_workers=self.max_workers)
        futures = {executor.submit(_run, t): t.name for t in tasks}
        try:
            done, not_done = wait(futures, timeout=self.timeout)
            # 按提交顺序汇总，保证 order 确定性
            for future, name in futures.items():
                if future in not_done:
                    continue
                try:
                    _name, text = future.result()
                    result.outputs[name] = text
                except Exception as e:  # noqa: BLE001 - 单个子任务异常不能中止整批
                    detail = f"{type(e).__name__}: {e}"
                    result.errors[name] = detail
                    result.outputs[name] = f"[失败] {detail}"
                result.order.append(name)
            # 超时未完成的：取消 + 如实报告（不静默丢弃）
            for future, name in futures.items():
                if future not in not_done:
                    continue
                future.cancel()
                detail = f"超过超时 {self.timeout}s 仍未完成"
                result.unfinished.append(name)
                result.errors[name] = detail
                result.outputs[name] = f"[未完成] {detail}"
                result.order.append(name)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return result

    # ---- 串行：按依赖链，前一步产出喂给后一步 ----

    def run_sequential(
        self,
        chain: list[tuple[str, AgentLoop]],
        *,
        initial: str = "",
        make_topic: Callable[[str, str], str] | None = None,
    ) -> DispatchResult:
        """按顺序跑一串子 Agent，前一步的产出可注入后一步的 topic。

        - `chain`：[(步骤名, 子Agent), ...]，按列表顺序严格串行。
        - `initial`：喂给第一步的初始话题。
        - `make_topic(prev_output, step_name)`：可选，把上一步产出"拼进"下一步的话题。
          默认直接把上一步产出原样作为下一步 topic（体现"后一步依赖前一步结论"）。
        - 返回按执行顺序记录结果的 DispatchResult。
        """
        result = DispatchResult()
        prev_output = initial
        for step_name, agent in chain:
            topic = (make_topic(prev_output, step_name)
                     if make_topic is not None else prev_output)
            reply = agent.run(topic)
            result.outputs[step_name] = reply.text
            result.order.append(step_name)
            prev_output = reply.text
        return result


def make_dispatcher(**kwargs: Any) -> Dispatcher:
    """便捷构造一个分派器。"""
    return Dispatcher(**kwargs)


__all__ = ["Dispatcher", "DispatchedTask", "DispatchResult", "make_dispatcher"]
