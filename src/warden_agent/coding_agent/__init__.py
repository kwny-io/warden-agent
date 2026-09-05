"""Coding Agent：给需求 → 读代码 → 出 diff → 走门禁落地。"""

from warden_agent.coding_agent.coding_agent import CodingResult, run_coding_task

__all__ = ["run_coding_task", "CodingResult"]
