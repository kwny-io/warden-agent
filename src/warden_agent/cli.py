"""warden —— 命令行入口：连接运行中的 Warden Agent HTTP 服务。

提供：
  warden chat <run_id> "<问题>"     送一句话给 Agent（POST /chat/{run_id}）
  warden chat-stream <run_id> "..."  流式对话（POST /chat/stream/{run_id}）
  warden approvals                  查看待审批队列（GET /approvals）
  warden approve <run_id>           批准该会话的审批（POST /approve/{run_id}）
  warden reject <run_id>            拒绝（POST /reject/{run_id}）
  warden health                     健康检查（GET /health/live + /health/ready）
  warden caps                       列出能力（GET /capabilities）

默认连 http://127.0.0.1:8000；可用环境变量 WARDEN_BASE_URL 覆盖。
需要先启动服务：  py -m warden_agent.web.run_server
"""

from __future__ import annotations

import argparse
import os
import sys

import httpx

DEFAULT_BASE = os.environ.get("WARDEN_BASE_URL", "http://127.0.0.1:8000")


def _client() -> httpx.Client:
    # trust_env=False：访问本地服务不走系统代理（避免 127.0.0.1 被代理转发导致 502）
    return httpx.Client(base_url=DEFAULT_BASE, timeout=60.0, trust_env=False)


def _die(msg: str, code: int = 1) -> None:
    print(f"warden: 错误: {msg}", file=sys.stderr)
    sys.exit(code)


def _cmd_chat(args: argparse.Namespace) -> None:
    try:
        with _client() as c:
            resp = c.post(f"/chat/{args.run_id}", json={"text": args.text})
            body = resp.json()
    except httpx.HTTPError as e:
        _die(f"无法连接服务 {DEFAULT_BASE}（先启动: py -m warden_agent.web.run_server）：{e}")
    if resp.status_code != 200:
        _die(f"服务返回 {resp.status_code}: {body.get('detail', body)}")
    kind = body.get("kind")
    if kind == "needs_approval":
        ap = body.get("approval", {})
        print("需要审批:")
        print(f"  工具   : {ap.get('tool_name')}")
        print(f"  参数   : {ap.get('arguments')}")
        print(f"  原因   : {ap.get('reason')}")
        print(f"  审批   : warden approve {args.run_id}   /   warden reject {args.run_id}")
        return
    print(body.get("text", ""))


def _cmd_approvals(args: argparse.Namespace) -> None:
    try:
        with _client() as c:
            resp = c.get("/approvals")
            items = resp.json()
    except httpx.HTTPError as e:
        _die(f"无法连接服务：{e}")
    if resp.status_code != 200:
        _die(f"服务返回 {resp.status_code}")
    if not items:
        print("（审批队列为空）")
        return
    for it in items:
        print(f"run_id : {it.get('run_id')}")
        print(f"  工具 : {it.get('tool_name')}  参数: {it.get('arguments')}")
        print(f"  原因 : {it.get('reason')}")
        print(f"  审批 : warden approve {it.get('run_id')}")


def _cmd_approve(args: argparse.Namespace) -> None:
    _do_decision("approve", args.run_id)


def _cmd_reject(args: argparse.Namespace) -> None:
    _do_decision("reject", args.run_id)


def _do_decision(action: str, run_id: str) -> None:
    try:
        with _client() as c:
            resp = c.post(f"/{action}/{run_id}")
            body = resp.json()
    except httpx.HTTPError as e:
        _die(f"无法连接服务：{e}")
    if resp.status_code != 200:
        _die(f"服务返回 {resp.status_code}: {body.get('detail', body)}")
    if body.get("kind") == "needs_approval":
        print(f"已{action}，但随后又触发新的审批:")
        print(f"  工具 : {body['approval']['tool_name']}")
        return
    print(f"{action} 完成 -> 状态: {body.get('status')}")
    if body.get("text"):
        print(body["text"])


def _cmd_stream(args: argparse.Namespace) -> None:
    try:
        with _client() as c, c.stream(
            "POST", f"/chat/stream/{args.run_id}", json={"text": args.text}
        ) as resp:
            if resp.status_code != 200:
                print(resp.text, file=sys.stderr)
                sys.exit(1)
            for line in resp.iter_lines():
                if line.strip():
                    print(line)
    except httpx.HTTPError as e:
        _die(f"无法连接服务：{e}")


def _cmd_health(args: argparse.Namespace) -> None:
    try:
        with _client() as c:
            live = c.get("/health/live")
            ready = c.get("/health/ready")
    except httpx.HTTPError as e:
        _die(f"无法连接服务：{e}")
    print(f"live  {live.status_code}  {live.text}")
    print(f"ready {ready.status_code}  {ready.text}")


def _cmd_caps(args: argparse.Namespace) -> None:
    try:
        with _client() as c:
            resp = c.get("/capabilities")
    except httpx.HTTPError as e:
        _die(f"无法连接服务：{e}")
    if resp.status_code != 200:
        _die(f"服务返回 {resp.status_code}")
    body = resp.json()
    print("工具:", ", ".join(body.get("tools", [])))
    print("特性:", body.get("features"))


def _cmd_coding(args: argparse.Namespace) -> None:
    """本地跑一个编码需求（不需 HTTP 服务）：读代码 → 出 diff → 走门禁落地。"""
    from warden_agent.coding_agent import run_coding_task

    result = run_coding_task(args.requirement, args.workdir)
    print(result.text)
    if result.applied_files:
        print("\n已应用改动的文件:", ", ".join(result.applied_files))


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="warden", description="Warden Agent 命令行")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_chat = sub.add_parser("chat", help="送一句话给 Agent")
    p_chat.add_argument("run_id")
    p_chat.add_argument("text")
    p_chat.set_defaults(func=_cmd_chat)

    p_stream = sub.add_parser("stream", help="流式对话")
    p_stream.add_argument("run_id")
    p_stream.add_argument("text")
    p_stream.set_defaults(func=_cmd_stream)

    sub.add_parser("approvals", help="查看待审批队列").set_defaults(func=_cmd_approvals)

    p_appr = sub.add_parser("approve", help="批准某 run 的审批")
    p_appr.add_argument("run_id")
    p_appr.set_defaults(func=_cmd_approve)

    p_rej = sub.add_parser("reject", help="拒绝某 run 的审批")
    p_rej.add_argument("run_id")
    p_rej.set_defaults(func=_cmd_reject)

    sub.add_parser("health", help="健康检查").set_defaults(func=_cmd_health)
    sub.add_parser("caps", help="列出能力").set_defaults(func=_cmd_caps)

    p_coding = sub.add_parser("coding", help="本地跑一个编码需求（读代码→出diff→门禁落地）")
    p_coding.add_argument("requirement")
    p_coding.add_argument("--workdir", default=".", help="git 仓库根目录（默认当前目录）")
    p_coding.set_defaults(func=_cmd_coding)
    return p


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
