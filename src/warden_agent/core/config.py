"""配置加载：支持 .env 文件，避免把密钥写进代码。

小且零依赖的实现（不引 python-dotenv）：
  启动时调用 load_env()，会读取项目根目录的 .env（若存在）并把键值写进环境变量。
  之后所有代码只要读 os.environ 即可，密钥不进代码、不提交仓库（.gitignore 已忽略 .env）。

用法：
    from warden_agent.core.config import load_env
    load_env()   # 在程序入口最前面调用一次
    # 之后 os.environ.get("DEEPSEEK_API_KEY") 就能拿到

设 `WARDEN_IGNORE_DOTENV=1` → **根本不打开这个文件**，配置只从环境变量来。
这条开关存在的两个理由都是"静默改行为"那一类的：
  - 部署侧：容器/工作目录里混进一个 `.env`，它会悄悄盖掉你没显式设的键
    （12 因素：配置只从环境进；镜像里漏进一个 `.env` 是最阴的那种事故）；
  - 测试侧：`web/e2e/start-e2e-server.mjs` 用它把"本机 .env 恰好写了什么"
    从 e2e 的输入里剔除——否则 e2e 的档位会随开发机的本地文件变。
"""
from __future__ import annotations

import os
from pathlib import Path

from warden_agent.core.settings import env_bool


def load_env(path: str | Path = ".env") -> None:
    """从 .env 文件读取 KEY=VALUE 行并写入环境变量（不覆盖已存在的）。

    `WARDEN_IGNORE_DOTENV=1` 时**根本不打开这个文件**（配置只从环境来）。
    注意这条开关只管"要不要读这个文件"，与"读到了要不要覆盖已有键"是两件事
    （后者永远是"不覆盖"）。
    """
    if env_bool("WARDEN_IGNORE_DOTENV"):
        return
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
