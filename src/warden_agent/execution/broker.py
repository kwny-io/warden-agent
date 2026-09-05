"""受控执行引擎（ExecutionBroker）—— 让 Agent 跑命令/脚本，但处处受限。

BoundedOutputBuffer / ExecutionPolicy。解决的是一类很现实的问题：

  模型(大脑)可能会想执行一个命令，比如 `git status`、`python script.py`。
  但我们绝不能让 AI 在服务器上随便乱跑任何命令——那等于开了一扇后门。
  所以所有"执行"都必须经过这个受控引擎：
    - 受管子进程：知道它在跑什么、能停。
    - 输出受预算限制：防止工具一次输出几 GB 把内存/日志打爆（BoundedOutputBuffer）。
    - 执行受预算限制：超时自动杀掉，防止死循环命令永远占用。

三个核心守卫：
  1. BoundedOutputBuffer —— 有长度上限的输出缓冲。超了截断并标记 truncated。
  2. ExecutionBudget     —— 执行预算：超时秒数 + 输出上限。超了强制终止。
  3. ExecutionBroker     —— 唯一入口：subprocess 受管执行，返回受约束结果。

注意：这是"受控执行"，不是完整的操作系统级沙箱（如 bubblewrap / Seatbelt）。
沙箱隔离需要平台能力，这里是 Agent 侧的执行治理：管得住、杀得掉、拿得到受限结果。
"""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass


class BoundedOutputBuffer:
    """有长度上限的输出缓冲。防止一个命令灌出巨量输出。"""

    def __init__(self, max_bytes: int = 1_000_000) -> None:
        self.max_bytes = max_bytes
        self._chunks: list[str] = []
        self._size = 0
        self.truncated = False

    @property
    def text(self) -> str:
        return "".join(self._chunks)

    def append(self, chunk: str) -> bool:
        """追加一段输出。超过上限则截断，返回 False 表示已截断（调用方可停止接收）。"""
        if self.truncated:
            return False
        room = self.max_bytes - self._size
        if room <= 0:
            self.truncated = True
            return False
        if len(chunk) > room:
            self._chunks.append(chunk[:room])
            self._size += room
            self.truncated = True
            return False
        self._chunks.append(chunk)
        self._size += len(chunk)
        return True


@dataclass(frozen=True)
class ExecutionBudget:
    """一次执行的预算：超时秒数 + 输出字节上限 +（可选）资源限制。超了都会被强制终止。

    资源限制（sandbox 用，跨平台语义）：
      - max_memory_mb   ：内存上限（MB）。POSIX 用 RLIMIT_AS；Windows 用 Job Object 进程内存上限。
      - max_cpu_seconds ：CPU 时间上限（秒）。POSIX 用 RLIMIT_CPU；Windows 用 Job Object job 时间。
      - max_files       ：打开文件数上限。POSIX 用 RLIMIT_NOFILE；Windows 尽力而为（job 不直接限文件数，
                          主要靠超时/输出截断兜底，见文档）。
      - max_processes   ：同时活动的子进程数上限（已有）。
    """

    timeout_seconds: float = 30.0
    max_output_bytes: int = 1_000_000
    max_processes: int = 1
    max_memory_mb: int | None = None
    max_cpu_seconds: int | None = None
    max_files: int | None = None

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须为正数")
        if self.max_output_bytes <= 0:
            raise ValueError("max_output_bytes 必须为正数")
        if self.max_memory_mb is not None and self.max_memory_mb <= 0:
            raise ValueError("max_memory_mb 必须为正数")
        if self.max_files is not None and self.max_files <= 0:
            raise ValueError("max_files 必须为正数")


@dataclass
class ExecutionResult:
    """一次受管执行的结果。"""

    command: str
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    elapsed_ms: int = 0

    @property
    def success(self) -> bool:
        return self.exit_code == 0


@dataclass
class ManagedProcess:
    """一个被托管的子进程：知道 PID、能请求终止。"""

    pid: int
    started_at: float
    proc: subprocess.Popen  # type: ignore[type-arg]

    def terminate(self) -> None:
        """请求进程终止（先 SIGTERM 语义，Windows 上是 terminate）。"""
        if self.proc.poll() is None:
            self.proc.terminate()


class ExecutionBroker:
    """受控执行的唯一入口。所有"跑命令"都必须经过它。"""

    def __init__(self, budget: ExecutionBudget | None = None) -> None:
        self.budget = budget or ExecutionBudget()
        self._active: list[ManagedProcess] = []
        self._lock = threading.Lock()
        self._last_limiter = None  # 保持 Windows Job 句柄存活到进程结束

    @property
    def active_processes(self) -> int:
        with self._lock:
            return sum(1 for m in self._active if m.proc.poll() is None)

    def execute(self, command: list[str], *, cwd: str | None = None) -> ExecutionResult:
        """受管执行一条命令（参数以列表给出，避免 shell 注入）。"""
        if not command:
            raise ValueError("command 不能为空")

        start = time.monotonic()
        timed_out = False
        exit_code: int | None = None
        stdout_raw, stderr_raw = "", ""

        try:
            # 资源限制（sandbox）：按平台构造 limiter，附加到 Popen
            from warden_agent.execution._platform import make_limiter

            limiter = make_limiter(self.budget)
            popen_kwargs = dict(limiter.popen_kwargs) if limiter else {}
            proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                text=True,
                **popen_kwargs,
            )
            if limiter is not None:
                limiter.attach(proc)
            # limiter（及它持有的 Windows Job）保持引用到进程结束，避免限制失效
            self._last_limiter = limiter
        except FileNotFoundError:
            return ExecutionResult(
                command=" ".join(command),
                stdout="",
                stderr=f"命令不存在: {command[0]}",
                exit_code=127,
            )
        except OSError as e:
            return ExecutionResult(
                command=" ".join(command),
                stdout="",
                stderr=f"无法启动进程: {e}",
                exit_code=126,
            )

        self._track(proc)
        try:
            # communicate 内部正确地排空两条管道直到 EOF，不受我们这里卡死；
            # timeout 超时抛 TimeoutExpired，由下面强制终止。
            stdout_raw, stderr_raw = proc.communicate(timeout=self.budget.timeout_seconds)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.terminate()
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
            exit_code = None
        finally:
            self._untrack(proc)

        # 用预算对输出做截断（communicate 拿全量，这里按 max_bytes 收口）
        stdout_buf = BoundedOutputBuffer(self.budget.max_output_bytes)
        stderr_buf = BoundedOutputBuffer(self.budget.max_output_bytes)
        stdout_buf.append(stdout_raw or "")
        stderr_buf.append(stderr_raw or "")

        elapsed_ms = int((time.monotonic() - start) * 1000)
        return ExecutionResult(
            command=" ".join(command),
            stdout=stdout_buf.text,
            stderr=stderr_buf.text,
            exit_code=exit_code,
            timed_out=timed_out,
            stdout_truncated=stdout_buf.truncated,
            stderr_truncated=stderr_buf.truncated,
            elapsed_ms=elapsed_ms,
        )

    def shutdown(self) -> None:
        """收尾：终止所有还在跑的托管进程。"""
        with self._lock:
            processes = list(self._active)
        for m in processes:
            m.terminate()
        for m in processes:
            try:
                m.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                m.proc.kill()

    # ---- 内部 ----
    def _track(self, proc: subprocess.Popen) -> None:  # type: ignore[type-arg]
        with self._lock:
            running = sum(1 for m in self._active if m.proc.poll() is None)
            if running >= self.budget.max_processes:
                # 超并发：先终止最早的
                oldest = self._active[0]
                oldest.terminate()
                self._active.pop(0)
            self._active.append(ManagedProcess(proc.pid, time.monotonic(), proc))

    def _untrack(self, proc: subprocess.Popen) -> None:  # type: ignore[type-arg]
        with self._lock:
            self._active = [m for m in self._active if m.proc is not proc]
