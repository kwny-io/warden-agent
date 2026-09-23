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
