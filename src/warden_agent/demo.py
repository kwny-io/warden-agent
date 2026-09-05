"""演示入口：用真实 DeepSeek 跑一次对话（或流式 / 带工具）。

用法：
    cd /d/warden-agent
    py -c "from warden_agent.demo import run_deepseek_demo; run_deepseek_demo()"
    py -c "from warden_agent.demo import run_stream_demo; run_stream_demo()"

运行前先设置 DEEPSEEK_API_KEY（见 README）。
"""
from __future__ import annotations

from warden_agent.core.config import load_env
from warden_agent.model.deepseek import DeepSeekError, DeepSeekModel
from warden_agent.model.model import ChatRequest, Message


def run_deepseek_demo(question: str = "你好，请用一句话介绍你自己。") -> None:
    """调一次真实 DeepSeek，打印模型回答。"""
    load_env()  # 读 .env（可选），密钥从环境变量取
    try:
        model = DeepSeekModel()
    except DeepSeekError as e:  # 没设 key
        print(f"[提示] {e}")
        print("设置方法(PowerShell)：\n  $env:DEEPSEEK_API_KEY = 'sk-xxxx'")
        return

    try:
        reply = model.chat(ChatRequest(messages=[Message(role="user", content=question)]))
        print("\n===== DeepSeek 回答 =====")
        print(reply.content)
        if reply.usage:
            print(f"\n[用量] prompt={reply.usage.prompt_tokens} "
                  f"completion={reply.usage.completion_tokens} total={reply.usage.total_tokens}")
    except DeepSeekError as e:
        print(f"[错误] {e}")
    finally:
        model.close()


def run_stream_demo(question: str = "请用三句话介绍一下流式输出。") -> None:
    """流式演示：模型一边生成一边打印（打字机效果），展示 SSE 增量能力。"""
    load_env()  # 读 .env（可选），密钥从环境变量取
    try:
        model = DeepSeekModel()
    except DeepSeekError as e:
        print(f"[提示] {e}")
        return

    print("\n===== 流式回答（边生成边输出）=====")
    try:
        reply = model.chat(ChatRequest(
            messages=[Message(role="user", content=question)], stream=True))
        for delta in reply.deltas:
            print(delta, end="", flush=True)
        print()
        if reply.usage:
            print(f"\n[用量] total={reply.usage.total_tokens}")
    except DeepSeekError as e:
        print(f"[错误] {e}")
    finally:
        model.close()


if __name__ == "__main__":
    run_deepseek_demo()


def run_build_agent_demo(question: str = "上海天气用weather.get查一下") -> None:
    """阶段9 门面演示：一行 build_agent() 装配出 Agent，离线就能跑。

    agent = build_agent(provider="deepseek", tools={...})  # 有 key 用真模型
    agent = build_agent(provider=None, tools={...})        # 无 key 用离线假模型
    """
    load_env()
    # 先定义一个 Pydantic 技能卡（自动出 schema + 校验，不用手写 JSON）
    from pydantic import BaseModel

    from warden_agent.tool.catalog import pydantic_tool

    class WeatherReq(BaseModel):
        city: str

    @pydantic_tool("weather.get", "查城市天气", WeatherReq)
    def get_weather(req: WeatherReq) -> str:
        return f"{req.city}: 晴, 25度"

    from warden_agent.agent import build_agent

    # 一行装配：工具集 + 离线假模型（不花钱也能跑）
    agent = build_agent(provider=None, tools=[get_weather])
    print("\n===== build_agent() 一键装配 =====")
    print(f"输入: {question}")
    print(f"回答: {agent.chat(question)}")

    # 类型化结果：让它按 Pydantic 类返回结构化的最终答案。
    # 注意：离线假模型只会回"已完成"这种纯文本，还原不了 JSON → 演示会抛 TypedOutputError。
    # 这说明 typed_reply 需要真模型按 schema 返回结构化 JSON（见 run_typed_demo）。
    class WeatherReport(BaseModel):
        city: str
        condition: str

    from warden_agent.runtime.session import TypedOutputError

    try:
        typed = agent.typed_reply(WeatherReport,
                                  "上海天气用weather.get查一下，然后总结成报告")
        print(f"类型化结果: city={typed.city}, condition={typed.condition}")
    except TypedOutputError as e:
        print(f"[类型化结果] 离线假模型无法返回结构化 JSON → {e}")
        print("想看类型化结果，用真模型：")
        print("  py -c \"from warden_agent.demo import run_typed_demo; run_typed_demo()\"")


def run_typed_demo() -> None:
    """类型化结果交付演示（配合 DeepSeek 真模型，若没有 key 会自动提示）。"""
    from pydantic import BaseModel

    class CityReport(BaseModel):
        city: str
        summary: str

    load_env()
    from warden_agent.agent import build_agent

    try:
        agent = build_agent(provider="deepseek")
    except Exception:  # noqa: BLE001 - 没 key 时的友好提示
        print("\n[提示] 未设置 DEEPSEEK_API_KEY，跳过真实类型化演示。")
        print("设置方法(PowerShell)：\n  $env:DEEPSEEK_API_KEY = 'sk-xxxx'")
        return

    print("\n===== 类型化结果（真模型按 schema 返回 → 还原成对象）=====")
    report = agent.typed_reply(CityReport, "介绍一下上海，返回结构化结果")
    print(f"city: {report.city}")
    print(f"summary: {report.summary}")
