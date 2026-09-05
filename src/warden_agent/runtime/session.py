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
from dataclasses import dataclass
from typing import Any, cast

from warden_agent.core.run.status import AgentRun, RunStatus
from warden_agent.loop.loop import exec_tool
from warden_agent.model.model import AgentChatModel, ChatRequest, Message, ToolCall
from warden_agent.policy.policy import Decision, PolicyDenied, PolicyEngine
from warden_agent.store.base import RunStore
from warden_agent.tool.catalog import ToolCatalog

logger = logging.getLogger(__name__)


# PolicyDenied 统一在 policy/policy.py 定义（loop 与 session 共用），此处不再重复定义。


class TypedOutputError(Exception):
    """类型化结果交付失败：模型返回的内容还原不成给定 Pydantic 类型。"""


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

        # 结构化输出目标（typed_reply 用）：类型化结果还原
        self._reply_type: Any = None
        self._reply_schema: dict[str, Any] | None = None

        # 从数据库恢复：有历史就恢复状态+对话，没有就是全新 run
        self.run = store.load_run(run_id) or AgentRun(run_id)
        self.messages: list[Message] = store.load_messages(run_id)
        # 当前正被审批拦截、等待放行的工具调用（批准后才执行）
        self._gated: ToolCall | None = None
        self._approval: ApprovalRequest | None = None

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
            self._gated = ToolCall(id=approval_id, name=tool_name, arguments=arguments)

        # 若消息里没有系统指令且会话刚建，补一条
        if not any(m.role == "system" for m in self.messages):
            self.messages.append(Message(role="system", content=system_prompt))

    # ---------- 持久化辅助 ----------
    def _persist_run(self) -> None:
        self.store.save_run(self.run)

    def _persist_all_messages(self) -> None:
        # 简化：会话持有的消息作为整体重写（教学版）；生产可用增量 append
        for m in self.messages:
            self.store.append_message(self.run_id, m)

    # ---------- 对外主入口 ----------
    def start(self, user_text: str) -> SessionOutcome:
        """开始(或继续)处理一句用户指令，返回：最终回答 / 需要审批。"""
        if self.run.status in (RunStatus.PENDING, RunStatus.QUEUED):
            self.run.mark_queued()
            self.run.start()
            self._persist_run()

        if not self._already_has_user_turn(user_text):
            self.messages.append(Message(role="user", content=user_text))
            self.store.append_message(self.run_id, self.messages[-1])

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

    def _run_loop(self, on_content: Callable[[str], Any]) -> Any:
        """循环骨架：模型调用 → 工具/审批 → 到最终内容交给 on_content。"""
        if self.run.status in (RunStatus.COMPLETED, RunStatus.FAILED,
                               RunStatus.CANCELLED, RunStatus.TIMED_OUT):
            raise RuntimeError(f"会话已结束，不能继续({self.run.status.name})")

        for _ in range(self.max_iterations):
            response = self.model.chat(ChatRequest(
                messages=self.messages,
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
                        return self._hold_for_approval(call, verdict.reason)
                    self._execute(call)
                continue  # 本批工具都执行完，回到循环让模型再想

            if response.content is not None:
                self.messages.append(Message(role="assistant", content=response.content))
                self.store.append_message(self.run_id, self.messages[-1])
                self.run.begin_completing()
                self.run.complete()
                self._persist_run()
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
        """以生成器方式处理一句用户指令，逐增量产出事件（配合 SSE 打字机）。"""
        if self.run.status in (RunStatus.PENDING, RunStatus.QUEUED):
            self.run.mark_queued()
            self.run.start()
            self._persist_run()

        if not self._already_has_user_turn(user_text):
            self.messages.append(Message(role="user", content=user_text))
            self.store.append_message(self.run_id, self.messages[-1])

        yield {"type": "start"}

        for _ in range(self.max_iterations):
            response = self.model.chat(ChatRequest(
                messages=self.messages,
                tools=[t.to_openai_schema() for t in self.catalog.all()],
                stream=True,  # 流式：模型增量返回在 response.deltas 里
            ))

            # 模型正在生成的文字：逐个增量往外推（打字机核心）
            for delta in response.deltas:
                yield {"type": "delta", "text": delta}

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
                        outcome = self._hold_for_approval(call, verdict.reason)
                        assert isinstance(outcome, NeedsApproval)
                        yield {"type": "needs_approval",
                               "approval": self._approval_dict(outcome.approval)}
                        return
                    # 流式下也把工具结果落库（复用 _execute，携带 tool_call_id；含稳定性+错误喂回）
                    self._execute(call)
                continue

            if response.content is not None:
                self.messages.append(Message(role="assistant", content=response.content))
                self.store.append_message(self.run_id, self.messages[-1])
                self.run.begin_completing()
                self.run.complete()
                self._persist_run()
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


    def _hold_for_approval(self, call: ToolCall, reason: str) -> SessionOutcome:
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
        # 简化判断：防止同一句重复入队（教学版）
        return any(m.role == "user" and m.content == user_text for m in self.messages)

    # ---------- 只读查询 ----------
    def status(self) -> RunStatus:
        return self.run.status

    def is_terminal(self) -> bool:
        return self.run.status in (RunStatus.COMPLETED, RunStatus.FAILED,
                                   RunStatus.CANCELLED, RunStatus.TIMED_OUT)
