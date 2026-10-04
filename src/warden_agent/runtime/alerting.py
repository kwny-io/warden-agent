"""告警口径：把"需要人管、但没人管"的状态变成可查询、可接监控的东西。

为什么需要（这是运维面上最具体的一个洞）：
  一个 Run 进入 `WAITING_APPROVAL` / `WAITING_INTERACTION` 之后，
  **在等到人处理之前不会有任何动静**——
  没有超时、没有重试、没有任何通知。线上没人盯，它就一直挂着；等发现时可能已经挂了几小时。
  项目此前对这件事只有"`/recovery/plan` 里能看到 `awaiting_human`"——能看见，但没有任何
  "挂太久了"的判断，也就没法接告警。

这里提供的就是那个判断：**等人工超过阈值还没人处理的 Run**。它能被 cron 直接消费
（`warden stuck --older-than-min 60`，有输出就告警），也能走 HTTP 给监控系统抓。

**口径上的诚实说明（很重要）**：
  "等了多久"是用 Run 的 `updated_at`（最后一次活动的时刻）近似的，不是"进入等待那一刻"。
  差别在于：如果这个 Run 在等待期间被别的操作碰过（例如又查了一次状态），
  `updated_at` 会被刷新，于是"等待时长"被低估。要做到严格精确，需要在进入等待时
  单独记一个时间戳（`pending_approvals` 加一列）。**当前实现是保守的**：只会低估、不会虚报，
  所以"它报了警"这件事是可信的；但"它没报警"不等于一定没挂太久。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

DEFAULT_STUCK_SECONDS = 3600.0

# 单条告警 / 一次输出里**最多列几个 Run**（唯一事实源：CLI、告警投递、HTTP 都从这里取）。
#
# 为什么必须有上界（第五十七批**实测**，不是拍脑袋）：消息体积随命中数线性增长——
#   1 个 = 218 字节文本 / 514 字节 body；100 个 = 15KB / 31KB；
#   1000 个 = 108KB / 267KB；5000 个 = 496KB / 1.29MB。
# 而接收端普遍有上限：nginx 默认 `client_max_body_size` = 1MB（超了直接 413），
# 企业微信/钉钉群机器人的 text 上限只有 2000~4096 字节。
# 群体性事故（一个审批环节卡住 → 成百上千个 Run 排队等人工）正是**最需要告警的时刻**，
# 那一刻消息最大、最可能被拒绝或截断；投递失败只记一条 warning，于是“没有告警”看起来像“没事”。
# 20 行 × ~100 字节 ≈ 2KB，落在群机器人上限内，同时按等待时长倒序保留了最该先看的那些；
# 被省略的个数**写在消息里**（不能悄悄少给——同第五十二批“静默少跑 40 条”那类错）。
ALERT_LIST_LIMIT = 20


@dataclass(frozen=True)
class StuckRun:
    """一个"在等人、且等太久了"的 Run。"""

    run_id: str
    status: str
    waiting_seconds: float | None  # None = 拿不到时间戳（当成"未知"，保守处理）
    detail: str = ""
    # 时长算自哪个来源（口径要透明，见模块文档的"诚实说明"）：
    #   approval      —— 用"进入等待审批的时刻"，精确
    #   last_activity —— 用 Run 的最后活动时间近似，只会低估
    #   unknown       —— 两个都拿不到
    source: str = "unknown"


def _age_seconds(raw: object, now: _dt.datetime) -> float | None:
    """把 `updated_at`（ISO 字符串）换算成"距今多少秒"。拿不到就返回 None。"""
    if not raw:
        return None
    try:
        moment = _dt.datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if moment.tzinfo is None:               # 老数据可能没带时区，按 UTC 解释
        moment = moment.replace(tzinfo=_dt.UTC)
    return max(0.0, (now - moment).total_seconds())


def stuck_awaiting_human(
    store: object,
    checkpoints: object,
    *,
    older_than_seconds: float = DEFAULT_STUCK_SECONDS,
    owner: str | None = None,
    limit: int = 200,
    now: _dt.datetime | None = None,
) -> list[StuckRun]:
    """找出等待人工处理超过 `older_than_seconds` 的 Run（按等得最久的排前面）。

    `store` 需提供 `list_runs`（拿 updated_at）；`checkpoints` 是 CheckpointStore
    （给 RecoveryController 用来分类状态）。

    时长**优先**用 `pending_approval_created_at`（精确：进入等待那一刻），拿不到才退回
    Run 的最后活动时间（近似：只低估）。`StuckRun.source` 说明用的是哪个，
    调用方可以据此决定要不要提示"实际可能更久"。
    """
    from warden_agent.runtime.recovery import RecoveryController

    moment = now or _dt.datetime.now(_dt.UTC)
    controller = RecoveryController(checkpoints)  # type: ignore[arg-type]
    awaiting = controller.plan().awaiting_human

    # run_id -> updated_at（一次查完，避免逐个 load_run）
    ages: dict[str, float | None] = {}
    listed = store.list_runs(limit=max(limit, 200))  # type: ignore[attr-defined]
    for row in listed:
        ages[str(row["run_id"])] = _age_seconds(row.get("updated_at"), moment)

    # 存储支持"待审批时刻"就用它；内存版 / 老库没有这个方法 → 自动退回近似值
    approval_time = getattr(store, "pending_approval_created_at", None)

    out: list[StuckRun] = []
    for cp in awaiting:
        if owner is not None:
            run = store.load_run(cp.run_id)  # type: ignore[attr-defined]
            if run is None or run.user_id != owner:
                continue

        age: float | None = None
        source = "unknown"
        if callable(approval_time):
            age = _age_seconds(approval_time(cp.run_id), moment)
            if age is not None:
                source = "approval"
        if age is None:
            approximate = ages.get(cp.run_id)
            if approximate is not None:
                age, source = approximate, "last_activity"

        if age is None:
            # 拿不到时间戳：保守地**报出来**（标为未知），而不是默默漏掉——
            # 漏报一个可能挂了很久的 Run，比多报一个"未知"更糟。
            out.append(
                StuckRun(cp.run_id, cp.status.name, None, "拿不到等待时长", "unknown")
            )
            continue
        if age >= older_than_seconds:
            out.append(StuckRun(cp.run_id, cp.status.name, age, cp.step or "", source))

    out.sort(key=lambda s: (s.waiting_seconds is None, -(s.waiting_seconds or 0.0)))
    return out


def describe_stuck(runs: list[StuckRun], *, limit: int | None = ALERT_LIST_LIMIT) -> str:
    """把告警结果渲染成一行行文本（CLI 用；有输出即代表“需要人管”）。

    `limit`：最多列几个（`None` = 不限）。**头一行始终是真实总数**，
    被省略的个数写在末尾——「少给了」必须说出来，不能让人以为就这么多（第五十七批）。
    不设上界时，5000 个 Run 会渲染出 496KB 文本：既作为告警发不出去，也不适合当 cron 邮件。
    """
    if not runs:
        return "没有等待人工超时的 run。"
    shown = runs if limit is None else runs[: max(0, limit)]
    lines = [f"等待人工处理超时，共 {len(runs)} 个（越靠前等得越久）："]
    for item in shown:
        if item.waiting_seconds is None:
            waited = "等待时长未知"
        else:
            hours = item.waiting_seconds / 3600
            waited = f"已等 {hours:.1f} 小时"
            if item.source == "last_activity":
                waited += "（近似：按最后活动时间算，实际可能更久）"
        step = f"｜{item.detail}" if item.detail else ""
        lines.append(f"  {item.run_id:<28} {item.status:<20} {waited}{step}")
    if len(shown) < len(runs):
        lines.append(
            f"  ……另有 {len(runs) - len(shown)} 个未列出（完整清单：`warden stuck --all`，"
            f"或 `GET /alerts/stuck?limit=0`）"
        )
    return "\n".join(lines)
