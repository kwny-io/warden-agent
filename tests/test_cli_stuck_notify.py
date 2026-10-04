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

import argparse
import json
import re
from pathlib import Path

import pytest

from warden_agent import cli
from warden_agent.core.run.status import AgentRun, RunStatus
from warden_agent.runtime import notify as notify_mod
from warden_agent.runtime.alerting import ALERT_LIST_LIMIT
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


def test_命中很多时输出有上界_且首行是真实总数(tmp_path, capsys) -> None:
    """100 个卡死的 Run：默认只列最该先看的 ALERT_LIST_LIMIT 个。

    首行必须是真实总数（100），末尾必须写出省略多少个——「少给了」得能看出来。
    """
    db = _many_stuck_db(tmp_path, 100)

    with pytest.raises(SystemExit) as ei:
        cli.main(["stuck", "--db", db, "--older-than-min", "0"])

    out, _ = capsys.readouterr()
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert ei.value.code == 3
    assert lines[0].startswith("等待人工处理超时，共 100 个")
    assert len(lines) <= ALERT_LIST_LIMIT + 2, f"输出没上界：{len(lines)} 行"
    assert "另有 80 个未列出" in lines[-1]
    assert len(out.encode("utf-8")) < 4096, "cron 邮件/群机器人也要能承住"


def test_all才是全量_反向控制(tmp_path, capsys) -> None:
    """`--all` 是拿到完整清单的唯一方式：100 行，不许出现「未列出」。"""
    db = _many_stuck_db(tmp_path, 100)

    with pytest.raises(SystemExit):
        cli.main(["stuck", "--db", db, "--older-than-min", "0", "--all"])

    out, _ = capsys.readouterr()
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 101, f"--all 应列出全部：{len(lines)} 行"
    assert "未列出" not in out


def test_limit参数能真的指定条数(tmp_path, capsys) -> None:
    """`--limit N` 必须真的存在且能指定条数。

    第五十七批自己踩的坑：CHANGELOG 与 `docs/operations.md` 都写了 `--limit N`，
    而 argparse 里从来没有这个参数——`warden stuck --limit 3` 直接
    `unrecognized arguments`（退出码 2）。逃逸口本身也得有守卫。
    """
    db = _many_stuck_db(tmp_path, 100)

    with pytest.raises(SystemExit):
        cli.main(["stuck", "--db", db, "--older-than-min", "0", "--limit", "3"])

    out, _ = capsys.readouterr()
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines[0].startswith("等待人工处理超时，共 100 个"), lines[0]
    assert len(lines) == 5, f"--limit 3 应只列 3 条（+首行+末行）：{len(lines)} 行"
    assert "另有 97 个未列出" in lines[-1]


def test_limit_0_与_all_等价(tmp_path, capsys) -> None:
    """`--limit 0` = 不设上限（与 `--all` 同义）：101 行，不许出现「未列出」。"""
    db = _many_stuck_db(tmp_path, 100)

    with pytest.raises(SystemExit):
        cli.main(["stuck", "--db", db, "--older-than-min", "0", "--limit", "0"])

    out, _ = capsys.readouterr()
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 101, f"--limit 0 应给全量：{len(lines)} 行"
    assert "未列出" not in out


def test_limit与json一起用时省略个数走stderr(tmp_path, capsys) -> None:
    """`--json --limit 3`：stdout 仍是纯 JSON 且只 3 条，省略个数走 stderr。"""
    db = _many_stuck_db(tmp_path, 100)

    with pytest.raises(SystemExit):
        cli.main(["stuck", "--db", db, "--older-than-min", "0", "--json", "--limit", "3"])

    out, err = capsys.readouterr()
    assert len(json.loads(out)) == 3
    assert "只列出了 3 个（共 100 个）" in err
    assert "只列出了" not in out


def test_文档里写的_stuck_参数必须真的存在() -> None:
    """文档与 CLI 的**表面**必须同源——守卫要钉在 argparse 上，不能钉在文档上。

    （第五十七批：文档写了 `--limit N`，而 CLI 没这个参数，退出码 2。
    这条守卫就是那次遗漏的补丁：`docs/operations.md` 里跟 `stuck` 同一行出现的
    每个 `--flag` 都必须在 `stuck` 子命令的 option_strings 里。）
    """
    parser = cli._build_parser()  # noqa: SLF001
    sub = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)  # noqa: SLF001
    )
    stuck = sub.choices["stuck"]
    opts = {s for a in stuck._actions for s in a.option_strings}  # noqa: SLF001
    assert {"--all", "--limit", "--json", "--notify", "--older-than-min"} <= opts

    doc = (Path(__file__).resolve().parents[1] / "docs" / "operations.md").read_text(
        encoding="utf-8"
    )
    mentioned: set[str] = set()
    for line in doc.splitlines():
        if "stuck" in line:
            mentioned |= set(re.findall(r"--[a-z][a-z0-9-]+", line))
    missing = sorted(m for m in mentioned if m not in opts)
    assert not missing, f"docs/operations.md 提到的 stuck 参数在 CLI 里不存在：{missing}"


def test_json也有上界_且省略个数走stderr(tmp_path, capsys) -> None:
    """`--json` 的形状仍是列表（不打破消费方），但同样有上界；少给了走 stderr。"""
    db = _many_stuck_db(tmp_path, 100)

    with pytest.raises(SystemExit):
        cli.main(["stuck", "--db", db, "--older-than-min", "0", "--json"])

    out, err = capsys.readouterr()
    payload = json.loads(out)
    assert len(payload) == ALERT_LIST_LIMIT
    assert "只列出了 20 个（共 100 个）" in err
    assert "只列出了" not in out, "--json 的 stdout 必须仍是纯 JSON"


def test_命中很多时投递的载荷也不跟着长(tmp_path, monkeypatch) -> None:
    """真投递口径：100 个 Run 时，发出去的 body 有上界，但 count 是真实总数。"""
    captured: dict[str, object] = {}

    class _Capture:
        requires_network = True

        def send(self, *, title: str, summary: str, details: dict[str, object]) -> bool:
            captured.update({"title": title, "summary": summary, "details": details})
            return True

    fake = _Capture()
    monkeypatch.setattr(notify_mod, "notifier_from_env", lambda env=None: fake)
    db = _many_stuck_db(tmp_path, 100)

    with pytest.raises(SystemExit) as ei:
        cli.main(["stuck", "--db", db, "--older-than-min", "0", "--notify"])

    assert ei.value.code == 3
    details = captured["details"]
    assert (details["count"], details["listed"], details["omitted"]) == (100, 20, 80)
    assert captured["title"].startswith("[warden] 100 个 Run")
    assert "另有 80 个未列出" in captured["summary"]


def _many_stuck_db(tmp_path: Path, n: int) -> str:
    """造 n 个卡在等审批的 Run（真 SQLite）。"""
    path = str(tmp_path / f"many-{n}.db")
    store = SqliteStore(path)
    cps = checkpoint_store_for(store)
    assert cps is not None
    for i in range(n):
        rid = f"run-{i:04d}"
        store.save_run(AgentRun(rid))
        cps.save(
            Checkpoint(
                run_id=rid,
                status=RunStatus.WAITING_APPROVAL,
                iteration=1,
                step="awaiting_approval",
            )
        )
    return path


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
