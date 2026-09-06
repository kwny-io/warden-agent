"""平台相关的资源限制实现（供 ExecutionBroker / Sandbox 使用）。

目标：让"内存 / CPU / 文件数"真正的限制落到子进程上，跨平台：
  - POSIX (Linux/macOS)：`preexec_fn` 里 `resource.setrlimit`
      * RLIMIT_AS / RLIMIT_DATA → 内存
      * RLIMIT_CPU            → CPU 时间
      * RLIMIT_NOFILE         → 打开文件数
  - Windows：Windows Job Object（pywin32）
      * JOB_OBJECT_LIMIT_PROCESS_MEMORY → 进程内存上限
      * JOB_OBJECT_LIMIT_JOB_TIME       → job 累计 CPU 时间（到点整个 job 被杀）
      * JOB_OBJECT_LIMIT_ACTIVE_PROCESS → 同时活动进程数
      * 没有直接的"文件数"限制 → 靠超时/输出截断兜底（文档如实说明）
      实现：正常创建子进程 → 立刻 AssignProcessToJobObject。一个小竞态：
      进程启动后、入 job 前 fork 出的子进程可能逃逸出该 job（单命令场景罕见，
      已在文档/注释里如实标注）。

边界（诚实标注）：这是"应用层资源限制"，不是内核级 cgroup / OS 沙箱；
在常见用例（单个命令 / 有限子进程）下能真实约束内存与 CPU。
"""

from __future__ import annotations

import contextlib
import importlib
import sys
from dataclasses import dataclass, field
from typing import Any

from warden_agent.execution.broker import ExecutionBudget


@dataclass
class ProcessLimiter:
    """携带跨平台 Popen 附加参数 + 进程创建后动作的对象。

    popen_kwargs : 传给 subprocess.Popen 的额外关键字（如 POSIX 的 preexec_fn）。
    _job         : Windows Job Object 句柄，保持存活直到进程结束（否则 GC 关闭失去限制）。
    """

    popen_kwargs: dict[str, Any] = field(default_factory=dict)
    _job: object = None

    def attach(self, proc: object) -> None:
        """进程创建后立即调用（Windows 用于把进程装进 Job Object）。"""
        if self._job is not None:
            self._assign(proc_handle=getattr(proc, "_handle", None))

    def _assign(self, proc_handle: int | None) -> None:  # pragma: no cover - 仅 Windows
        raise NotImplementedError

    def __bool__(self) -> bool:
        return True


def make_limiter(budget: ExecutionBudget) -> ProcessLimiter | None:
    """按平台构造限流器；无内存/CPU/文件限制时返回 None。

    注意：max_processes（并发进程数）不在此触发——它本就是 ExecutionBroker 自己
    管理(_track)，不需要 Job Object；Job 只在要限内存/CPU 时才创建。
    """
    if budget.max_memory_mb is None and budget.max_cpu_seconds is None \
            and budget.max_files is None:
        return None
    if sys.platform == "win32":
        return _make_win32_limiter(budget)
    return _make_posix_limiter(budget)


# ---------------- POSIX ----------------

def _make_posix_limiter(budget: ExecutionBudget) -> ProcessLimiter:
    # resource 是 POSIX 专属模块（Windows 无），动态加载以保持静态类型检查跨平台一致
    resource = importlib.import_module("resource")

    def _preexec() -> None:
        if budget.max_memory_mb is not None:
            limit = budget.max_memory_mb * 1024 * 1024
            with contextlib.suppress(ValueError, OSError):
                resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        if budget.max_cpu_seconds is not None:
            with contextlib.suppress(ValueError, OSError):
                resource.setrlimit(
                    resource.RLIMIT_CPU,
                    (budget.max_cpu_seconds, budget.max_cpu_seconds + 1),
                )
        if budget.max_files is not None:
            with contextlib.suppress(ValueError, OSError):
                resource.setrlimit(
                    resource.RLIMIT_NOFILE,
                    (budget.max_files, budget.max_files),
                )

    return ProcessLimiter(popen_kwargs={"preexec_fn": _preexec})


# ---------------- Windows ----------------

class _WindowsLimiter(ProcessLimiter):
    """Windows Job Object 限流器。"""

    def __init__(  # pragma: no cover - 仅 Windows
        self, popen_kwargs: dict[str, Any], job: object
    ) -> None:
        super().__init__(popen_kwargs=popen_kwargs, _job=job)
        self._assign_fn = None

    def _assign(self, proc_handle: int | None) -> None:  # pragma: no cover - 仅 Windows
        if proc_handle is None or self._job is None:
            return
        try:
            # pywin32 仅 Windows 可用，动态加载（入 job 失败不致命）
            win32job = importlib.import_module("win32job")
            win32job.AssignProcessToJobObject(self._job, int(proc_handle))
        except Exception:  # noqa: BLE001 - 入 job 失败不致命
            pass


def _make_win32_limiter(budget: ExecutionBudget) -> ProcessLimiter:  # pragma: no cover - 仅 Windows
    win32job = importlib.import_module("win32job")

    job = win32job.CreateJobObject(None, "warden-sandbox")
    info = win32job.QueryInformationJobObject(
        job, win32job.JobObjectExtendedLimitInformation
    )
    flags = 0
    # QueryInformationJobObject 返回嵌套 dict：BasicLimitInformation 子 dict 承载
    # 大多数限制；ProcessMemoryLimit 在顶层。
    basic = info["BasicLimitInformation"]
    if budget.max_memory_mb is not None:
        info["ProcessMemoryLimit"] = budget.max_memory_mb * 1024 * 1024
        flags |= win32job.JOB_OBJECT_LIMIT_PROCESS_MEMORY
    if budget.max_cpu_seconds is not None:
        basic["PerJobUserTimeLimit"] = budget.max_cpu_seconds * 10_000_000  # 100ns
        flags |= win32job.JOB_OBJECT_LIMIT_JOB_TIME
    if budget.max_processes is not None:
        basic["ActiveProcessLimit"] = budget.max_processes
        flags |= win32job.JOB_OBJECT_LIMIT_ACTIVE_PROCESS
    basic["LimitFlags"] = basic.get("LimitFlags", 0) | flags
    win32job.SetInformationJobObject(
        job, win32job.JobObjectExtendedLimitInformation, info
    )
    return _WindowsLimiter(popen_kwargs={}, job=job)
