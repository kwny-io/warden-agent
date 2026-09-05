"""配置加载：支持 .env 文件，避免把密钥写进代码。

小且零依赖的实现（不引 python-dotenv）：
  启动时调用 load_env()，会读取项目根目录的 .env（若存在）并把键值写进环境变量。
  之后所有代码只要读 os.environ 即可，密钥不进代码、不提交仓库（.gitignore 已忽略 .env）。

用法：
    from warden_agent.core.config import load_env
    load_env()   # 在程序入口最前面调用一次
    # 之后 os.environ.get("DEEPSEEK_API_KEY") 就能拿到
"""
from __future__ import annotations

import os
from pathlib import Path


def load_env(path: str | Path = ".env") -> None:
    """从 .env 文件读取 KEY=VALUE 行并写入环境变量（不覆盖已存在的）。"""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        # 跳过空行和注释
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:  # 不覆盖已设置的环境变量
            os.environ[key] = value
