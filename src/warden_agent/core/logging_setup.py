"""日志规范化：统一配置日志输出格式与级别。


原则：
  1. 统一用标准 logging，不 print。
  2. 一条日志一行，带时间、级别、logger 名、消息。
  3. 关键事件（审批、DENY、完成、失败）用 info/warning 级别，方便追踪。
  4. 绝不把 API Key、完整 Prompt 写进日志（安全）。
"""
from __future__ import annotations

import logging
import sys

# 统一的日志格式：时间 + 级别 + 模块 + 消息
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(level: int = logging.INFO) -> None:
    """配置根 logger。应用启动时调用一次即可。"""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_FORMAT))
    root = logging.getLogger()
    root.handlers.clear()  # 避免重复添加
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    """取一个子 logger（带模块名）。"""
    return logging.getLogger(name)
