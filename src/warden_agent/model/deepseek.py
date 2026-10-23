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
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from warden_agent.core.settings import env_opt, env_str
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


# 探不通的原因分档：**只许增加可操作的新档，不许把两件事合成一档**。
# 为什么要有这一层（笔记 95，2026-10-19）：界面上原本一律显示
# “探测失败：AuthenticationError: Error code: 401 …”，而用户真正要做的动作
# 是“换 Key / 去充值 / 改地址 / 等一等”——这四件事在原始异常里长得一模一样。
# 我自己今天就因为分不清「余额不足」和「Key 无效」而反复换 Key。
PROBE_FAILURE_HINTS: dict[str, str] = {
    "ok": "",
    "key_rejected": "API Key 被拒（失效、抄错或没权限）——重新导入 Key",
    "quota_exhausted": "账户余额不足或用额度用完（上游拒绝服务）——去上游充值",
    "model_not_found": "上游没有这个模型——检查模型名或 base_url",
    "rate_limited": "被上游限流——稍后重试",
    "timeout": "超时——网络慢或上游卡住（钥匙可能没问题）",
    "unreachable": "连不上上游（DNS / 网络 / 代理）——先查网络",
    "upstream_error": "上游 5xx——供应商自己挂了，不是你的 Key 问题",
    "unknown": "",
}


def _cause_chain(exc: BaseException) -> list[BaseException]:
    """沿 `__cause__` / `__context__` 展开（`probe()` 会把真原因包一层 ModelCallError）。"""
    seen: list[BaseException] = []
    cur: BaseException | None = exc
    while cur is not None and cur not in seen and len(seen) < 8:
        seen.append(cur)
        cur = cur.__cause__ or cur.__context__
    return seen


def probe_failure_code(exc: BaseException) -> str:
    """把探测失败归到一档**可操作**的原因上。

    只用结构信息（状态码 / 异常类名），**不看错误串措辞**做主要判断：
    OpenAI 兼容生态里字符串文案五花八门，而 `status_code` 是协议层的坑位。
    最后那条文本兜底只为兼容「不返 402、只在 body 里写余额」的网关，
    且它只在前面全部认不出时才生效。
    """
    for e in _cause_chain(exc):
        status = getattr(e, "status_code", None)
        name = type(e).__name__
        if status in (401, 403) or name in ("AuthenticationError", "PermissionDeniedError"):
            return "key_rejected"
        if status == 402:
            return "quota_exhausted"
        if status == 404 or name == "NotFoundError":
            return "model_not_found"
        if status == 429 or name == "RateLimitError":
            return "rate_limited"
        if name in ("APITimeoutError", "TimeoutError", "TimeoutException"):
            return "timeout"
        if name in ("APIConnectionError", "ConnectError", "ConnectionError", "gaierror"):
            return "unreachable"
        if isinstance(status, int) and 500 <= status < 600:
            return "upstream_error"
    text = str(exc).lower()
    if "quota" in text or "余额" in text or "insufficient" in text:
        return "quota_exhausted"
    if "timed out" in text or "timeout" in text:
        return "timeout"
    return "unknown"


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
        trust_env: bool = True,
    ) -> None:
        # api_key 可显式传入；否则从环境变量取；SSO 厂商也可用无 key（走 AZURE）——这里只处理标准 key
        key = api_key or env_opt("OPENAI_API_KEY") or env_opt("DEEPSEEK_API_KEY")
        if not key:
            raise ModelCallError(
                "缺少 API Key：请设置环境变量 OPENAI_API_KEY 或 DEEPSEEK_API_KEY，"
                "或在构造时传入 api_key。"
            )
        # 官方 SDK 客户端：支持超时/重试/流式
        from openai import DefaultHttpxClient, OpenAI

        # `trust_env=False` 时不读系统代理。自建接入必须关掉它：那条路存在的理由就是
        # 「对话不出你的网络」，而 Windows 的系统代理**不认** ProxyOverride 里的
        # `localhost;127.*;10.*;…`，httpx 照样把 127.0.0.1 / 10.x 的请求塞给代理 ——
        # 于是"内网端点"的对话被转发到了公网代理上（静默 502 只是它的表征）。
        # 内置公网厂商保留默认（`trust_env=True`）：在代理后面访问 api.deepseek.com 正是需要它。
        #
        # 用 `openai.DefaultHttpxClient` 而不是自己 `import httpx`：openai 3.x 内嵌的是
        # `httpx2`（发行包名不同、类也不同），拿顶层的 `httpx.Client` 传进去类型对不上
        # （mypy 会拦），运行时也可能不是同一个 transport 实现。这个类是 SDK 自己导出的，
        # 版本跟着 SDK 走。
        http_client = None if trust_env else DefaultHttpxClient(
            timeout=timeout, trust_env=False
        )
        self._client = OpenAI(api_key=key, base_url=base_url,
                              timeout=timeout, max_retries=max_retries,
                              http_client=http_client)
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

    def probe(self, timeout: float = 5.0) -> None:
        """探一次「这个 provider 到底通不通」，失败就抛。

        只调 `GET /models`（列模型目录，不消耗 token，也不走对话配额）：
        它验证的正是网关里最容易单独坏掉的那一环——鉴权 + 网络 + base_url。

        为什么需要它：`/models` 曾经只用「有没有 key」回答「能不能用」，
        于是界面上写「可用」、用户发出第一条消息才被告知 provider 挂了（笔记 95）。
        """
        try:
            self._client.with_options(timeout=timeout, max_retries=0).models.list()
        except Exception as e:  # noqa: BLE001 —— 任何失败都是「探不通」，原因要原样带出去
            raise ModelCallError(f"探测失败：{type(e).__name__}: {e}") from e

    # ================= 统一接口入口 =================
    def chat(self, request: ChatRequest) -> ChatResponse:
        kwargs = self._build_kwargs(request)
        if request.stream:
            return self._chat_stream(kwargs)
        return self._chat_once(kwargs)

    def _build_kwargs(self, request: ChatRequest) -> dict[str, Any]:
        """组装发往 OpenAI 兼容端点的参数（chat / chat_stream_iter 共用）。"""
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
        return kwargs

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

    # ================= 真流式：生成器，边收 chunk 边产出增量 =================
    def chat_stream_iter(self, request: ChatRequest) -> Iterator[dict[str, Any]]:
        """真流式生成器：模型每吐一段就立刻 yield，不等整段生成完。

        产出格式：
          {"type": "delta", "text": "..."}   —— 一小段增量文字
          {"type": "done",  "response": ChatResponse} —— 流结束，
            response 里带完整的 content / tool_calls / finish_reason（供会话循环续跑）。
        """
        kwargs = self._build_kwargs(request)
        deltas: list[str] = []
        tool_acc: dict[int, dict[str, Any]] = {}
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
                    # 关键差异：收到就立刻往外吐，而不是攒到最后
                    yield {"type": "delta", "text": delta.content}
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
        yield {
            "type": "done",
            "response": ChatResponse(
                content=content,
                tool_calls=tool_calls,
                finish_reason=finish_reason or ("tool_calls" if tool_calls else "stop"),
                deltas=deltas,
                usage=usage,
            ),
        }

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
        key = api_key or env_opt("DEEPSEEK_API_KEY")
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
        key = api_key or env_opt("ZHIPU_API_KEY")
        super().__init__(api_key=key, model=model,
                         base_url=kwargs.pop("base_url", ZHIPU_BASE_URL), **kwargs)


class BailianModel(OpenAiCompatibleModel):
    """阿里云百炼（通义）：OpenAI 兼容端点，默认读 DASHSCOPE_API_KEY。"""

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_BAILIAN_MODEL,
                 **kwargs: Any) -> None:
        key = api_key or env_opt("DASHSCOPE_API_KEY")
        super().__init__(api_key=key, model=model,
                         base_url=kwargs.pop("base_url", BAILIAN_BASE_URL), **kwargs)


# 厂商名 -> (子类, 使用的环境变量)。用于 create_model 按名选择。
_PROVIDERS: dict[str, type[OpenAiCompatibleModel]] = {
    "deepseek": DeepSeekModel,
    "openai": OpenAIModel,
    "zhipu": ZhipuModel,
    "bailian": BailianModel,
}


@dataclass(frozen=True)
class CustomEndpoint:
    """自建端点的启动配置。**可能只有一半**（有地址缺 key），调用方要看得见。"""

    base_url: str | None
    api_key: str | None
    model: str

    @property
    def ready(self) -> bool:
        """三件套齐全才算能用。

        只有地址没有 key 时若照样放行，空 key 会交给 SDK，而 `OpenAiCompatibleModel`
        在 key 为空时会回落到 `OPENAI_API_KEY` / `DEEPSEEK_API_KEY` —— 等于把别家的
        密钥发到你这个端点上。
        """
        return bool(self.base_url and self.api_key)


def custom_endpoint_from_env() -> CustomEndpoint:
    """读「要不要用自建端点」这份启动配置（未配置时各字段为 None / 默认模型名）。

    为什么这份读取落在这里而不是 `web/run_server.py`：`WARDEN_BASE_URL` /
    `WARDEN_MODEL_API_KEY` / `WARDEN_MODEL` 在 `core/settings.py` 的 ENV_SPECS 里
    登记的归属模块就是本文件（`test_config_surface.py::test_只有授权模块能读某个变量`
    会拦住第二个读取方——那是防"同名两用"的机制，不该绕过）。启动方只问
    「配好了没有 / 是半截吗」，不自己再拼一遍。

    返回值**不折叠成"是否就绪"**：启动日志要能说出「设了地址但缺 key」，
    那是最难查的一类失败（配置写了、日志里没提、行为还是假模型）。
    """
    return CustomEndpoint(
        base_url=env_opt("WARDEN_BASE_URL"),
        api_key=env_opt("WARDEN_MODEL_API_KEY"),
        model=env_str("WARDEN_MODEL", DEFAULT_DEEPSEEK_MODEL),
    )


def create_model(provider: str, api_key: str | None = None, **kwargs: Any) -> OpenAiCompatibleModel:
    """按厂商名创建一个模型实例。

    provider 支持 deepseek / openai / zhipu / bailian / custom。
      - 内置四家：各自绑定 base_url 和默认 model，key 从对应环境变量读。
      - custom：通用接入任意 OpenAI 兼容 API。全部从环境变量读（零改源码）：
          WARDEN_MODEL_API_KEY -> API Key
          WARDEN_BASE_URL      -> 兼容端点（如自建网关 / Ollama / 硅基流动）
          WARDEN_MODEL         -> 模型名
        也支持直接在 create_model 传 base_url / model / api_key 覆盖。

    为什么 custom 用的是 `WARDEN_MODEL_API_KEY` 而不是 `WARDEN_API_KEY`：
      后者是 **HTTP 服务的鉴权密钥**（`web/run_server.py` 用它做 Bearer 认证）。
      两者同名会造成一个很难发现的坑——用 `WARDEN_API_KEY` 开鉴权、又用 custom 接自建网关时，
      模型会把你**服务端的鉴权密钥**当成模型 key 发给那个网关。所以这两个用途彻底分开。

    另一个刻意的设计：custom **不会**回落到 `OPENAI_API_KEY` / `DEEPSEEK_API_KEY`。
      否则"我把 custom 指向了某个第三方网关，却忘了配 key"会变成
      **把你的 DeepSeek key 发给那个第三方**——密钥外泄。custom 的 key 必须显式配置。

    用法：
        model = create_model("zhipu")          # 读 ZHIPU_API_KEY
        model = create_model("deepseek")       # 读 DEEPSEEK_API_KEY
        model = create_model("custom")  # 读 WARDEN_MODEL_API_KEY / WARDEN_BASE_URL / WARDEN_MODEL
    """
    if provider == "custom":
        key = api_key or env_opt("WARDEN_MODEL_API_KEY")
        base_url = kwargs.pop("base_url", env_opt("WARDEN_BASE_URL"))
        model = kwargs.pop("model",
                           env_str("WARDEN_MODEL", DEFAULT_DEEPSEEK_MODEL))
        if not base_url:
            raise ModelCallError(
                "custom 接入需要 WARDEN_BASE_URL（OpenAI 兼容端点）。"
                "可在 .env 里设 WARDEN_BASE_URL + WARDEN_MODEL + WARDEN_MODEL_API_KEY，"
                "或用 create_model('custom', base_url=..., model=..., api_key=...)。"
            )
        if not key:
            raise ModelCallError(
                "custom 接入需要密钥：请设 WARDEN_MODEL_API_KEY，"
                "或 create_model('custom', api_key=...)。\n"
                "  · 注意是 WARDEN_MODEL_API_KEY，**不是** WARDEN_API_KEY"
                "（后者是 HTTP 服务的鉴权密钥，两者用途不同）。\n"
                "  · 本机网关 / Ollama 这类不校验密钥的端点：填任意非空占位串，"
                "例如 WARDEN_MODEL_API_KEY=not-needed。"
            )
        # key 显式传入 → 基类不会再去回落 OPENAI_API_KEY/DEEPSEEK_API_KEY（防密钥外泄到第三方）
        # 不走系统代理：custom 存在的理由就是"对话不出你的网络"，而 Windows 的系统代理
        # 不认 ProxyOverride 里的 127.*/10.*（见本文件 OpenAiCompatibleModel 的注释），
        # 走代理时内网端点的请求会被静默转发到公网。调用方显式传 trust_env 时以调用方为准。
        kwargs.setdefault("trust_env", False)
        return OpenAiCompatibleModel(api_key=key, model=model, base_url=base_url, **kwargs)
    try:
        cls = _PROVIDERS[provider]
    except KeyError:
        choices = sorted(_PROVIDERS) + ["custom"]
        raise ModelCallError(f"未知厂商: {provider!r}，可选 {choices}") from None
    return cls(api_key=api_key, **kwargs)


def _safe_json(raw: str) -> dict[str, Any]:
    """把模型返回的 arguments 字符串安全解析成字典，坏 JSON 兜底为空。"""
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}
