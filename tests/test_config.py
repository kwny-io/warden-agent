"""配置加载测试。"""
import os
from pathlib import Path

from warden_agent.core.config import load_env


def test_load_env_读取键值并写入环境变量(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# 注释行\n"
        'MY_TEST_KEY=hello123\n'
        'PORT="9000"\n'
        "EMPTY_LINE\n\n",
        encoding="utf-8",
    )
    # 确保测试键不在环境里，避免与真实环境冲突
    for k in ("MY_TEST_KEY", "PORT"):
        os.environ.pop(k, None)
    load_env(env_file)
    assert os.environ.get("MY_TEST_KEY") == "hello123"
    assert os.environ.get("PORT") == "9000"


def test_load_env_不覆盖已存在的环境变量(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("MY_EXISTING_KEY=new\n", encoding="utf-8")
    os.environ["MY_EXISTING_KEY"] = "old"
    load_env(env_file)
    assert os.environ.get("MY_EXISTING_KEY") == "old"  # 已存在的优先，不被覆盖
    os.environ.pop("MY_EXISTING_KEY", None)


def test_load_env_文件不存在不报错(tmp_path: Path) -> None:
    """文件不存在不该抛异常，也不该往环境里塞东西。

    断言"什么也没改变"而不是"没抛错"：这个函数是**启动路径**的一部分，
    静默地塞一个空字符串变量比报错更难查（下游按"设了但为空"处理）。
    """
    before = dict(os.environ)
    load_env(tmp_path / "no-such-file.env")
    assert dict(os.environ) == before
