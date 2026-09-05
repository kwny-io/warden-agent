"""DeepSeek / OpenAI 兼容模型实现 —— 用官方 SDK，完整还原真实能力。


DeepSeek 官方推荐的接法就是用 openai 官方 Python SDK，把 base_url 指向 DeepSeek：
    client = OpenAI(api_key=..., base_url="https://api.deepseek.com")
所以这里直接用官方 SDK，而不是自己手搓 httpx —— 更贴近生产、少踩坑。

完整还原的能力：
  1. 流式（SSE）：stream=True，逐段出内容（增量），而不是一次性返回。
  2. 完整工具调用：支持流式下 tool_call 的"参数分片累加"（真实 Agent 必踩的坑——
     模型把参数拆成一串 token 流过来，要拼起来再 json.loads）。
  3. 结构化输出：response_format={type:"json_schema", json_schema:{...}}，
     让模型严格按 JSON Schema 返回，从而可还原成类型化最终结果。
  4. 完整消息角色映射：system / user / assistant / tool（带 tool_call_id）。
  5. token 用量追踪（usage）。

多模型：同一个类改 base_url + model 就是另一个厂商。
  - DeepSeek:  base_url="https://api.deepseek.com", model="deepseek-chat"
  - OpenAI:    base_url="https://api.openai.com/v1",  model="gpt-4o-mini"
"""
from __future__ import annotations

import json
import os
from typing import Any

from warden_agent.model.model import (
    AgentChatModel,
    ChatRequest,
    ChatResponse,
    Message,
    ModelUsage,
    ToolCall,
)

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"

# 更多 OpenAI 兼容厂商（官方对接文档里的端点与默认模型）
ZHIPU_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"      # 智谱 GLM
DEFAULT_ZHIPU_MODEL = "glm-4-flash"
BAILIAN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"  # 阿里云百炼
DEFAULT_BAILIAN_MODEL = "qwen-plus"


class ModelCallError(Exception):
    """模型调用异常（网络 / 认证 / 服务端错误）。"""


# 兼容旧名：早期代码用的是 DeepSeekError
DeepSeekError = ModelCallError


class OpenAiCompatibleModel(AgentChatModel):
    """基于 OpenAI 官方 SDK 的兼容模型（DeepSeek / OpenAI 共用一份代码）。"""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_DEEPSEEK_MODEL,
        base_url: str = DEEPSEEK_BASE_URL,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        timeout: float = 60.0,
        *,
        max_retries: int = 2,
    ) -> None:
        # api_key 可显式传入；否则从环境变量取；SSO 厂商也可用无 key（走 AZURE）——这里只处理标准 key
        key = api_key or os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            raise ModelCallError(
                "缺少 API Key：请设置环境变量 OPENAI_API_KEY 或 DEEPSEEK_API_KEY，"
                "或在构造时传入 api_key。"
            )
        # 官方 SDK 客户端：支持超时/重试/流式
        from openai import OpenAI

        self._client = OpenAI(api_key=key, base_url=base_url,
                              timeout=timeout, max_retries=max_retries)
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

    # ================= 统一接口入口 =================
    def chat(self, request: ChatRequest) -> ChatResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": self._messages_to_openai(request.messages),
            "temperature": self.temperature,
        }
        if self.max_tokens:
            kwargs["max_tokens"] = self.max_tokens
        if request.tools:
            # 真实 OpenAI/DeepSeek 协议不允许工具名里有"."（只允许 [a-zA-Z0-9_-]），
            # 但框架内部允许 weather.get 这种可读命名。所以发送前做一次"合法化映射"：
            #   weather.get -> weather_get 发出；收到 tool_call 再映射回 weather.get。
            # 这是只有真实调 API 才会暴露的坑，mock 测试发现不了。
            kwargs["tools"], self._name_unmap = self._sanitize_tools(request.tools)
            kwargs["tool_choice"] = "auto"
        else:
            self._name_unmap = {}
        if request.structured_output is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_result",
                    "strict": True,
                    "schema": request.structured_output,
                },
            }
        if request.stream:
            return self._chat_stream(kwargs)
        return self._chat_once(kwargs)

    # ================= 非流式：一次返回 =================
    def _chat_once(self, kwargs: dict[str, Any]) -> ChatResponse:
        try:
            raw = self._client.chat.completions.create(**kwargs)
        except Exception as e:  # 网络/认证/服务端
            raise ModelCallError(f"模型调用失败: {e}") from e

        choice = raw.choices[0]
        message = choice.message
        return ChatResponse(
            content=message.content,
            tool_calls=self._parse_tool_calls(message.tool_calls),
            finish_reason=choice.finish_reason or "stop",
            usage=self._parse_usage(getattr(raw, "usage", None)),
        )

    # ================= 流式：逐段返回增量 =================
    def _chat_stream(self, kwargs: dict[str, Any]) -> ChatResponse:
        deltas: list[str] = []
        # 流式工具调用：按 index 累积每个 tool_call 的参数片段
        tool_acc: dict[int, dict[str, Any]] = {}   # index -> {"id","name","arguments"}
        finish_reason: str | None = None
        usage = None
        try:
            stream = self._client.chat.completions.create(**kwargs, stream=True)
            for chunk in stream:
                if not chunk.choices:
                    if getattr(chunk, "usage", None):
                        usage = self._parse_usage(chunk.usage)
                    continue
                delta = chunk.choices[0].delta
                finish_reason = chunk.choices[0].finish_reason or finish_reason
                if delta and delta.content:
                    deltas.append(delta.content)
                # 累积工具调用参数（流式下参数是一小段一小段来的）
                if delta and delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        acc = tool_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                        if tc.id:
                            acc["id"] = tc.id
                        if tc.function and tc.function.name:
                            acc["name"] = tc.function.name
                        if tc.function and tc.function.arguments:
                            acc["arguments"] += tc.function.arguments
                if getattr(chunk, "usage", None):
                    usage = self._parse_usage(chunk.usage)
        except Exception as e:
            raise ModelCallError(f"流式模型调用失败: {e}") from e

        content = "".join(deltas) if deltas else None
        tool_calls: list[ToolCall] | None = None
        if tool_acc:
            tool_calls = [
                ToolCall(
                    id=acc["id"] or f"call_{i}",
                    name=self._unmap_name(acc["name"]),
                    arguments=_safe_json(acc.get("arguments", "") or "{}"),
                )
                for i, acc in sorted(tool_acc.items())
            ]
        return ChatResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason or ("tool_calls" if tool_calls else "stop"),
            deltas=deltas,
            usage=usage,
        )

    # ================= 翻译：统一消息 -> OpenAI 消息 =================
    def _messages_to_openai(self, messages: list[Message]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "tool":
                # 工具结果必须携带 tool_call_id，且 content 是字符串
                out.append({
                    "role": "tool",
                    "tool_call_id": (m.tool_call.id if m.tool_call else ""),
                    "content": m.content,
                })
            elif m.role == "assistant" and m.tool_call is not None:
                # 把带 tool_call 的 assistant 消息完整还原（真实 API 需要 tool_calls 字段，
                # 且 content 显式给 null，否则 400）。工具名要合法化，参数要 JSON 字符串。
                tc = m.tool_call
                out.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": self._sanitize_tool_name(tc.name),
                            "arguments": json.dumps(tc.arguments or {}, ensure_ascii=False),
                        },
                    }],
                })
            else:
                out.append({"role": m.role, "content": m.content})
        return out

    # ================= 工具名合法化映射（真实 API 不允许"."）=================
    @staticmethod
    def _sanitize_tool_name(name: str) -> str:
        """把框架内可读的工具名（可能带点，如 weather.get）转成协议允许的（weather_get）。"""
        return name.replace(".", "_")

    @classmethod
    def _sanitize_tools(
        cls, tools: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """把一堆工具 schema 里的 function.name 合法化；返回 [新schema, 映射 old->new]。"""
        umap: dict[str, str] = {}
        sanitized: list[dict[str, Any]] = []
        for tool in tools:
            fn = tool.get("function", {})
            original = fn.get("name", "")
            new_name = cls._sanitize_tool_name(original)
            # 记录 新名 -> 原名（用于把模型返回的名字还原）
            umap[new_name] = original
            # 复制一份，替换名字
            new_fn = dict(fn)
            new_fn["name"] = new_name
            new_tool = dict(tool)
            new_tool["function"] = new_fn
            sanitized.append(new_tool)
        return sanitized, umap

    def _unmap_name(self, name: str) -> str:
        """把模型返回的工具名（weather_get）还原成框架名（weather.get）。"""
        # 用最新一次映射（一次 chat 调用内有效）
        return self._name_unmap.get(name, name)

    # ================= 翻译：OpenAI tool_calls -> 统一 ToolCall =================
    def _parse_tool_calls(self, raw: Any) -> list[ToolCall] | None:
        if not raw:
            return None
        calls: list[ToolCall] = []
        for tc in raw:
            calls.append(ToolCall(
                id=tc.id,
                name=self._unmap_name(tc.function.name),
                arguments=_safe_json(tc.function.arguments or "{}"),
            ))
        return calls or None

    @staticmethod
    def _parse_usage(raw: Any) -> ModelUsage | None:
        if raw is None:
            return None
        # 支持 dict 和 SDK 对象两种形态
        def _g(obj: Any, field: str) -> int | None:
            v = getattr(obj, field, None) if not isinstance(obj, dict) else obj.get(field)
            return v
        return ModelUsage(
            prompt_tokens=_g(raw, "prompt_tokens"),
            completion_tokens=_g(raw, "completion_tokens"),
            total_tokens=_g(raw, "total_tokens"),
        )

    def close(self) -> None:
        self._client.close()


# ---- 便捷别名：DeepSeek 和 OpenAI，读各自环境变量 ----
class DeepSeekModel(OpenAiCompatibleModel):
    """DeepSeek：用 openai SDK 接 DeepSeek 端点。"""

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_DEEPSEEK_MODEL,
                 **kwargs: Any) -> None:
        # 默认读 DEEPSEEK_API_KEY；OpenAiCompatibleModel 已兼容读取
        key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        super().__init__(api_key=key, model=model,
                         base_url=kwargs.pop("base_url", DEEPSEEK_BASE_URL), **kwargs)


class OpenAIModel(OpenAiCompatibleModel):
    """OpenAI 官方：同一套代码，换 base_url + model + 环境变量。"""

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_OPENAI_MODEL,
                 **kwargs: Any) -> None:
        super().__init__(api_key=api_key, model=model,
                         base_url=kwargs.pop("base_url", DEFAULT_OPENAI_BASE_URL), **kwargs)


class ZhipuModel(OpenAiCompatibleModel):
    """智谱 GLM：OpenAI 兼容端点，默认读 ZHIPU_API_KEY。"""

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_ZHIPU_MODEL,
                 **kwargs: Any) -> None:
        key = api_key or os.environ.get("ZHIPU_API_KEY")
        super().__init__(api_key=key, model=model,
                         base_url=kwargs.pop("base_url", ZHIPU_BASE_URL), **kwargs)


class BailianModel(OpenAiCompatibleModel):
    """阿里云百炼（通义）：OpenAI 兼容端点，默认读 DASHSCOPE_API_KEY。"""

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_BAILIAN_MODEL,
                 **kwargs: Any) -> None:
        key = api_key or os.environ.get("DASHSCOPE_API_KEY")
        super().__init__(api_key=key, model=model,
                         base_url=kwargs.pop("base_url", BAILIAN_BASE_URL), **kwargs)


# 厂商名 -> (子类, 使用的环境变量)。用于 create_model 按名选择。
_PROVIDERS: dict[str, type[OpenAiCompatibleModel]] = {
    "deepseek": DeepSeekModel,
    "openai": OpenAIModel,
    "zhipu": ZhipuModel,
    "bailian": BailianModel,
}


def create_model(provider: str, api_key: str | None = None, **kwargs: Any) -> OpenAiCompatibleModel:
    """按厂商名创建一个模型实例。provider 支持 deepseek/openai/zhipu/bailian。

    用法：
        model = create_model("zhipu")          # 读 ZHIPU_API_KEY
        model = create_model("deepseek")       # 读 DEEPSEEK_API_KEY
    """
    try:
        cls = _PROVIDERS[provider]
    except KeyError:
        raise ModelCallError(f"未知厂商: {provider!r}，可选 {sorted(_PROVIDERS)}") from None
    return cls(api_key=api_key, **kwargs)


def _safe_json(raw: str) -> dict[str, Any]:
    """把模型返回的 arguments 字符串安全解析成字典，坏 JSON 兜底为空。"""
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}
