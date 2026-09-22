"""运行时会话（AgentSession）：把状态机、对话、工具、审批、持久化串成一个完整 Run。



相比之前的简化版，阶段2补上了三件""的事：
  1. 完整状态机恢复：不只恢复对话，还恢复 Run 状态（含"等待审批中"这类中间态）。
  2. 真正的 ASK 审批：遇到 ASK 不再直接放行，而是"挂起"进 WAITING_APPROVAL，
     等人工 approve()/reject() 后才继续执行那个被拦截的工具。
  3. 可驱动：返回一个"结果"，由外层(CLI / HTTP)决定怎么处理（收尾 or 等审批）。

一次会话的生命周期(配合状态机)：
  创建 -> 开始(PENDING->QUEUED->RUNNING) -> 循环思考
        -> 遇 ASK 工具 => WAITING_APPROVAL，等人工拍板
        -> 批准 => 执行该工具 -> 继续循环 ... 直到 COMPLETED 返回最终回答
  全程每步状态和对话都写进 SQLite，崩溃后可恢复。
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, cast

from warden_agent.core.run.status import AgentRun, RunStatus
from warden_agent.loop.cognition import (
    intent_hint,
    manage_context,
    plan_context,
    recall_context,
)
from warden_agent.loop.loop import exec_tool
from warden_agent.memory.owner import owner_scope
from warden_agent.model.model import AgentChatModel, ChatRequest, ChatResponse, Message, ToolCall
from warden_agent.policy.policy import Decision, PolicyDenied, PolicyEngine
from warden_agent.runtime.checkpoint import (
    CheckpointManager,
    CheckpointStore,
    CompletionGuard,
)
from warden_agent.store.base import RunStore
from warden_agent.tool.catalog import ToolCatalog

logger = logging.getLogger(__name__)


# PolicyDenied 统一在 policy/policy.py 定义（loop 与 session 共用），此处不再重复定义。


class TypedOutputError(Exception):
    """类型化结果交付失败：模型返回的内容还原不成给定 Pydantic 类型。"""


class RunNotResumable(Exception):
    """该 Run 不能自动续跑（已完成/已取消，或正卡在等人工）。

    崩溃恢复时区分"能自动续"和"必须等人"很关键：把等待审批的 Run 自动续跑，
    等于绕过人工闸门；把已完成的 Run 再跑一遍，是重复执行。
    """


@dataclass
class ApprovalRequest:
    """一次"需要人工批准"的请求，等 approve/reject。"""

    approval_id: str
    tool_name: str
    arguments: dict[str, Any]
    reason: str


# ---- 会话对外返回的两种结果 ----
@dataclass
class FinalReply:
    """会话正常结束，返回最终回答 + 全程对话。"""

    text: str
    messages: list[Message]


@dataclass
class NeedsApproval:
    """会话被审批卡住，等外层拿到 approval 去 approve/reject。"""

    approval: ApprovalRequest


SessionOutcome = FinalReply | NeedsApproval


def _ensure_tool_results(messages: list[Message]) -> list[Message]:
    """补齐历史里"悬空"的工具调用后再发给模型（不动数据库里的原始记录）。

    真实 API（DeepSeek/OpenAI）要求 assistant 的 tool_calls 后面必须紧跟
    对应 tool_call_id 的 tool 结果，否则 400。历史里可能有被中断的调用
    （比如曾异常退出在"记完工具调用、还没执行"之间），这里在请求层
    给每个悬空调用补一条合成的 tool 结果。
    """
    out: list[Message] = []
    pending: list[ToolCall] = []  # 还没等到结果的 tool_calls
    for m in messages:
        if m.role == "tool" and m.tool_call:
            out.append(m)
            for i, c in enumerate(pending):
                if c.id == m.tool_call.id:
                    pending.pop(i)
                    break
            continue
        if pending:
            # 后面跟的不是对应的 tool 结果 → 这批悬空调用永远等不到了，就地补齐
            for c in pending:
                out.append(Message(
                    role="tool",
                    content="[工具调用被中断，未获得结果]",
                    tool_call=c,
                ))
            pending = []
        out.append(m)
        if m.role == "assistant" and m.tool_call:
            pending.append(m.tool_call)
    for c in pending:  # 历史末尾就悬着的情况
        out.append(Message(role="tool", content="[工具调用被中断，未获得结果]", tool_call=c))
    return out


class AgentSession:
    """一次可恢复、可审批的 Agent 运行会话。"""

    def __init__(
        self,
        run_id: str,
        model: AgentChatModel,
        catalog: ToolCatalog,
        policy_engine: PolicyEngine,
        store: RunStore,
        system_prompt: str = "你是一个能使用工具的助手。",
        max_iterations: int = 10,
        stability: Any = None,
        planner: Any = None,
        intent: Any = None,
        memory: Any = None,
        memory_scope: Any = None,
        max_context_chars: int = 0,
        checkpoint_store: CheckpointStore | None = None,
    ) -> None:
        self.run_id = run_id
        self.model = model
        self.catalog = catalog
        self.policy = policy_engine
        self.store = store
        self.system_prompt = system_prompt
        self.max_iterations = max_iterations
        # 工具稳定性层（StableToolExecutor）：可选。给了它，这个会话里的工具调用
        # 就走"超时+退避重试+降级/熔断"（与 AgentLoop 共用 exec_tool 单一来源），
        # 让稳定性在产品路径(HTTP/流式/CLI)也生效。
        self.stability = stability
        # 认知能力（与会话侧和 demo 侧的 AgentLoop **共用 loop/cognition.py 同一份实现**）：
        #   planner          阶段规划：复杂任务拆阶段，注入当前阶段目标
        #   intent           意图路由：调用前校验"该不该调"，疑似误调则提示而非执行
        #   memory/scope     记忆按需取用：按关键词重叠注入相关记忆
        #   max_context_chars 上下文裁剪阈值（0 = 不裁剪）
        self.planner = planner
        self.intent = intent
        self.memory = memory
        self._memory_scope = memory_scope
        self.max_context_chars = max_context_chars
        # 存档点：把"跑到第几轮、正在哪一步"也落库。load_run + messages 能还原状态与
        # 对话，但还原不出迭代位置——那正是跨 Run 恢复控制器（runtime/recovery.py）
        # 分组的依据。没给 store 时是空实现（内存态），不影响会话本身。
        self._checkpoints = CheckpointManager(checkpoint_store)
        # 重启后接续"这是第几次尝试"，别让重试计数归零（否则重试上限形同虚设）
        self._checkpoints.adopt_attempts_from(run_id)

        # 结构化输出目标（typed_reply 用）：类型化结果还原
        self._reply_type: Any = None
        self._reply_schema: dict[str, Any] | None = None

        # 从数据库恢复：有历史就恢复状态+对话，没有就是全新 run
        self.run = store.load_run(run_id) or AgentRun(run_id)
        self.messages: list[Message] = store.load_messages(run_id)
        # 当前正被审批拦截、等待放行的工具调用（批准后才执行）
        self._gated: ToolCall | None = None
        self._approval: ApprovalRequest | None = None
        # 完成门禁的基线：驱动开始时已存在的悬空工具调用（恢复来的历史，不算"本次未执行"）
        self._dangling_baseline: set[str] = set()

        # 恢复"等待审批"的中间态：把上次卡住的那一步也还原
        pending = store.load_pending_approval(run_id)
        if pending:
            approval_id, tool_name, arguments, reason = pending
            self._approval = ApprovalRequest(
                approval_id=approval_id,
                tool_name=tool_name,
                arguments=arguments,
                reason=reason,
            )
            # 从历史里找回被挂起的"原始" tool_call（模型返回的 id 必须原样保留）：
            # 它是最后一条 assistant(tool_call)，且尚未出现配对的 tool 结果。
            # 找不回才退回用审批单号重建。
            original: ToolCall | None = None
            answered = {
                m.tool_call.id for m in self.messages if m.role == "tool" and m.tool_call
            }
            for m in reversed(self.messages):
                if (
                    m.role == "assistant"
                    and m.tool_call
                    and m.tool_call.name == tool_name
                    and m.tool_call.id not in answered
                ):
                    original = m.tool_call
                    break
            self._gated = ToolCall(
                id=(original.id if original else approval_id),
                name=tool_name,
                arguments=(original.arguments if original else arguments),
            )

        # 若消息里没有系统指令且会话刚建，补一条
        if not any(m.role == "system" for m in self.messages):
            # 必须插在最前面：恢复历史时若追加到末尾，
            # 会出现 [user, assistant(tool_call), system, tool] 这种
            # system 拆散工具调用配对的序列，真实 API 直接 400
            self.messages.insert(0, Message(role="system", content=system_prompt))

    # ---------- 持久化辅助 ----------
    def _persist_run(self) -> None:
        self.store.save_run(self.run)

    def _checkpoint(self, step: str, iteration: int) -> None:
        """记一个存档点（run_id + 此刻状态 + 迭代编号 + 进行到哪一步）。

        存档写失败不能拖垮会话——降级为告警，与审计的"尽力而为"策略一致。
        """
        try:
            self._checkpoints.capture(self.run, iteration, step)
        except Exception:  # noqa: BLE001 - 存档是辅助能力，不能成为主路径单点
            logger.warning("存档点写入失败 run=%s step=%s", self.run_id, step)

    def _persist_all_messages(self) -> None:
        # 简化：会话持有的消息作为整体重写（教学版）；生产可用增量 append
        for m in self.messages:
            self.store.append_message(self.run_id, m)

    # ---------- 对外主入口 ----------
    def start(self, user_text: str) -> SessionOutcome:
        """开始(或继续)处理一句用户指令，返回：最终回答 / 需要审批。"""
        # 多轮对话：上一轮已结束（COMPLETED 等），新消息就开启新一轮执行周期
        if self.run.is_terminal():
            self.run.restart()
            self._persist_run()

        if self.run.status in (RunStatus.PENDING, RunStatus.QUEUED):
            self.run.mark_queued()
            self.run.start()
            self._persist_run()

        if not self._already_has_user_turn(user_text):
            self.messages.append(Message(role="user", content=user_text))
            self.store.append_message(self.run_id, self.messages[-1])

        return self._advance()

    def resume(self) -> SessionOutcome:
        """从已存状态**继续**一次未完成的运行（不追加新的用户输入）。

        与 `start()` 的区别：`start()` 需要一句新用户指令；`resume()` 用于"进程崩溃/
        重启后回到存档点接着跑"——状态与对话都已从库里恢复，直接从循环继续。
        跨 Run 恢复工作进程（`runtime/worker.py`）就是靠它把计划执行掉的。

        可续 / 不可续：
          - `FAILED`（终态）→ 允许：这就是"重试"，回到 PENDING 重新驱动；
          - `COMPLETED` / `CANCELLED` / `TIMED_OUT` → 抛 `RunNotResumable`（跑了是重复执行）；
          - `WAITING_APPROVAL` / `WAITING_INTERACTION` / `SUSPENDED` → 抛 `RunNotResumable`
            （**必须等人**，自动续跑等于绕过人工闸门）；
          - 其余（PENDING / QUEUED / RUNNING / …）→ 继续跑。
        """
        if self.run.status == RunStatus.FAILED:
            # 重试：清掉上次残留的审批状态，回到 PENDING 重新驱动。
            # attempts +1 并随存档点写回——重试上限就是靠它判断的。
            self._checkpoints.attempts += 1
            self._clear_approval()
            self.run.restart()
            self._persist_run()
        elif self.run.is_terminal():
            raise RunNotResumable(
                f"Run {self.run_id!r} 已是终态 {self.run.status.name}，无需续跑"
            )
        if self.run.status in (
            RunStatus.WAITING_APPROVAL,
            RunStatus.WAITING_INTERACTION,
            RunStatus.SUSPENDED,
        ):
            raise RunNotResumable(
                f"Run {self.run_id!r} 正在等人工（{self.run.status.name}），不能自动续跑"
            )
        if self.run.status in (RunStatus.PENDING, RunStatus.QUEUED):
            self.run.mark_queued()
            self.run.start()
            self._persist_run()
        return self._advance()

    def run_typed(self, reply_type: Any, user_text: str) -> Any:
        """类型化结果交付：让模型严格按给定 Pydantic 类的 schema 返回，还原成对象。

        - reply_type：一个 Pydantic 模型类（如 class WeatherReport(BaseModel)）。
        - 会话用它的 JSON Schema（structured_output）驱动模型 → 模型返回 JSON →
          `reply_type.model_validate()` 校验 → 还原成类型化对象。

        返回值：reply_type 的实例。若模型返回的 JSON 不符合 schema，抛 TypedOutputError。
        """
        self._reply_type = reply_type
        self._reply_schema = reply_type.model_json_schema()

        # 多轮对话：上一轮已结束，新消息开启新一轮执行周期
        if self.run.is_terminal():
            self.run.restart()
            self._persist_run()

        if self.run.status in (RunStatus.PENDING, RunStatus.QUEUED):
            self.run.mark_queued()
            self.run.start()
            self._persist_run()

        if not self._already_has_user_turn(user_text):
            self.messages.append(Message(role="user", content=user_text))
            self.store.append_message(self.run_id, self.messages[-1])

        return self._advance_typed()

    # ---------- 审批入口 ----------
    def pending_approval(self) -> ApprovalRequest | None:
        """当前是否有等待审批的请求。"""
        return self._approval

    def approve(self) -> SessionOutcome:
        """人工批准被拦截的工具：执行它，继续循环。"""
        if not self._gated or not self._approval:
            raise RuntimeError("当前没有等待审批的请求")
        call = self._gated
        self._clear_approval()
        self.run.resume()  # WAITING_APPROVAL -> RUNNING
        self._persist_run()
        return self._execute_and_continue(call)

    def reject(self) -> SessionOutcome:
        """人工拒绝被拦截的工具：不执行它，把"已拒绝"作为工具结果告诉模型，继续。"""
        if not self._gated or not self._approval:
            raise RuntimeError("当前没有等待审批的请求")
        call = self._gated
        self._clear_approval()
        self.run.resume()
        self._persist_run()
        # 把"用户拒绝"作为工具结果，让模型知道这步没做
        # 关键：必须携带 call 引用，使 tool 消息的 tool_call_id 与 assistant 的
        # tool_calls[].id 一致，否则真实 API 会报 400（mock 测不出这条）。
        denied = Message(role="tool", content=f"[用户拒绝执行 {call.name}]", tool_call=call)
        self.messages.append(denied)
        self.store.append_message(self.run_id, denied)
        return self._advance()

    # ---------- 内部：主推进 ----------
    def _advance(self) -> SessionOutcome:
        """默认推进：循环跑完，最终内容作为 FinalReply 返回。"""
        return cast(SessionOutcome, self._run_loop(self._finalize_plain))

    def _advance_typed(self) -> Any:
        """类型化推进：循环跑完，最终内容校验还原成 reply_type 对象返回。"""
        return self._run_loop(self._finalize_typed)

    def _mark_failed(self) -> None:
        """把"驱动失败"落到 Run 状态上（非终态才置 FAILED）。

        为什么必须有这一步：驱动过程抛异常（模型报错 / 迭代超上限 / 策略拒绝）时，
        若只是把异常抛给调用方、状态仍停在 `RUNNING`，那么崩溃恢复会把它算作
        **可续跑**（`recovery.plan()` 只对 `FAILED` 走"重试 + attempts 上限"分支），
        于是 `attempts` 永不递增、重试上限形同虚设——一个坏 Run 会被**无限重试**，
        每一轮恢复都白跑一次。把状态置为 `FAILED` 后，"重试计数 + 上限"才真正生效。
        """
        if self.run.status in (RunStatus.COMPLETED, RunStatus.FAILED,
                               RunStatus.CANCELLED, RunStatus.TIMED_OUT):
            return  # 已终态（例如收尾 on_content 抛错时已 COMPLETED）——不改写
        self.run.fail()
        self._persist_run()

    @contextmanager
    def _fail_run_on_error(self) -> Iterator[None]:
        """驱动期异常 → 先标记 FAILED 再原样抛出（见 `_mark_failed` 的说明）。"""
        try:
            yield
        except Exception:
            self._mark_failed()
            raise

    def _dangling_tool_ids(self) -> set[str]:
        """历史里"assistant 发起了 tool_call、但还没有配对 tool 结果"的调用 id。

        注意：**不能**直接拿它当"未执行"——恢复来的历史里可能本就有悬空调用
        （进程在"记下工具调用、还没执行"之间崩过），那是 `_ensure_tool_results`
        在请求层补齐的合法场景，不该阻止完成。所以完成门禁用的是
        "本次驱动**新产生**的悬空"（减去驱动开始时的基线），见 `_mark_completed`。
        """
        answered = {
            m.tool_call.id for m in self.messages if m.role == "tool" and m.tool_call
        }
        return {
            m.tool_call.id for m in self.messages
            if m.role == "assistant" and m.tool_call and m.tool_call.id not in answered
        }

    def _mark_completed(self, content: str) -> None:
        """经**完成门禁**后把 Run 置为 COMPLETED（收口唯一的完成路径）。

        门禁只拦"**本次驱动**新产生的悬空工具调用"——即模型这一轮说要调、却没执行。
        恢复来的历史悬空（driving 开始时就存在）不算，那是合法的可恢复场景。
        所以门禁平时不触发；一旦触发，说明有改动让循环"带着未执行的调用就完成了"。
        """
        new_pending = self._dangling_tool_ids() - self._dangling_baseline
        CompletionGuard().validate(
            self.run,
            pending_tools=len(new_pending),
            has_final_content=bool(content),
        )
        self.run.begin_completing()
        self.run.complete()
        self._persist_run()

    def _last_user_text(self) -> str:
        """取当前这一轮的用户输入（各入口都会把它 append 进 messages）。"""
        for m in reversed(self.messages):
            if m.role == "user" and m.content:
                return m.content
        return ""

    def _tool_schema(self, name: str) -> dict[str, Any]:
        """取工具说明书（供意图路由器识别触发信号）；未注册返回空 dict。"""
        try:
            spec = self.catalog.get(name)
        except KeyError:
            return {}
        return {"description": spec.description, "parameters": spec.parameters_schema}

    def _request_messages(self) -> list[Message]:
        """构造本次发给模型的消息：system + 记忆上下文 + 阶段计划 + 历史（并裁剪）。

        记忆 / 计划是**每次请求临时注入**的，**不写进 `self.messages`** —— 否则会被
        持久化，恢复会话时重复叠加。上下文裁剪同理：只作用于本次请求，不改存档。

        这几步与 AgentLoop 共用 `loop/cognition.py` 的同一份实现，所以"认知能力"
        在产品路径（HTTP / CLI / 流式）上是真实生效的，不再是 demo 专属。
        """
        history = _ensure_tool_results(self.messages)
        user_text = self._last_user_text()
        extra: list[Message] = []
        # 记忆按**归属者**过滤：只召回当前用户自己的记忆（owner=run.user_id），
        # 否则会把别人的记忆注入本用户提示词（跨租户投毒）。
        mem = recall_context(self.memory, self._memory_scope, user_text,
                             owner=self.run.user_id)
        if mem:
            extra.append(Message(role="system", content=mem))
        plan = plan_context(self.planner, user_text)
        if plan:
            extra.append(Message(role="system", content=plan))
        # 注入在**首条 system 之后**，不要把 system 提示顶到最末尾
        combined = history[:1] + extra + history[1:] if extra and history else history
        return manage_context(combined, self.max_context_chars)

    def _cognition_hint(self, call: ToolCall) -> str | None:
        """调用前的意图校验：疑似误调时返回要喂回模型的提醒文案（与 AgentLoop 同一实现）。"""
        return intent_hint(
            self.intent, self._tool_schema(call.name), call,
            self._last_user_text(), self.messages,
        )

    def _run_loop(self, on_content: Callable[[str], Any]) -> Any:
        """循环骨架：模型调用 → 工具/审批 → 到最终内容交给 on_content。"""
        if self.run.status in (RunStatus.COMPLETED, RunStatus.FAILED,
                               RunStatus.CANCELLED, RunStatus.TIMED_OUT):
            raise RuntimeError(f"会话已结束，不能继续({self.run.status.name})")

        with owner_scope(self.run.user_id), self._fail_run_on_error():
            # 记录驱动起点：此后**新**产生的悬空工具调用才算"未执行"（见 _mark_completed）
            self._dangling_baseline = self._dangling_tool_ids()
            for iteration in range(self.max_iterations):
                self._checkpoint("model_call", iteration)
                response = self.model.chat(ChatRequest(
                    messages=self._request_messages(),
                    tools=[t.to_openai_schema() for t in self.catalog.all()],
                    structured_output=self._reply_schema,
                ))

                if response.tool_calls:
                    for call in response.tool_calls:
                        note = Message(role="assistant",
                                       content=f"[调用工具 {call.name}]", tool_call=call)
                        self.messages.append(note)
                        self.store.append_message(self.run_id, note)

                        verdict = self.policy.evaluate(call.name, call.arguments)
                        if verdict.decision == Decision.DENY:
                            logger.warning("策略 DENY 工具 %s: %s", call.name, verdict.reason)
                            raise PolicyDenied(
                                f"策略拒绝执行 {call.name!r}: {verdict.reason}"
                            )
                        if verdict.decision == Decision.ASK:
                            return self._hold_for_approval(call, verdict.reason, iteration)
                        # 【认知】调用前的意图校验：疑似误调就提示模型，不执行
                        hint = self._cognition_hint(call)
                        if hint is not None:
                            hint_msg = Message(role="tool", content=hint, tool_call=call)
                            self.messages.append(hint_msg)
                            self.store.append_message(self.run_id, hint_msg)
                            continue
                        self._checkpoint("tool_exec", iteration)
                        self._execute(call)
                    continue  # 本批工具都执行完，回到循环让模型再想

                if response.content is not None:
                    self.messages.append(Message(role="assistant", content=response.content))
                    self.store.append_message(self.run_id, self.messages[-1])
                    self._mark_completed(response.content)
                    self._checkpoint("done", iteration)
                    return on_content(response.content)

            raise RuntimeError("AgentLoop 迭代超过上限，任务未收敛（可能模型一直在调用工具）")

    def _finalize_plain(self, content: str) -> SessionOutcome:
        """普通收尾：内容直接作为最终回答。"""
        return FinalReply(text=content, messages=self.messages)

    def _finalize_typed(self, content: str) -> Any:
        """类型化收尾：把模型返回的 JSON 校验还原成 reply_type 对象。

        模型按 schema 返回 JSON → 用 reply_type 校验 → 还原成类型化对象。
        若不符合 schema（缺字段/类型错），抛 TypedOutputError。
        """
        if self._reply_type is None:
            raise RuntimeError("typed_reply 未设置回复类型")
        try:
            import json as _json
            data = _json.loads(content) if not isinstance(content, dict) else content
            return self._reply_type.model_validate(data)
        except Exception as e:  # noqa: BLE001 - JSON 解析/校验失败都归为类型化输出错误
            raise TypedOutputError(
                f"模型返回内容无法还原成 {self._reply_type.__name__}: {e}\n原始内容: {content}"
            ) from e


    # ---------- 流式（打字机）：逐 token 产出事件 ----------
    # 生成器 yield 的每个元素是一个"事件字典"，由 Web 层转成 SSE。
    # 事件类型：
    #   {"type":"start"}                    会话开始/继续
    #   {"type":"delta","text":"..."}       模型生成的增量（一个或多个字符）
    #   {"type":"tool","name":"...","arguments":{...}}  想调用工具
    #   {"type":"final","text":"..."}       最终回答（模型说完了）
    #   {"type":"needs_approval","approval":{...}}       需要审批
    def stream(self, user_text: str) -> Iterator[dict[str, Any]]:
        """流式入口：包一层"失败即标记 FAILED"，实现体见 `_stream_impl`。"""
        try:
            # owner_scope：让 memory.remember 这类共享工具知道"当前是谁在用"
            with owner_scope(self.run.user_id):
                yield from self._stream_impl(user_text)
        except Exception:
            # 与 `_run_loop` 同理：流式路径抛错也必须把 Run 置为 FAILED，
            # 否则它会停在 RUNNING、被恢复计划当成"可续跑"而无限重试。
            self._mark_failed()
            raise

    def _stream_impl(self, user_text: str) -> Iterator[dict[str, Any]]:
        """以生成器方式处理一句用户指令，逐增量产出事件（配合 SSE 打字机）。"""
        # 多轮对话：上一轮已结束（COMPLETED 等），新消息就开启新一轮执行周期。
        # 注意：stream() 不走 _run_loop 的终态拦截，必须在这里先重开，
        # 否则会在 wait_for_approval()/resume() 处撞上非法状态转换（UI 第二条消息报 500）。
        if self.run.is_terminal():
            self.run.restart()
            self._persist_run()

        if self.run.status in (RunStatus.PENDING, RunStatus.QUEUED):
            self.run.mark_queued()
            self.run.start()
            self._persist_run()

        if not self._already_has_user_turn(user_text):
            self.messages.append(Message(role="user", content=user_text))
            self.store.append_message(self.run_id, self.messages[-1])

        yield {"type": "start"}

        # 驱动起点基线：此后新产生的悬空工具调用才算"未执行"（见 _mark_completed）
        self._dangling_baseline = self._dangling_tool_ids()
        for iteration in range(self.max_iterations):
            self._checkpoint("model_call", iteration)
            request = ChatRequest(
                messages=self._request_messages(),
                tools=[t.to_openai_schema() for t in self.catalog.all()],
                stream=True,  # 流式：模型增量返回在 response.deltas 里
            )
            response: ChatResponse | None = None
            if hasattr(self.model, "chat_stream_iter"):
                # 真流式：模型边生成边吐增量，立刻透传给前端（打字机）
                for ev in self.model.chat_stream_iter(request):
                    if ev["type"] == "delta":
                        yield {"type": "delta", "text": ev["text"]}
                    else:  # done
                        response = ev["response"]
            else:
                # 老实现（假模型/脚本模型）：一次性返回，靠 response.deltas 补增量
                response = self.model.chat(request)
                for delta in response.deltas or []:
                    yield {"type": "delta", "text": delta}
            assert response is not None

            if response.tool_calls:
                for call in response.tool_calls:
                    # 先推工具事件，再落库
                    yield {"type": "tool", "name": call.name,
                           "arguments": call.arguments}
                    note = Message(role="assistant",
                                   content=f"[调用工具 {call.name}]", tool_call=call)
                    self.messages.append(note)
                    self.store.append_message(self.run_id, note)

                    verdict = self.policy.evaluate(call.name, call.arguments)
                    if verdict.decision == Decision.DENY:
                        logger.warning("策略 DENY 工具 %s: %s", call.name, verdict.reason)
                        raise PolicyDenied(
                            f"策略拒绝执行 {call.name!r}: {verdict.reason}"
                        )
                    if verdict.decision == Decision.ASK:
                        outcome = self._hold_for_approval(call, verdict.reason, iteration)
                        assert isinstance(outcome, NeedsApproval)
                        yield {"type": "needs_approval",
                               "approval": self._approval_dict(outcome.approval)}
                        return
                    # 【认知】调用前的意图校验（与上面非流式路径同一实现）
                    hint = self._cognition_hint(call)
                    if hint is not None:
                        hint_msg = Message(role="tool", content=hint, tool_call=call)
                        self.messages.append(hint_msg)
                        self.store.append_message(self.run_id, hint_msg)
                        yield {"type": "tool_hint", "name": call.name, "text": hint}
                        continue
                    # 流式下也把工具结果落库（复用 _execute，携带 tool_call_id；含稳定性+错误喂回）
                    self._checkpoint("tool_exec", iteration)
                    self._execute(call)
                continue

            if response.content is not None:
                self.messages.append(Message(role="assistant", content=response.content))
                self.store.append_message(self.run_id, self.messages[-1])
                self._mark_completed(response.content)
                self._checkpoint("done", iteration)
                yield {"type": "final", "text": response.content}
                return

        raise RuntimeError("AgentLoop 迭代超过上限，任务未收敛（可能模型一直在调用工具）")

    @staticmethod
    def _approval_dict(approval: ApprovalRequest) -> dict[str, Any]:
        return {
            "approval_id": approval.approval_id,
            "tool_name": approval.tool_name,
            "arguments": approval.arguments,
            "reason": approval.reason,
        }


    def _hold_for_approval(
        self, call: ToolCall, reason: str, iteration: int = 0
    ) -> SessionOutcome:
        """ASK：挂起，进 WAITING_APPROVAL，等人工拍板。"""
        self._gated = call
        self._approval = ApprovalRequest(
            approval_id=f"appr-{self.run_id}-{len(self.messages)}",
            tool_name=call.name,
            arguments=call.arguments,
            reason=reason or "需要人工批准",
        )
        self.run.wait_for_approval()  # RUNNING -> WAITING_APPROVAL
        self._persist_run()
        # 存档点状态 = WAITING_APPROVAL：跨 Run 恢复据此把它归入 awaiting_human
        # （不能自动续跑，必须等人拍板）
        self._checkpoint("awaiting_approval", iteration)
        # 把待审批的一步也存下来，重启后能继续等批准
        self.store.save_pending_approval(
            self.run_id,
            self._approval.approval_id,
            self._approval.tool_name,
            self._approval.arguments,
            self._approval.reason,
        )
        logger.info("工具 %s 需要人工批准，会话进入 WAITING_APPROVAL", call.name)
        return NeedsApproval(self._approval)

    def _execute_and_continue(self, call: ToolCall) -> SessionOutcome:
        """审批通过后：执行被拦截的工具，继续循环。"""
        self._execute(call)
        return self._advance()

    def _execute(self, call: ToolCall) -> None:
        result, error = exec_tool(self.catalog, self.stability, call.name, call.arguments)
        # 关键：工具结果必须携带与 assistant.tool_calls[].id 一致的 call 引用，
        # 否则真实 API 要求"assistant 发起的 tool_call 必须一一被 tool 消息响应"，
        # 会报 400（mock 测不出这条，只有真实调用会暴露）。
        if error is not None:
            # 工具失败 + 有稳定性层兜底仍失败 → 把错误喂回模型，让它自己纠正/换招
            content = f"[工具 {call.name} 执行失败，请修正后重试] {error}"
        else:
            content = str(result)
        msg = Message(role="tool", content=content, tool_call=call)
        self.messages.append(msg)
        self.store.append_message(self.run_id, msg)

    def _clear_approval(self) -> None:
        self._gated = None
        self._approval = None
        self.store.clear_pending_approval(self.run_id)

    def _already_has_user_turn(self, user_text: str) -> bool:
        """**当前这一轮**是否已经记过这条用户消息（防止崩溃后重发同一句而重复落库）。

        只检查**最后一条**消息，而不是扫描全历史：历史里更早出现过同样的话，那属于
        **上一轮**——多轮对话里用户完全可能重复说同一句（"再试一次"），
        按全历史匹配会把这一轮静默吞掉：模型照样被驱动了，但对话历史里少了一条用户消息
        （历史与执行不同步）。真正的"重发去重"应该用 `Idempotency-Key`（见 HTTP 层），
        而不是按内容猜。
        """
        if not self.messages:
            return False
        last = self.messages[-1]
        return last.role == "user" and (last.content or "") == user_text

    # ---------- 只读查询 ----------
    def status(self) -> RunStatus:
        return self.run.status

    def is_terminal(self) -> bool:
        return self.run.status in (RunStatus.COMPLETED, RunStatus.FAILED,
                                   RunStatus.CANCELLED, RunStatus.TIMED_OUT)
