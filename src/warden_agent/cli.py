"""warden —— 命令行入口：连接运行中的 Warden Agent HTTP 服务。

提供：
  warden chat <run_id> "<问题>"     送一句话给 Agent（POST /chat/{run_id}）
  warden chat-stream <run_id> "..."  流式对话（POST /chat/stream/{run_id}）
  warden approvals                  查看待审批队列（GET /approvals）
  warden approve <run_id>           批准该会话的审批（POST /approve/{run_id}）
  warden reject <run_id>            拒绝（POST /reject/{run_id}）
  warden health                     健康检查（GET /health/live + /health/ready）
  warden caps                       列出能力（GET /capabilities）
  warden coding "<需求>"             本地编码任务（读代码 → 出 diff → 门禁落地）
  warden recover                    本地读存档点，输出跨 Run 恢复计划（只判断不执行）
  warden backup [dest]              做一份一致性备份（SQLite 在线备份 API）
  warden restore <backup> [--force] 从备份恢复（破坏性，会覆盖目标库）
  warden stuck [--older-than-min N] 列出等待人工处理超时的 run（接告警用，有则退出码 3）

默认连 http://127.0.0.1:8000；可用环境变量 `WARDEN_SERVER_URL` 覆盖要连的**服务地址**。
需要先启动服务：  py -m warden_agent.web.run_server

⚠️ 为什么这里用 `WARDEN_SERVER_URL` 而不是 `WARDEN_BASE_URL`：
  后者已经被 **custom 模型**占用（`model/deepseek.py` 拿它当 OpenAI 兼容端点）。
  两者曾经同名 → 配了自建模型网关之后，`warden chat` 会把请求发到那个**模型网关**上去。
  现在彻底分开：`WARDEN_SERVER_URL` = CLI 要连的 Warden 服务；`WARDEN_BASE_URL` = 模型端点。
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from typing import Any

import httpx


def default_base_url(env: Mapping[str, str] | None = None) -> str:
    """CLI 要连的 Warden 服务地址。`WARDEN_SERVER_URL` 可覆盖。

    刻意**不**读 `WARDEN_BASE_URL`——那是模型端点（custom provider）；
    混用的后果是 CLI 把请求发到模型网关上去。
    """
    src: Mapping[str, str] = env if env is not None else os.environ
    return src.get("WARDEN_SERVER_URL") or "http://127.0.0.1:8000"


DEFAULT_BASE = default_base_url()


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


def _cmd_recover(args: argparse.Namespace) -> None:
    """读取存档点，输出跨 Run 恢复计划；`--apply` 则真正执行一轮恢复。

    默认**只判断不执行**（打印计划）。加 `--apply` 会用 `RecoveryWorker` 真正续跑：
    该续的续、该重试的重试（超上限不再试）、等人工的不碰、终态的跳过。

    ⚠️ 真跑起来需要与原会话一致的模型/工具/策略装配（`build_agent(...)` 的参数）。
    这里用默认装配（离线假模型、无工具），够验证链路与离线场景；生产请在自己的
    工作进程里用同一套装配构造 `RecoveryWorker`（见 `runtime/worker.py`）。
    """
    import json

    from warden_agent.runtime.checkpoint import checkpoint_store_for
    from warden_agent.runtime.recovery import RecoveryController
    from warden_agent.store.sqlite import SqliteStore

    db = args.db or os.environ.get("WARDEN_DB_PATH") or "warden-agent-local.db"
    try:
        store = SqliteStore(db)
    except Exception as e:  # 打不开库（路径不对/损坏）
        _die(f"无法打开存档库 {db}: {e}")
    cp_store = checkpoint_store_for(store)
    assert cp_store is not None  # SqliteStore 一定支持 checkpoint
    controller = RecoveryController(cp_store)

    if args.apply:
        _apply_recovery(store, cp_store, controller, args)
        return

    plan = controller.plan()

    owner = args.owner
    def _visible(run_id: str) -> bool:
        if not owner:
            return True
        run = store.load_run(run_id)
        return run is not None and run.user_id == owner

    groups = {
        "该续跑 (resume)": plan.to_resume,
        "该重试 (retry)": plan.to_retry,
        "等待人工 (await_human)": plan.awaiting_human,
        "已终态 (skip)": plan.terminal,
    }
    if args.json:
        payload = {
            "db": db,
            "decisions": {k: v for k, v in plan.decisions.items() if _visible(k)},
            "to_resume": [c.to_dict() for c in plan.to_resume if _visible(c.run_id)],
            "to_retry": [c.to_dict() for c in plan.to_retry if _visible(c.run_id)],
            "awaiting_human": [c.to_dict() for c in plan.awaiting_human if _visible(c.run_id)],
            "terminal": [c.to_dict() for c in plan.terminal if _visible(c.run_id)],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    total = sum(1 for k in plan.decisions if _visible(k))
    if total == 0:
        print(f"存档库 {db} 里没有可恢复的 run（可能从未运行过，或已全部处理）。")
        return
    print(f"存档库: {db}")
    for title, cps in groups.items():
        rows = [c for c in cps if _visible(c.run_id)]
        if not rows:
            continue
        print(f"\n{title}  ({len(rows)})")
        for cp in rows:
            print(
                f"  {cp.run_id:<28} 状态={cp.status.name:<16} "
                f"迭代={cp.iteration} 步骤={cp.step} 尝试={cp.attempts}"
            )
    print("\n（以上只是计划；真正续跑请加 --apply）")


def _cmd_backup(args: argparse.Namespace) -> None:
    """做一份一致性备份（SQLite 在线备份 API，不需要停服务）。"""
    from warden_agent.runtime.backup import BackupError, backup_sqlite

    db = args.db or os.environ.get("WARDEN_DB_PATH") or "warden-agent-local.db"
    try:
        info = backup_sqlite(db, args.dest or None)
    except BackupError as e:
        _die(str(e))
    print(f"备份完成：{info['backup']}")
    print(f"  源库：{info['source']}")
    print(f"  大小：{info['bytes']} 字节｜校验：{info['verified']}")


def _cmd_restore(args: argparse.Namespace) -> None:
    """从备份恢复。目标库已存在时必须显式 --force（恢复是破坏性操作）。"""
    from warden_agent.runtime.backup import BackupError, restore_sqlite

    db = args.db or os.environ.get("WARDEN_DB_PATH") or "warden-agent-local.db"
    try:
        info = restore_sqlite(args.backup, db, overwrite=args.force)
    except BackupError as e:
        _die(str(e))
    print(f"恢复完成：{info['backup']} → {info['restored_to']}")
    print(f"  大小：{info['bytes']} 字节｜校验：{info['verified']}")
    print("  ⚠️ 请确认该库当前没有别的进程在用（服务应已停止）")


def _cmd_stuck(args: argparse.Namespace) -> None:
    """列出"等待人工处理超时"的 run —— 可直接接 cron 告警（有输出就该有人看）。"""
    import json

    from warden_agent.runtime.alerting import describe_stuck, stuck_awaiting_human
    from warden_agent.runtime.checkpoint import checkpoint_store_for
    from warden_agent.store.sqlite import SqliteStore

    db = args.db or os.environ.get("WARDEN_DB_PATH") or "warden-agent-local.db"
    try:
        store = SqliteStore(db)
    except Exception as e:  # 打不开库
        _die(f"无法打开存档库 {db}: {e}")
    cp_store = checkpoint_store_for(store)
    assert cp_store is not None

    runs = stuck_awaiting_human(
        store, cp_store,
        older_than_seconds=args.older_than_min * 60.0,
        owner=args.owner or None,
    )
    if args.json:
        print(json.dumps(
            [{"run_id": r.run_id, "status": r.status,
              "waiting_seconds": r.waiting_seconds, "detail": r.detail} for r in runs],
            ensure_ascii=False, indent=2,
        ))
    else:
        print(describe_stuck(runs))
    # 有需要人工处理的就返回非 0，便于 cron/监控用退出码判断
    if runs:
        raise SystemExit(3)


def _apply_recovery(
    store: Any, cp_store: Any, controller: Any, args: argparse.Namespace
) -> None:
    """`recover --apply`：用默认装配真正执行一轮恢复，并打印每个 run 的处置。"""
    from warden_agent.agent import build_agent
    from warden_agent.core.settings import env_flag
    from warden_agent.runtime.locking import run_lock_for
    from warden_agent.runtime.worker import RecoveryWorker

    # Run 级锁：多副本下同一个 run 可能同时出现在两边的恢复计划里，没有闸门就会
    # 两边一起写、后写覆盖前写。开了 WARDEN_SHARED_STATE 就用存储里的共享锁。
    lock = run_lock_for(store, shared=env_flag(os.environ.get("WARDEN_SHARED_STATE")))
    agent = build_agent(store=store)
    worker = RecoveryWorker(controller, agent.session_factory, lock=lock)
    actions = worker.run_once()
    if not actions:
        print("没有需要处理的 run。")
        return
    print(f"执行一轮恢复，共 {len(actions)} 个 run：")
    for a in actions:
        detail = f"  {a.detail}" if a.detail else ""
        print(f"  {a.run_id:<28} {a.action}{detail}")


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

    p_recover = sub.add_parser("recover", help="跨 Run 恢复计划（读存档点，只判断不执行）")
    p_recover.add_argument(
        "--db", default="", help="存档库路径（默认取 WARDEN_DB_PATH，再退到本地默认库）"
    )
    p_recover.add_argument("--owner", default="", help="只看某个用户的 run（默认全部）")
    p_recover.add_argument("--json", action="store_true", help="以 JSON 输出（便于脚本消费）")
    p_recover.add_argument(
        "--apply", action="store_true",
        help="真正执行一轮恢复（默认只打印计划；用默认模型/工具装配，见命令文档）",
    )
    p_recover.set_defaults(func=_cmd_recover)

    p_backup = sub.add_parser("backup", help="做一份一致性备份（不需要停服务）")
    p_backup.add_argument(
        "dest", nargs="?", default="", help="备份文件路径（默认带时间戳自动命名）"
    )
    p_backup.add_argument("--db", default="", help="源库路径（默认取 WARDEN_DB_PATH）")
    p_backup.set_defaults(func=_cmd_backup)

    p_restore = sub.add_parser("restore", help="从备份恢复（破坏性：会覆盖目标库）")
    p_restore.add_argument("backup", help="备份文件路径")
    p_restore.add_argument("--db", default="", help="目标库路径（默认取 WARDEN_DB_PATH）")
    p_restore.add_argument("--force", action="store_true", help="目标库已存在时确认覆盖")
    p_restore.set_defaults(func=_cmd_restore)

    p_stuck = sub.add_parser("stuck", help="列出等待人工处理超时的 run（接告警用）")
    p_stuck.add_argument(
        "--older-than-min", type=float, default=60.0, help="超过多少分钟算超时（默认 60）"
    )
    p_stuck.add_argument("--owner", default="", help="只看某个用户的 run（默认全部）")
    p_stuck.add_argument("--db", default="", help="存档库路径（默认取 WARDEN_DB_PATH）")
    p_stuck.add_argument("--json", action="store_true", help="以 JSON 输出")
    p_stuck.set_defaults(func=_cmd_stuck)
    return p


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
