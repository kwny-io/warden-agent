"""模型抽象层：Agent 的『大脑』。

  - AgentChatModel     -> 接口，代表"一个能对话的模型"
  - AgentChatRequest   -> 发给模型的一条请求（历史消息 + 可用的工具）
  - AgentChatResponse  -> 模型回的一句话 / 或模型想调用哪个工具

白话解释：
这一层的作用是把『AI 是哪个厂商的（DeepSeek？OpenAI？智谱？）』这件事藏起来。
上面（AgentLoop）只认我们定义的 AgentChatModel 接口，根本不管背后是哪个模型。
想要换模型，就换一个实现了这个接口的类，上面一行都不用改。
这叫"面向接口编程"，也是最重要的设计原则之一。

为了让你不看外部文档也能跑起来、也能测试，我们做了一个"本地假模型"
(FakeModel) —— 它不真连网，只按写好的规则假装是 AI。等你要接真模型时，
照着它再写一个真模型实现（见项目的面试指南里的说明方向）即可。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


# ---- 模型消息：一段对话里的一条话 ----
@dataclass
class Message:
    role: str   # 'system' 系统指令 / 'user' 用户说的 / 'assistant' AI说的 / 'tool' 工具结果
    content: str
    # 如果是 AI 想调用工具，这里存工具名 + 参数
    tool_call: ToolCall | None = None


@dataclass
class ToolCall:
    """AI 提出来的"要不要调工具"的请求。"""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的字典（用于存数据库/传输）。"""
        return {"id": self.id, "name": self.name, "arguments": self.arguments}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolCall:
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            arguments=dict(data.get("arguments") or {}),
        )


# ---- 发给模型的东西 ----
@dataclass
class ChatRequest:
    messages: list[Message]                    # 到目前为止的全部对话
    tools: list[dict[str, Any]] = field(default_factory=list)  # 可用的技能卡(OpenAI格式)
    stream: bool = False                       # 是否流式（SSE 增量返回）
    # 结构化输出：要求模型按给定 JSON Schema 返回，用于类型化最终结果（如 record）
    structured_output: dict[str, Any] | None = None


# ---- 模型回的东西 ----
@dataclass
class ChatResponse:
    content: str | None          # AI 说的话（如果只是调工具，可能没有话）
    tool_calls: list[ToolCall] | None = None  # AI 想调的工具
    finish_reason: str = "stop"  # stop=说完了 / tool_calls=想调工具
    # 流式：生成的中间增量（按内容逐段给），最终一轮 response 的 content 是拼好的全量
    deltas: list[str] = field(default_factory=list)
    # 用量（token 统计，来自模型的 usage）；流式模型可能只在最后一段带
    usage: ModelUsage | None = None


@dataclass
class ModelUsage:
    """一次模型调用的 token 用量。"""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


# ---- 模型接口：任何真模型/假模型都要实现这个 ----
class AgentChatModel(Protocol):
    """一个能对话的模型。想换模型就换一个实现这个接口的类。"""

    def chat(self, request: ChatRequest) -> ChatResponse:
        ...
