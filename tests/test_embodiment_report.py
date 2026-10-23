"""时间线报告：自包含、可复现、被拒的必须是红的。"""

from __future__ import annotations

import re
from pathlib import Path

from warden_agent.embodiment.report import ReportMeta, build_timeline_html, write_timeline
from warden_agent.embodiment.types import Code, Event

META = ReportMeta(
    title="测试报告",
    backend="sim",
    joint_count=3,
    metrics={"拍数": "200", "拒绝": "1"},
)


def _events() -> list[Event]:
    return [
        Event(t=0.0, kind="command", code=Code.OK.value, detail="目标 [0.3, 0.0, 0.0]"),
        Event(t=0.02, kind="command", code=Code.LIMIT.value, detail="关节 0 超出 [-1.4, 1.4]"),
        Event(t=0.04, kind="safety", code=Code.ESTOP.value, detail="急停中"),
    ]


def test_报告是自包含的(tmp_path: Path) -> None:
    """无外链、无脚本、无 <link>：断网也能打开，也不会因为 CDN 挂了变成白屏。"""
    path = write_timeline(str(tmp_path / "r.html"), _events(), META)
    text = Path(path).read_text(encoding="utf-8")
    assert "http://" not in text and "https://" not in text
    assert "<script" not in text.lower()
    assert "<link" not in text.lower()
    assert "src=" not in text


def test_被拒事件在报告里是红的且有理由() -> None:
    text = build_timeline_html(_events(), META)
    assert text.count('class="mark denied"') == 2
    assert "row-denied" in text
    assert "关节 0 超出 [-1.4, 1.4]" in text
    assert "拒绝" in text


def test_事件文本会被转义() -> None:
    """detail 来自设备和模型，必须有被当成 HTML 执行的自觉。"""
    evil = Event(t=0.1, kind="command", code=Code.OK.value,
                 detail='<script>alert("x")</script>')
    text = build_timeline_html([evil], META)
    assert "<script>alert" not in text
    assert "&lt;script&gt;" in text


def test_同一输入两次生成字节一致(tmp_path: Path) -> None:
    a = write_timeline(str(tmp_path / "a.html"), _events(), META)
    b = write_timeline(str(tmp_path / "b.html"), _events(), META)
    assert Path(a).read_bytes() == Path(b).read_bytes()


def test_没有事件时报告不假装有数据() -> None:
    text = build_timeline_html([], META)
    assert "没有事件" in text
    assert "<circle" not in text


def test_演示产出的报告时间轴只有一个时钟(tmp_path: Path) -> None:
    """回归：supervisor 的事件原来打的是 time.monotonic（开机秒数），
    与反射环的设备时钟混在一张图上，横轴会从 0 直接跳到 39 万秒。"""
    from warden_agent import demo_robot

    out = tmp_path / "demo.html"
    assert demo_robot.main(["--sim", "--report", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    marks = [float(m) for m in re.findall(r"<td class=num>([\d.]+)</td>", text)]
    assert marks, "报告里应该有时间列"
    assert max(marks) < 60.0, f"时间轴混进了墙钟（最大 {max(marks)} 秒）"
