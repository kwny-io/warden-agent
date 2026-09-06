"""build_agent(sandbox=True) 接线测试 —— 沙箱工具进入产品路径。

验证四件事：
  1. 开关生效：sandbox=True 才暴露 shell.run，默认不暴露（向后兼容）；
  2. 网络策略：疑似联网命令在语义层被拒绝（跨平台确定性）；
  3. 真实执行：命令跑在沙箱里，stdout / exit_code 正常回传；
  4. 只读工作区：命令跑在临时副本上——副本里能读写，宿主目录分毫不动。
"""
import sys
import tempfile
from pathlib import Path

import pytest

from warden_agent.agent import augment_catalog
from warden_agent.tool.catalog import ToolCatalog

_PY = f'"{Path(sys.executable).as_posix()}"'


def _catalog_with_sandbox() -> ToolCatalog:
    catalog = ToolCatalog()
    augment_catalog(catalog, sandbox=True)
    return catalog


def test_默认不暴露_shell_run_开关打开才注册() -> None:
    default_catalog = ToolCatalog()
    augment_catalog(default_catalog)
    with pytest.raises(KeyError):
        default_catalog.get("shell.run")

    sandboxed = _catalog_with_sandbox()
    assert sandboxed.get("shell.run").name == "shell.run"


def test_网络策略_默认禁网拒绝联网命令() -> None:
    catalog = _catalog_with_sandbox()
    result = catalog.execute("shell.run", {"command": "curl http://example.com"})
    assert "[沙箱拒绝]" in result
    assert "禁网" in result


def test_沙箱内真实执行_回传退出码与输出() -> None:
    catalog = _catalog_with_sandbox()
    result = catalog.execute(
        "shell.run", {"command": f'{_PY} -c "print(\'sandbox-ok\')"'}).strip()
    assert "sandbox-ok" in result
    assert "exit_code: 0" in result


def test_只读工作区_副本可写但宿主不动() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        host = Path(tmp)
        (host / "input.txt").write_text("hello", encoding="utf-8")

        catalog = _catalog_with_sandbox()
        ws = host.as_posix()
        seen = catalog.execute(
            "shell.run",
            {"command": f'{_PY} -c "import os;print(sorted(os.listdir(\'.\')))"',
             "workspace": ws})
        assert "input.txt" in seen

        # 副本里创建文件：命令应成功
        written = catalog.execute(
            "shell.run",
            {"command": f'{_PY} -c "import pathlib; pathlib.Path(\'new.txt\').write_text(\'x\')"',
             "workspace": ws})
        assert "exit_code: 0" in written
        # 但宿主目录分毫不动：副本的写入不回传
        assert not (host / "new.txt").exists()
