"""本地假模型：不连网的『模拟大脑』，专门为了让你能跑通和看懂。

测试桩/Stub 概念。真模型(DeepSeek/OpenAI)做的事，它用几条写死的规则假装出来：
从最后一条用户消息里，找出里面提到哪个工具名，就"假装"它想调用那个工具。

我们为什么需要它？
- 让你不花钱、不联网就能把整条 Agent 链路跑起来、看懂。
- 测试时保证结果是确定的（真模型每次回答都不同，没法断言）。
- 它是"最小可用的模型实现"，你以后照着它写真模型实现，结构一模一样。

它假装"思考"的规则（写死不联网）：
1. 如果用户消息里出现了某个工具的名字（比如 "weather"），
   它就返回一个"我要调这个工具"的请求，参数从消息里猜（比如把"上海"当城市名）。
2. 如果消息里没提到任何工具，它就回复一句固定的客套话，表示"我干完了"。
"""
from __future__ import annotations

from warden_agent.model.model import (
    AgentChatModel,
    ChatRequest,
    ChatResponse,
    ToolCall,
)


class FakeModel(AgentChatModel):
    """不联网的假模型，用写死的规则演示 AI 与工具配合的完整流程。"""

    def __init__(self) -> None:
        self._counter = 0  # 给每次工具调用编个唯一的号

    def chat(self, request: ChatRequest) -> ChatResponse:
        # 拿最后一条"用户"消息来"思考"
        last_user = next(
            (m for m in reversed(request.messages) if m.role == "user"),
            None,
        )
        text = last_user.content if last_user else ""

        # 规则0：如果对话里已经出现过"工具结果"，说明工具已经跑过，
        #        我们就假装"看到了结果"，直接给出最终结论，结束循环。
        #        （否则假模型会永远要求调工具，导致循环不收敛。）
        if any(m.role == "tool" for m in request.messages):
            return ChatResponse(content="已完成，结果已获取。", finish_reason="stop")

        # 规则1：用户提到了某个工具名 -> 假装想调它
        for tool in request.tools:
            name = tool["function"]["name"]  # 比如 "weather.get"
            # 简单启发式：工具全名或工具"域"名(如 weather)出现在消息里，就认为想用它
            keywords = (name, name.split(".")[0])
            if any(k in text for k in keywords):
                self._counter += 1
                args = self._guess_args(tool, text)
                return ChatResponse(
                    content=None,
                    tool_calls=[ToolCall(id=f"call_{self._counter}", name=name, arguments=args)],
                    finish_reason="tool_calls",
                )

        # 规则2：没提到任何工具 -> 表示完成
        return ChatResponse(content="我没有可用的工具来处理这件事，已结束。", finish_reason="stop")

    @staticmethod
    def _guess_args(tool_schema: dict[str, object], text: str) -> dict[str, object]:
        """从用户消息里瞎猜工具参数。演示用，别当真。"""
        fn = tool_schema["function"]
        assert isinstance(fn, dict), "function 应为对象"
        params = fn["parameters"]
        assert isinstance(params, dict), "参数应为对象"
        props = params.get("properties", {})
        assert isinstance(props, dict), "properties 应为对象"
        args: dict[str, object] = {}
        # 很多演示参数是 string 类型，就从消息里抠一个中文词当值
        for pname, pmeta in props.items():
            assert isinstance(pmeta, dict), "属性元数据应为对象"
            if pmeta.get("type") == "string" and pname == "city":
                args[pname] = "上海"  # 演示：固定给个城市
            else:
                args[pname] = "示例值"
        return args
