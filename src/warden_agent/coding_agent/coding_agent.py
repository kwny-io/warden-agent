"""Coding Agent —— 给一个需求，让它读代码、出 unified diff、走门禁落地。

定位：在 build_agent + git.apply_patch 之上做一个"最小 Coding Agent"。
它不是重型 IDE，而是把下面几件事串成一个可演示的闭环：
  1. 读仓库代码（受限的 code.list / code.read，限制在 workdir 内，防越权读盘）
  2. 让 Agent 理解需求、定位要改的地方
  3. 产出 unified diff 交给已有的 git.apply_patch（门禁校验基准后落地）
  4. 不自动 commit/push（保持一致的门禁语义，改动留成候选，人工决定）

之所以新建独立模块而不是直接塞进 build_agent 的默认工具集，是为了
保持"读代码"这种偏主动的能力按需启用，也让 Coding Agent 成为一个
可单独讲解/测试的产品切片。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from warden_agent.agent import build_agent
from warden_agent.model.model import AgentChatModel
from warden_agent.policy.policy import PolicyEngine, deny_when_path_in_protected
from warden_agent.tool.catalog import FunctionToolSpec, ToolSpec, function_tool

_MAX_READ_BYTES = 64 * 1024  # 单文件读取上限（防止把整个大文件灌给模型/tool）


def _resolve_within(root: str, rel: str) -> Path | None:
    """把 rel 解析到 root 下；若越出 root 则返回 None（防止读 workdir 之外的盘）。"""
    base = Path(root).resolve()
    # 拒绝绝对路径与 .. 逃逸
    p = (base / rel).resolve()
    return p if p.is_relative_to(base) else None


def _make_code_tools(workdir: str) -> list[ToolSpec]:
    """造两个受限的代码浏览工具：code.list（列目录）与 code.read（读单个文件）。"""

    @function_tool(
        "code.list",
        "列出 git 仓库 workdir 下某个子目录的文件/子目录（相对路径）。"
        "用于让 Agent 先看清仓库结构。",
        {"type": "object",
         "properties": {
             "path": {"type": "string",
                      "description": "相对 workdir 的目录路径；空或 '.' 表示根"}},
         "required": ["path"]},
        pure=True,
    )
    def code_list(path: str = ".") -> str:
        target = _resolve_within(workdir, path)
        if target is None or not target.is_dir():
            return f"[拒绝] 路径不在仓库内或不是目录: {path}"
        try:
            items = sorted(target.iterdir())
        except OSError as e:
            return f"[错误] {e}"
        lines = []
        for it in items[:200]:  # 限制条目数
            kind = "D" if it.is_dir() else "F"
            lines.append(f"{kind}  {it.name}")
        return "\n".join(lines) if lines else "(空目录)"

    @function_tool(
        "code.read",
        "读取 workdir 内某个文本文件的内容（最多 64KB），用于让 Agent 理解代码再改。",
        {"type": "object",
         "properties": {"path": {"type": "string", "description": "相对 workdir 的文件路径"}},
         "required": ["path"]},
        pure=True,
    )
    def code_read(path: str) -> str:
        target = _resolve_within(workdir, path)
        if target is None or not target.is_file():
            return f"[拒绝] 路径不在仓库内或不是文件: {path}"
        if target.stat().st_size > _MAX_READ_BYTES:
            return f"[截断] 文件超过 {_MAX_READ_BYTES} 字节，只读前 {_MAX_READ_BYTES} 字节"
        try:
            data = target.read_bytes()[: _MAX_READ_BYTES]
            return data.decode("utf-8", errors="replace")
        except OSError as e:
            return f"[错误] {e}"

    return [code_list, code_read]


@dataclass
class CodingResult:
    """一次需求驱动后的结果摘要。"""

    text: str  # Agent 的最终回答 / 说明
    applied_files: list[str] = field(default_factory=list)  # 被 git 落地改动的文件


def _protect_path_policy(protected_paths: tuple[str, ...] | None) -> PolicyEngine:
    """给 Coding Agent 装一道路径门禁：参数 path 落在受保护目录时 DENY。

    默认保护用户主目录（`~`）——即使模型/提示注入诱导工具去读 `~/.ssh` 这类
    宿主敏感路径，也会在执行前被策略挡下（与工具自身的 workdir 限制互为纵深防御）。
    """
    prefixes = protected_paths if protected_paths is not None else (str(Path.home()),)
    engine = PolicyEngine()
    engine.add(deny_when_path_in_protected(prefixes))
    return engine


def _extract_applied_files(tool_output: str) -> list[str]:
    """从 git.apply_patch 的返回文本里抽出落地改动的文件名。

    成功时形如 `已应用 2 个文件: a.py, b.py`；拒绝/冲突不会有这行，返回空列表。
    """
    match = re.search(r"已应用\s*\d+\s*个文件[:：]\s*(.*)", tool_output)
    if not match:
        return []
    return [name.strip() for name in match.group(1).split(",") if name.strip()]


def _track_applied(spec: ToolSpec, sink: list[str]) -> ToolSpec:
    """包一层 git 工具：执行后从产出里抽出落地文件记进 `sink`（不改原行为）。

    这样 CLI 才能真正报出"改了哪些文件"，而不是永远空列表。
    """
    if not isinstance(spec, FunctionToolSpec) or spec.function is None:
        return spec
    inner = spec.function

    def _wrapped(**kwargs: object) -> object:
        text = inner(**kwargs)
        for name in _extract_applied_files(str(text)):
            if name not in sink:
                sink.append(name)
        return text

    return replace(spec, function=_wrapped)


def run_coding_task(
    requirement: str,
    workdir: str,
    *,
    provider: str | AgentChatModel | None = None,
    expected_base_commit: str | None = None,
    max_iterations: int = 15,
    protected_paths: tuple[str, ...] | None = None,
) -> CodingResult:
    """对 workdir 仓库跑一个编码需求。

    - requirement         ：自然语言需求，如"给 hello.py 加一个 greet 函数"。
    - provider            ：None 用离线假模型；或 "deepseek" 等真实模型。
    - expected_base_commit：期望的基准 commit；None 只校验是仓库。
    - protected_paths     ：受保护目录前缀（传给 deny_when_path_in_protected）；
                            None 时默认保护用户主目录。
    """
    workdir = os.path.abspath(workdir)
    if not Path(workdir).is_dir():
        raise ValueError(f"workdir 不是目录: {workdir}")

    # 【结果收集】包一层 git.apply_patch，把落地文件记进 applied_files
    applied_files: list[str] = []
    # 复用 build_agent：工具 = 我们造的代码浏览工具 + git 门禁工具（带路径策略）
    agent = build_agent(
        provider=provider,
        tools=_make_code_tools(workdir) + _make_git_apply(
            workdir, expected_base_commit, applied_files),
        policy_engine=_protect_path_policy(protected_paths),
        system_prompt=(
            "你是一个 Coding Agent。你的任务是：先读仓库代码理解现状，再针对用户需求"
            "给出改动。若要落地，用 git.apply_patch 提交一段 unified diff（会做门禁校验）。"
            "不要自动提交。最后用中文说明你做了什么、改了什么文件。"
        ),
        max_iterations=max_iterations,
    )
    text = agent.chat(requirement)
    return CodingResult(text=text, applied_files=applied_files)


def _make_git_apply(
    workdir: str, expected_base_commit: str | None, applied_files: list[str],
) -> list[ToolSpec]:
    from warden_agent.git import make_git_tools

    return [
        _track_applied(spec, applied_files)
        for spec in make_git_tools(workdir, expected_base_commit=expected_base_commit)
    ]
