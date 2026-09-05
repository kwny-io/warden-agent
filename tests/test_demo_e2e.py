"""端到端演示（demo_e2e）可运行性测试：离线引导演示必须能完整跑通。"""
from __future__ import annotations

import io
import os
from contextlib import redirect_stdout

os.environ.pop("DEEPSEEK_API_KEY", None)

import warden_agent.demo_e2e as demo_e2e


def test_guided_demo_完整跑通() -> None:
    """离线引导演示应输出所有能力章节且不抛异常。"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        demo_e2e._guided_demo()
    out = buf.getvalue()
    # 应出现四个能力章节的关键标记
    assert "阶段规划" in out
    assert "工具意图判断" in out
    assert "RAG 引用" in out
    assert "结构化交接" in out
    assert "技能触发判断" in out
    # 规划的阶段导航（进度)
    assert "任务规划" in out


def test_autonomous_demo_自主闭环跑通() -> None:
    """离线自主闭环演示应真实跑完一条任务：主管派单→调研→交接→成稿→汇总。"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        demo_e2e._autonomous_demo()
    out = buf.getvalue()
    # 关键闭环标记：真实发生了工具调用链与交接
    assert "最终回答" in out
    assert "已按调研与成稿流程" in out
    assert "交接单" in out  # 结构化交接真发生了
    assert "research" in out and "write" in out  # 主管真的派了两个专员


def test_run_demo_无key走自主闭环优先() -> None:
    """无 DEEPSEEK_API_KEY 时 run_demo 先跑离线自主闭环，再跑分节引导。"""
    os.environ.pop("DEEPSEEK_API_KEY", None)
    buf = io.StringIO()
    with redirect_stdout(buf):
        demo_e2e.run_demo()
    out = buf.getvalue()
    # 自主闭环在前，引导拆解在后
    assert "离线自主闭环" in out
    assert "离线引导演示完成" in out
    assert out.index("离线自主闭环") < out.index("离线引导演示完成")

