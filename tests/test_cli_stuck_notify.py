"""`warden stuck --notify` 的投递口径：**投递失败必须说出来，退出码必须能区分**。

为什么要有这个文件（第五十六批）：
CLI 曾经在**一条告警都没送出去**时照样打印「已投递告警：N 个 Run（通道 X）」——
两次都能复现：

- 未配通道：`NullNotifier.send()` 恒 `False` → 输出「已投递告警：1 个 Run（通道 NullNotifier）」；
- webhook 连不上：`httpx.ConnectError: [WinError 10061] 目标计算机积极拒绝` →
  输出「已投递告警：1 个 Run（通道 WebhookNotifier）」。

两次退出码都是 3（=「有人等着处理」），于是 cron/监控**无法区分**「叫醒成功了」与
「一条都没出去」——值班的人以为有兜底，实际没有任何人被叫醒。

本文件真跑 `cli.main([...])`（在进程内），断言 stdout / stderr / 退出码三件事，
并且带**反向控制**（投递成功必须说「已投递」、退出码 3），避免把「永远说未投递」当成修好了。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from warden_agent import cli
from warden_agent.core.run.status import AgentRun, RunStatus
from warden_agent.runtime import notify as notify_mod
from warden_agent.runtime.checkpoint import Checkpoint, checkpoint_store_for
from warden_agent.store.sqlite import SqliteStore


class _FakeNotifier:
    """可控通道：`ok=True` 模拟投递成功，`ok=False` 模拟 webhook 返 500 / 连不上。"""

    def __init__(self, ok: bool) -> None:
        self.ok = ok
        self.calls: list[dict[str, object]] = []

    def send(self, *, title: str, summary: str, details: dict[str, object]) -> bool:
        self.calls.append({"title": title, "summary": summary, "details": details})
        return self.ok


def _stuck_db(tmp_path: Path, *, stuck: bool = True) -> str:
    """造一个真 SQLite 存档库；`stuck=True` 时里面有一个等审批超时的 Run。"""
    path = str(tmp_path / "stuck.db")
    store = SqliteStore(path)
    store.save_run(AgentRun("run-stuck"))
    cps = checkpoint_store_for(store)
    assert cps is not None
    if stuck:
        cps.save(
            Checkpoint(
                run_id="run-stuck",
                status=RunStatus.WAITING_APPROVAL,
                iteration=1,
                step="awaiting_approval",
            )
        )
    return path


def test_未配通道时不说已投递_退出码6(tmp_path, capsys, monkeypatch) -> None:
    """未配 `WARDEN_ALERT_WEBHOOK_URL`：空实现不发请求 → 必须说「未投递」+ 退出码 6。"""
    monkeypatch.delenv("WARDEN_ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("WARDEN_ALERT_WEBHOOK_HEADERS", raising=False)
    db = _stuck_db(tmp_path)

    with pytest.raises(SystemExit) as ei:
        cli.main(["stuck", "--db", db, "--older-than-min", "0", "--notify"])

    out, err = capsys.readouterr()
    assert ei.value.code == 6, "投递未发生却报成功，退出码必须是 6（与「有人等着处理」的 3 区分开）"
    assert "已投递告警" not in out, f"一条都没送出去就不能说已投递：{out!r}"
    assert "告警未投递" in out
    assert "NullNotifier" in out
    assert "WARDEN_ALERT_WEBHOOK_URL" in err, "stderr 要说清是没配置通道"


def test_通道投递失败时不说已投递_退出码6(tmp_path, capsys, monkeypatch) -> None:
    """通道存在但投递失败（返 500 / 连不上）：同样不许说「已投递」。"""
    broken = _FakeNotifier(ok=False)
    monkeypatch.setattr(notify_mod, "notifier_from_env", lambda env=None: broken)
    db = _stuck_db(tmp_path)

    with pytest.raises(SystemExit) as ei:
        cli.main(["stuck", "--db", db, "--older-than-min", "0", "--notify"])

    out, err = capsys.readouterr()
    assert ei.value.code == 6
    assert "已投递告警" not in out
    assert "告警未投递" in out and "_FakeNotifier" in out
    assert "投递失败" in err
    assert len(broken.calls) == 1, "失败也必须真的尝试过投递（不是没试就说失败）"


def test_投递成功才说已投递_退出码3(tmp_path, capsys, monkeypatch) -> None:
    """**反向控制**：真的投递成功时，必须说「已投递」且退出码回到 3。"""
    good = _FakeNotifier(ok=True)
    monkeypatch.setattr(notify_mod, "notifier_from_env", lambda env=None: good)
    db = _stuck_db(tmp_path)

    with pytest.raises(SystemExit) as ei:
        cli.main(["stuck", "--db", db, "--older-than-min", "0", "--notify"])

    out, err = capsys.readouterr()
    assert ei.value.code == 3, "投递成功时退出码仍是 3（有人等着处理）"
    assert "已投递告警：1 个 Run" in out and "_FakeNotifier" in out
    assert "告警未投递" not in out and "告警未投递" not in err
    assert good.calls and good.calls[0]["title"].startswith("[warden] 1 个 Run")


def test_json输出时投递成败都不污染stdout(tmp_path, capsys, monkeypatch) -> None:
    """`--json` 消费者拿到的必须是**合法 JSON**：投递成败的人话一律走 stderr。

    （上一版修法只把「原因」写 stderr，仍往 stdout 打一行 ⚠️——`json.loads` 直接报
    `Expecting value: line 1 column 1`。两种输出口径打架，正是本批要消掉的那类 bug。）
    """
    monkeypatch.delenv("WARDEN_ALERT_WEBHOOK_URL", raising=False)
    db = _stuck_db(tmp_path)

    with pytest.raises(SystemExit) as ei:
        cli.main(["stuck", "--db", db, "--older-than-min", "0", "--json", "--notify"])

    out, err = capsys.readouterr()
    assert ei.value.code == 6
    payload = json.loads(out)  # 必须是纯 JSON
    assert isinstance(payload, list) and payload[0]["run_id"] == "run-stuck"
    assert "告警未投递" in err and "NullNotifier" in err


def test_json输出时投递成功也只说在stderr(tmp_path, capsys, monkeypatch) -> None:
    """反向控制：投递成功时 stdout 同样是纯 JSON。"""
    good = _FakeNotifier(ok=True)
    monkeypatch.setattr(notify_mod, "notifier_from_env", lambda env=None: good)
    db = _stuck_db(tmp_path)

    with pytest.raises(SystemExit) as ei:
        cli.main(["stuck", "--db", db, "--older-than-min", "0", "--json", "--notify"])

    out, err = capsys.readouterr()
    assert ei.value.code == 3
    assert isinstance(json.loads(out), list)
    assert "已投递告警：1 个 Run" in err and "_FakeNotifier" in err
    assert "已投递告警" not in out


def test_没有卡死的run时不提投递_退出码0(tmp_path, capsys, monkeypatch) -> None:
    """没有命中：既不投递、也不提“投递”两个字，退出码 0（cron 判“没事”）。"""
    monkeypatch.setenv("WARDEN_ALERT_WEBHOOK_URL", "https://hook.example/x")
    db = _stuck_db(tmp_path, stuck=False)

    cli.main(["stuck", "--db", db, "--older-than-min", "0", "--notify"])  # 不抛 SystemExit

    out, err = capsys.readouterr()
    assert "已投递告警" not in out and "告警未投递" not in out
    assert "已投递告警" not in err and "告警未投递" not in err
