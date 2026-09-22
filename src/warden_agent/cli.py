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
  warden backup-pg --dbname X       PostgreSQL 备份（pg_dump -Fc + 产物校验）
  warden backup-prune --dir D --keep N  按保留策略清理备份（默认只演练，--yes 才真删）
  warden restore <backup> [--force] 从备份恢复（破坏性，会覆盖目标库）
  warden stuck [--older-than-min N] 列出等待人工处理超时的 run（接告警用，有则退出码 3）
  warden audit-verify               校验审计链是否完整（被动过则退出码 4）
  warden audit-export               导出审计记录（JSONL/CSV，带链字段，顺带校链）
  warden rotate-credentials         把存量凭证密文重加密到当前密钥（密钥轮换）

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
from pathlib import Path
from typing import Any

import httpx


def default_base_url(env: Mapping[str, str] | None = None) -> str:
    """CLI 要连的 Warden 服务地址。`WARDEN_SERVER_URL` 可覆盖。

    刻意**不**读 `WARDEN_BASE_URL`——那是模型端点（custom provider）；
    混用的后果是 CLI 把请求发到模型网关上去。
    """
    from warden_agent.core.settings import env_str

    return env_str("WARDEN_SERVER_URL", "http://127.0.0.1:8000", env)


DEFAULT_BASE = default_base_url()


def _db_from_args(args: argparse.Namespace) -> str:
    """解析要操作的数据库路径：`--db` 优先 → `WARDEN_DB_PATH` → 本机默认文件。

    抽出来是因为**七个命令**原本各写一遍同一个表达式（`args.db or os.environ.get(...) or ...`）——
    同一个默认值散在多处正是"改一处漏一处"的来源。
    """
    from warden_agent.core.settings import env_str

    return args.db or env_str("WARDEN_DB_PATH", "warden-agent-local.db")


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

    db = _db_from_args(args)
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

    db = _db_from_args(args)
    try:
        info = backup_sqlite(db, args.dest or None)
    except BackupError as e:
        _die(str(e))
    print(f"备份完成：{info['backup']}")
    print(f"  源库：{info['source']}")
    print(f"  大小：{info['bytes']} 字节｜校验：{info['verified']}")
    if args.keep:
        _prune_after_backup(Path(str(info["backup"])).parent, args.keep, args.dry_run)


def _prune_after_backup(directory: Path, keep: int, dry_run: bool) -> None:
    """备份后按保留策略清理旧备份（`--keep N`）。"""
    from typing import cast

    from warden_agent.runtime.backup import BackupError, prune_backups

    try:
        result = prune_backups(directory, keep, dry_run=dry_run)
    except BackupError as e:
        _die(str(e))
    tail = "（演练，未真删）" if result["dry_run"] else ""
    removed = cast("list[str]", result["removed"])
    print(f"保留策略 keep={keep}{tail}：目录内 {result['found']} 份，保留 {result['kept']}，"
          f"待删/已删 {len(removed)}")
    for path in removed:
        print(f"  - {path}")


def _cmd_backup_prune(args: argparse.Namespace) -> None:
    """按保留策略清理备份目录（默认**只演练**，加 `--yes` 才真删）。

    删除不可逆，所以默认干跑：先看清要删什么，再决定。
    """
    from typing import cast

    from warden_agent.runtime.backup import BackupError, prune_backups

    dry_run = not args.yes
    try:
        result = prune_backups(args.dir, args.keep, pattern=args.pattern, dry_run=dry_run)
    except BackupError as e:
        _die(str(e))
    removed = cast("list[str]", result["removed"])
    print(f"目录：{result['directory']}（匹配 {result['pattern']}）")
    print(f"共 {result['found']} 份，保留最近 {args.keep} 份，"
          f"{'待删' if dry_run else '已删'} {len(removed)} 份"
          + ("（演练，未真删；确认无误加 --yes）" if dry_run else ""))
    for path in removed:
        print(f"  - {path}")


def _cmd_backup_pg(args: argparse.Namespace) -> None:
    """PostgreSQL 备份（`pg_dump -Fc` + 产物校验）。

    密码走 `PGPASSWORD` 环境变量（libpq 的标准变量名），**不放命令行**——
    命令行参数会出现在 `ps` 与 shell history 里。
    """
    from warden_agent.core.settings import env_opt
    from warden_agent.runtime.backup import BackupError, backup_postgres

    params = {
        "host": args.host, "port": args.port,
        "dbname": args.dbname, "user": args.user,
        "password": env_opt("PGPASSWORD"),
    }
    try:
        info = backup_postgres(
            params, args.dest or None,
            pg_dump_bin=args.pg_dump, pg_restore_bin=args.pg_restore,
        )
    except BackupError as e:
        _die(str(e))
    print(f"备份完成：{info['backup']}")
    print(f"  源库：{info['source']}")
    print(f"  大小：{info['bytes']} 字节｜格式：{info['format']}｜校验：{info['verified']}")
    if args.keep:
        _prune_after_backup(Path(str(info["backup"])).parent, args.keep, args.dry_run)


def _cmd_restore(args: argparse.Namespace) -> None:
    """从备份恢复。目标库已存在时必须显式 --force（恢复是破坏性操作）。"""
    from warden_agent.runtime.backup import BackupError, restore_sqlite

    db = _db_from_args(args)
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

    db = _db_from_args(args)
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


def _cmd_audit_verify(args: argparse.Namespace) -> None:
    """校验审计链是否完整（防篡改的兑现方式：改了必然被发现）。

    有输出即代表**审计被人动过**（字段被改 / 中间被删 / 被重排），返回非 0 便于接巡检告警。
    """
    import json

    from warden_agent.web.audit import SqliteAuditStore

    db = _db_from_args(args)
    try:
        store = SqliteAuditStore(db_path=db)
    except Exception as e:  # 打不开库（路径不对/损坏）
        _die(f"无法打开审计库 {db}: {e}")

    ok, detail = store.verify_chain()
    if args.json:
        print(json.dumps({"ok": ok, "detail": detail, "db": db}, ensure_ascii=False))
    else:
        print(("✅ " if ok else "❌ ") + detail)
    if not ok:
        # 审计被动过是**需要人立刻看**的事件，用非 0 退出码让巡检能报警
        raise SystemExit(4)


def _safe_export_name(name: str) -> str:
    """校验导出文件名：只允许**纯文件名**（不许带目录、`..`、盘符、反斜杠）。

    导出物集中落在 `--out-dir` 指定的目录里，文件名由本函数把关，
    所以"写到哪"完全由 out-dir 决定，文件名无法把写入引到别处。
    """
    text = (name or "").strip()
    if not text:
        raise ValueError("导出文件名不能为空")
    if "\\" in text or "/" in text or ".." in text or text.startswith("."):
        raise ValueError(f"导出文件名必须是纯文件名（不含目录/..）：{name!r}")
    if len(text) > 3 and text[1] == ":":  # Windows 盘符
        raise ValueError(f"导出文件名不能带盘符：{name!r}")
    return text


def _utc_stamp() -> str:
    """UTC 时间戳（用于导出文件默认命名，如 20260922T120000Z）。"""
    import datetime as _dt

    return _dt.datetime.now(_dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def _cmd_audit_export(args: argparse.Namespace) -> None:
    """导出审计记录（JSONL / CSV），并**顺带校验链是否完整**。

    用途：归档 / 取证 / 交给审计方。导出**带上链字段**（id / prev_hash / hash），
    接收方可以拿同样的 `WARDEN_AUDIT_KEY` 独立复核这份导出有没有被动过——
    只导出内容的话，它只是一份"看起来对"的表格。

    产物落在 `--out-dir`（默认 `./audit-exports/`）下的一个文件里；文件名可用 `--name` 指定，
    只接受**纯文件名**（不带目录/`..`/盘符）——所以"写到哪"完全由 out-dir 决定。
    这是本机运维命令（直接读库、按操作者权限写盘），不是 HTTP 接口。

    退出码：0 正常；**4 = 链已断**（导出仍会写出，便于取证，但要立刻报警）；1 = 打不开库/写不出去。
    """
    import csv
    import json
    from pathlib import Path

    from warden_agent.web.audit import SqliteAuditStore

    db = _db_from_args(args)
    try:
        store = SqliteAuditStore(db_path=db)
    except Exception as e:  # 打不开库（路径不对/损坏）
        _die(f"无法打开审计库 {db}: {e}")

    chain_ok, detail = store.verify_chain()
    rows = store.export_records(after_id=args.after_id, limit=args.limit)

    suffix = "csv" if args.format == "csv" else "jsonl"
    try:
        safe_name = _safe_export_name(args.name or f"audit-{_utc_stamp()}.{suffix}")
    except ValueError as e:
        _die(str(e))
    out_dir = Path(args.out_dir).expanduser()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        _die(f"无法创建导出目录 {out_dir}: {e}")
    target_path = out_dir / safe_name

    try:
        with target_path.open("w", encoding="utf-8", newline="") as fh:
            if args.format == "csv":
                fields = [
                    "id", "at", "correlation_id", "tenant_id", "principal_type",
                    "principal_id", "product_id", "operation", "run_id", "method",
                    "path", "status", "prev_hash", "hash",
                ]
                writer = csv.DictWriter(fh, fieldnames=fields)
                writer.writeheader()
                for row in rows:
                    writer.writerow(row)
            else:  # jsonl（默认）
                for row in rows:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as e:
        _die(f"无法写入 {target_path}: {e}")

    print(f"导出 {len(rows)} 条（after_id={args.after_id}"
          + (f", limit={args.limit}" if args.limit else "")
          + f"）→ {target_path}")
    print(("✅ " if chain_ok else "❌ ") + detail)
    if not chain_ok:
        # 链断了要立刻让人知道：数据已导出，但"这份审计被动过"
        raise SystemExit(4)


def _cmd_rotate_credentials(args: argparse.Namespace) -> None:
    """把存量凭证密文从旧密钥重加密到当前密钥（轮换的第二半）。

    用法：先把新密钥配成 `WARDEN_CREDENTIAL_KEY`、旧密钥放进
    `WARDEN_CREDENTIAL_OLD_KEYS`，跑本命令完成重加密，确认无误后再摘掉旧密钥。
    不做这一步直接换密钥 = 存量凭证全部解不开。
    """
    import json

    from warden_agent.credential.broker import default_broker
    from warden_agent.credential.vault import rotate_credentials
    from warden_agent.store.sqlite import SqliteStore

    db = _db_from_args(args)
    try:
        store = SqliteStore(db)
    except Exception as e:  # 打不开库
        _die(f"无法打开凭证库 {db}: {e}")

    broker = default_broker(os.environ, vault=store)
    # 作用域：默认只处理部署级；多用户各自导入的 key 用 --scope 逐个指定（或 --all-scopes）
    scopes = [s for s in (args.scope or "").split(",") if s]
    extra = _scopes_in_store(store) if args.all_scopes else ()
    report = rotate_credentials(store, broker._cipher, scopes or ("",), extra_scopes=extra)  # noqa: SLF001

    if args.json:
        print(json.dumps(
            {"scanned": report.scanned, "rotated": report.rotated,
             "already_current": report.already_current, "failed": list(report.failed)},
            ensure_ascii=False, indent=2,
        ))
    else:
        print(report.describe())
    if not report.ok:
        # 有解不开的凭证 → 不能假装轮换成功（那些密文还是旧密钥，摘掉旧密钥就永久损失）
        raise SystemExit(5)


def _scopes_in_store(store: Any) -> list[str]:
    """列出库里出现过的所有凭证作用域（给 `--all-scopes` 用）。

    `store` 是 SqliteStore / PostgresStore，这里只读它的连接，取 distinct scope。
    取不到（表不存在 / 连接异常）就当作没有额外作用域，不因此中断轮换。
    """
    conn = getattr(store, "conn", None)
    if conn is None:
        return []
    try:
        rows = conn.execute("SELECT DISTINCT scope FROM credentials").fetchall()
    except Exception:  # noqa: BLE001 - 表不存在 / 连接异常
        return []
    return [str(r[0]) for r in rows]


def _apply_recovery(
    store: Any, cp_store: Any, controller: Any, args: argparse.Namespace
) -> None:
    """`recover --apply`：用默认装配真正执行一轮恢复，并打印每个 run 的处置。"""
    from warden_agent.agent import build_agent
    from warden_agent.core.settings import env_bool
    from warden_agent.runtime.locking import run_lock_for
    from warden_agent.runtime.worker import RecoveryWorker

    # Run 级锁：多副本下同一个 run 可能同时出现在两边的恢复计划里，没有闸门就会
    # 两边一起写、后写覆盖前写。开了 WARDEN_SHARED_STATE 就用存储里的共享锁。
    lock = run_lock_for(store, shared=env_bool("WARDEN_SHARED_STATE", False))
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
    p_backup.add_argument("--keep", type=int, default=0, help="备份后只保留最近 N 份（0 = 不清）")
    p_backup.add_argument("--dry-run", action="store_true", help="保留策略只演练、不真删")
    p_backup.set_defaults(func=_cmd_backup)

    p_restore = sub.add_parser("restore", help="从备份恢复（破坏性：会覆盖目标库）")
    p_restore.add_argument("backup", help="备份文件路径")
    p_restore.add_argument("--db", default="", help="目标库路径（默认取 WARDEN_DB_PATH）")
    p_restore.add_argument("--force", action="store_true", help="目标库已存在时确认覆盖")
    p_restore.set_defaults(func=_cmd_restore)

    p_bpg = sub.add_parser("backup-pg", help="PostgreSQL 备份（pg_dump -Fc + 产物校验）")
    p_bpg.add_argument("--host", default="localhost", help="数据库主机（默认 localhost）")
    p_bpg.add_argument("--port", default="5432", help="端口（默认 5432）")
    p_bpg.add_argument("--dbname", required=True, help="库名")
    p_bpg.add_argument("--user", default="", help="用户名")
    p_bpg.add_argument("--dest", default="", help="备份文件路径（默认按时间自动命名）")
    p_bpg.add_argument("--pg-dump", default="pg_dump", help="pg_dump 可执行文件（默认取 PATH）")
    p_bpg.add_argument("--pg-restore", default="pg_restore", help="pg_restore（用于校验产物）")
    p_bpg.add_argument("--keep", type=int, default=0, help="备份后只保留最近 N 份（0 = 不清）")
    p_bpg.add_argument("--dry-run", action="store_true", help="保留策略只演练、不真删")
    p_bpg.set_defaults(func=_cmd_backup_pg)

    p_prune = sub.add_parser(
        "backup-prune", help="按保留策略清理备份目录（默认只演练，--yes 才真删）"
    )
    p_prune.add_argument("--dir", required=True, help="备份目录")
    p_prune.add_argument("--keep", type=int, required=True, help="保留最近 N 份")
    p_prune.add_argument("--pattern", default="*.backup-*", help="匹配的备份文件名模式")
    p_prune.add_argument("--yes", action="store_true", help="确认真删（不加则只演练）")
    p_prune.set_defaults(func=_cmd_backup_prune)

    p_stuck = sub.add_parser("stuck", help="列出等待人工处理超时的 run（接告警用）")
    p_stuck.add_argument(
        "--older-than-min", type=float, default=60.0, help="超过多少分钟算超时（默认 60）"
    )
    p_stuck.add_argument("--owner", default="", help="只看某个用户的 run（默认全部）")
    p_stuck.add_argument("--db", default="", help="存档库路径（默认取 WARDEN_DB_PATH）")
    p_stuck.add_argument("--json", action="store_true", help="以 JSON 输出")
    p_stuck.set_defaults(func=_cmd_stuck)

    p_audit = sub.add_parser("audit-verify", help="校验审计链是否完整（防篡改巡检）")
    p_audit.add_argument("--db", default="", help="审计库路径（默认取 WARDEN_DB_PATH）")
    p_audit.add_argument("--json", action="store_true", help="以 JSON 输出")
    p_audit.set_defaults(func=_cmd_audit_verify)

    p_export = sub.add_parser(
        "audit-export", help="导出审计记录（JSONL/CSV，顺带校验链是否完整）"
    )
    p_export.add_argument("--db", default="", help="审计库路径（默认取 WARDEN_DB_PATH）")
    p_export.add_argument(
        "--out-dir", default="audit-exports", help="导出目录（默认 ./audit-exports）"
    )
    p_export.add_argument("--name", default="", help="导出文件名（纯文件名，默认按时间自动命名）")
    p_export.add_argument("--format", choices=["jsonl", "csv"], default="jsonl", help="导出格式")
    p_export.add_argument(
        "--after-id", type=int, default=0, help="只导出 id 大于该值的记录（增量导出）"
    )
    p_export.add_argument("--limit", type=int, default=0, help="最多导出多少条（0 = 全部）")
    p_export.set_defaults(func=_cmd_audit_export)

    p_rotate = sub.add_parser(
        "rotate-credentials", help="把存量凭证密文重加密到当前密钥（密钥轮换的第二半）"
    )
    p_rotate.add_argument("--db", default="", help="凭证库路径（默认取 WARDEN_DB_PATH）")
    p_rotate.add_argument("--scope", default="", help="只处理这些作用域（逗号分隔，默认部署级）")
    p_rotate.add_argument("--all-scopes", action="store_true", help="处理库里出现过的全部作用域")
    p_rotate.add_argument("--json", action="store_true", help="以 JSON 输出")
    p_rotate.set_defaults(func=_cmd_rotate_credentials)
    return p


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
