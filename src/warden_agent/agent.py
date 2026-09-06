"""一键装配门面：Agent —— 把"模型 + 工具 + 策略 + 存储"装成一个可直接对话的 Agent。

这个 Python 版给了一个更符合直觉的一行装配：
    agent = build_agent(provider="deepseek", tools={...})
    reply = agent.chat("上海天气怎么样")

价值：
  - 把阶段 2~8 攒的所有能力（状态机、审批、恢复、持久化、结构化输出）收敛成一个入口。
  - "开箱即用"是从"能用的零件"到"能交付的产品"的分界线，这正是 SDK 的分量。
"""

from __future__ import annotations

import shlex
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

from warden_agent.core.run.status import AgentRun
from warden_agent.model.fake import FakeModel
from warden_agent.model.model import AgentChatModel, Message
from warden_agent.policy.policy import PolicyEngine
from warden_agent.runtime.session import AgentSession, FinalReply, NeedsApproval
from warden_agent.store.base import RunStore
from warden_agent.tool.catalog import ToolCatalog, ToolSpec, function_tool

if TYPE_CHECKING:
    from warden_agent.execution.sandbox import SandboxSpec


class InMemoryRunStore:
    """进程内 RunStore（不落盘），方便 build_agent 开箱即用。

    完全实现 RunStore 接口（保持 SqliteStore 互换），适合演示/测试；
    要持久化就传一个 SqliteStore / PostgresStore 给 build_agent。
    """

    def __init__(self) -> None:
        self._runs: dict[str, AgentRun] = {}
        self._messages: dict[str, list[Message]] = {}
        self._pending: dict[str, tuple[str, str, dict[str, object], str]] = {}

    def save_run(self, run: AgentRun) -> None:
        self._runs[run.run_id] = run

    def load_run(self, run_id: str) -> AgentRun | None:
        run = self._runs.get(run_id)
        return run

    def append_message(self, run_id: str, message: Message) -> None:
        self._messages.setdefault(run_id, []).append(message)

    def load_messages(self, run_id: str) -> list[Message]:
        return list(self._messages.get(run_id, []))

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        """列出会话概要（前端对话列表用）：按插入序取最近 limit 个。

        字段形状与 SqliteStore.list_runs 对齐：title 取首条用户消息
        （没有消息的 run 回退用 run_id），msg_count 是对话条数。
        """
        items = list(self._runs.items())[-limit:]
        out: list[dict[str, Any]] = []
        for run_id, run in items:
            msgs = self._messages.get(run_id, [])
            first_user = next((m.content for m in msgs if m.role == "user"), None)
            out.append({
                "run_id": run_id,
                "status": run.status,
                "msg_count": len(msgs),
                "title": first_user or run_id,
            })
        return out

    def delete_run(self, run_id: str) -> None:
        self._runs.pop(run_id, None)
        self._messages.pop(run_id, None)
        self._pending.pop(run_id, None)

    def save_pending_approval(
        self,
        run_id: str,
        approval_id: str,
        tool_name: str,
        arguments: dict[str, object],
        reason: str,
    ) -> None:
        self._pending[run_id] = (approval_id, tool_name, arguments, reason)

    def load_pending_approval(
        self, run_id: str
    ) -> tuple[str, str, dict[str, object], str] | None:
        return self._pending.get(run_id)

    def clear_pending_approval(self, run_id: str) -> None:
        self._pending.pop(run_id, None)


class Agent:
    """门面返回的 Agent 对象：能对话、能流式、能要类型化结果。"""

    def __init__(
        self,
        session_factory: Callable[[str], AgentSession],
    ) -> None:
        self._session_factory = session_factory
        self._run_counter = 0

    def _new_session(self) -> AgentSession:
        self._run_counter += 1
        return self._session_factory(f"run-{self._run_counter}")

    def chat(self, user_text: str) -> str:
        """普通对话：返回最终回答文本。"""
        outcome = self._new_session().start(user_text)
        if isinstance(outcome, FinalReply):
            return outcome.text
        if isinstance(outcome, NeedsApproval):
            raise RuntimeError(
                f"需要审批: {outcome.approval.tool_name} "
                f"({outcome.approval.reason})"
            )
        raise RuntimeError(f"意外的会话结果: {outcome!r}")

    def stream(self, user_text: str) -> Iterator[dict[str, Any]]:
        """流式对话：逐增量产出事件（配合 SSE 打字机）。"""
        yield from self._new_session().stream(user_text)

    def typed_reply(self, reply_type: Any, user_text: str) -> Any:
        """类型化结果：让模型按 reply_type 的 schema 返回，还原成对象。"""
        return self._new_session().run_typed(reply_type, user_text)


def _make_sandbox_tool(spec: SandboxSpec) -> ToolSpec:
    """构造受沙箱管控的 `shell.run` 工具（build_agent(sandbox=True) 的落点）。

    安全语义全部来自 SandboxedExecutionBroker：只读工作区副本（改不到宿主）、
    默认禁网（NetworkPolicy 语义层拒绝）、超时强杀与输出量预算（ExecutionBudget）。
    """
    from warden_agent.execution.sandbox import SandboxedExecutionBroker

    broker = SandboxedExecutionBroker(spec=spec)

    @function_tool(
        "shell.run",
        "在受控沙箱中执行一条 shell 命令：命令跑在只读工作区副本里（改不到原目录），"
        "默认禁网（疑似联网的命令会被拒绝），超时与输出量受预算约束。"
        "用于需要运行命令/脚本的任务。",
        {"type": "object",
         "properties": {
             "command": {"type": "string",
                          "description": "要执行的命令行，如 python stats.py"},
             "workspace": {"type": "string",
                            "description": "可选：拷入只读工作区的输入目录；缺省为空工作区"}},
         "required": ["command"]},
        pure=False,
        triggers=("执行", "命令", "运行", "脚本", "shell", "run"),
    )
    def shell_run(command: str, workspace: str = "") -> str:
        try:
            argv = shlex.split(command)
        except ValueError as e:
            return f"[拒绝] 命令解析失败: {e}"
        if not argv:
            return "[拒绝] 命令为空"
        result = broker.execute(argv, workspace_input=workspace or None)
        head = f"exit_code: {result.exit_code}"
        if result.timed_out:
            head += "（超时被强制终止）"
        if result.stdout_truncated or result.stderr_truncated:
            head += "（输出被截断）"
        return (f"{head}\n--- stdout ---\n{result.stdout or '(空)'}\n"
                f"--- stderr ---\n{result.stderr or '(空)'}")

    return shell_run


def augment_catalog(
    catalog: ToolCatalog,
    *,
    memory: bool = False,
    skills: dict[str, str] | str | None = None,
    web: bool = False,
    mcp_server: str | None = None,
    git_workdir: str | None = None,
    memory_scope: Any = None,
    sandbox: bool = False,
    sandbox_spec: SandboxSpec | None = None,
) -> Any:
    """把 Memory / Skill / Web / MCP 的能力工具注册进目录（可复用给 HTTP 层）。

    返回：注入的运行时依赖（额外能力），目前是 MemoryService（所有会话共享）。
    不做任何抛出——能力不可用（如没 node）就静默跳过，保证降级不崩溃。
    """
    from warden_agent.memory import InMemoryMemoryStore, MemoryScope, MemoryService
    from warden_agent.memory.tools import make_memory_tools

    extra: dict[str, Any] = {}
    scope = memory_scope or MemoryScope.SESSION

    # 1. 记忆
    if memory:
        memory_service = MemoryService(InMemoryMemoryStore())
        for spec in make_memory_tools(memory_service, scope):
            catalog.register(spec)
        extra["memory_service"] = memory_service

    # 2. 技能（SKILL.md）
    if skills:
        from warden_agent.skill import SkillCatalog, SkillPackageParser, skill_activation_tool
        from warden_agent.skill.skill import load_skills_from_dir

        skill_catalog = SkillCatalog()
        parser = SkillPackageParser()
        if isinstance(skills, str):  # 传目录路径 → 批量加载
            load_skills_from_dir(skill_catalog, skills)
        else:  # 传 dict{alias: SKILL.md 文本}
            for alias, text in skills.items():
                skill_catalog.load_skill(alias, parser.parse(text), source="inline")
        for alias in skill_catalog.aliases():
            catalog.register(skill_activation_tool(skill_catalog, alias))
        extra["skill_catalog"] = skill_catalog

    # 3. Web 搜索/抓取
    if web:
        from warden_agent.web import make_web_tools
        for spec in make_web_tools():
            catalog.register(spec)

    # 4. MCP（需 node；可用则导入经过审查的工具）
    if mcp_server:
        import contextlib

        from warden_agent.mcp import McpClient, node_available
        if node_available():
            # MCP 连不上/超时不炸整个 agent（优雅降级）
            with contextlib.suppress(Exception):
                McpClient(mcp_server).import_reviewed(catalog)
        extra["mcp_server"] = mcp_server

    # 5. Git（revision 探测 + patch 应用门禁）
    if git_workdir:
        from warden_agent.git import make_git_tools
        from warden_agent.git.revision import DirectGitProbe, GitRepositoryRef, make_git_context

        rev = DirectGitProbe().inspect_head(
            make_git_context(), GitRepositoryRef(git_workdir))
        expected = rev.commit if rev.repository else None
        for spec in make_git_tools(git_workdir, expected_base_commit=expected):
            catalog.register(spec)
        extra["git_workdir"] = git_workdir

    # 6. 沙箱（受控命令执行：只读副本 + 默认禁网 + 预算约束）
    if sandbox:
        from warden_agent.execution.sandbox import SandboxSpec

        catalog.register(_make_sandbox_tool(sandbox_spec or SandboxSpec()))
        extra["sandbox"] = True

    return extra


def build_agent(
    provider: str | AgentChatModel | None = None,
    tools: dict[str, ToolSpec] | list[ToolSpec] | None = None,
    policy_engine: PolicyEngine | None = None,
    store: RunStore | None = None,
    system_prompt: str = "你是一个能使用工具的助手。",
    max_iterations: int = 10,
    api_key: str | None = None,
    memory: bool = False,
    skills: dict[str, str] | str | None = None,
    web: bool = False,
    mcp_server: str | None = None,
    git_workdir: str | None = None,
    sandbox: bool = False,
    sandbox_spec: SandboxSpec | None = None,
) -> Agent:
    """一键装配一个 Agent：模型 + 工具 + 策略 + 存储 +（可选）能力。

    参数：
      provider       模型来源。给字符串("deepseek"/"openai"/"zhipu"/"bailian")走真实模型；
                     给 AgentChatModel 实例直接用；None 用离线假模型(不花钱)。
      tools          工具集。dict(名字→技能卡) 或 list[ToolSpec]；None 表示无工具。
      policy_engine  审批策略。None 表示全部放行。
      store          存储。RunStore 实现(SqliteStore/InMemoryRunStore)；None 用进程内存。
      api_key        真实模型用。None 时读对应环境变量。
      memory         True=启用记忆工具(memory.remember/recall)。
      skills         dict{alias: SKILL.md} 或 目录路径 → 加载技能系统。
      web            True=启用 web.search / web.fetch。
      mcp_server     MCP server 启动命令（有 node 则导入其工具）。
      sandbox        True=暴露受沙箱管控的 shell.run 工具（只读工作区副本 +
                     默认禁网 + 超时/输出预算约束）；sandbox_spec 可自定义 SandboxSpec。
    """
    # 模型：字符串=厂商名走真实模型；有 chat 方法的对象=直接当模型实例；None=离线假模型
    model: AgentChatModel
    if isinstance(provider, str):
        from warden_agent.model.deepseek import create_model
        model = create_model(provider, api_key=api_key)
    elif provider is None:
        model = FakeModel()
    else:
        model = provider  # 视为已实现的 AgentChatModel 实例

    # 工具箱：先装用户工具，再扩充能力工具
    catalog = ToolCatalog()
    if tools:
        items = (tools.items() if isinstance(tools, dict)
                 else [(s.name, s) for s in tools])
        for _name, spec in items:
            catalog.register(spec)  # spec 是已生成的技能卡实例（见 pydantic_tool/function_tool）
    augment_catalog(
        catalog, memory=memory, skills=skills, web=web,
        mcp_server=mcp_server, git_workdir=git_workdir,
        sandbox=sandbox, sandbox_spec=sandbox_spec,
    )

    # 策略与存储
    policy = policy_engine or PolicyEngine()
    run_store: RunStore = store if store is not None else InMemoryRunStore()

    def make_session(run_id: str) -> AgentSession:
        return AgentSession(
            run_id=run_id,
            model=model,
            catalog=catalog,
            policy_engine=policy,
            store=run_store,
            system_prompt=system_prompt,
            max_iterations=max_iterations,
        )

    return Agent(make_session)
