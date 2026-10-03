"""配置加载测试。"""
import os
from pathlib import Path

import pytest

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


def test_load_env_设了IGNORE_DOTENV就根本不读文件(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`WARDEN_IGNORE_DOTENV=1` 时，.env 里的键**一个都不该进环境**。

    这条开关是给"配置只从环境来"的场景用的（容器/k8s 与 e2e）：`.env` 只在本机存在（gitignore），
    它会悄悄盖掉你没显式设的键 —— e2e 的"确定性档"最怕这个（档位会随跑的人的本地文件变）。
    """
    env_file = tmp_path / ".env"
    env_file.write_text("MY_IGNORED_KEY=should-not-appear\n", encoding="utf-8")
    monkeypatch.delenv("MY_IGNORED_KEY", raising=False)
    monkeypatch.setenv("WARDEN_IGNORE_DOTENV", "1")

    load_env(env_file)

    assert "MY_IGNORED_KEY" not in os.environ


def test_load_env_IGNORE_DOTENV为假值时仍然读(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """口径必须与该仓库其它布尔开关一致（`_BOOL_ON` = 1/true/yes/on），不能另立一套。

    否则就是又一件"同名不同义"的事：操作者按文档写 `WARDEN_IGNORE_DOTENV=true`，
    而这里只认 `1` —— 静默不生效，完全看不出。
    """
    env_file = tmp_path / ".env"
    env_file.write_text("MY_BOOL_KEY=v\n", encoding="utf-8")
    for value in ("0", "false", "off", ""):
        monkeypatch.delenv("MY_BOOL_KEY", raising=False)
        monkeypatch.setenv("WARDEN_IGNORE_DOTENV", value)
        load_env(env_file)
        assert os.environ.get("MY_BOOL_KEY") == "v", f"取值 {value!r} 不该关掉读 .env"
        os.environ.pop("MY_BOOL_KEY", None)
