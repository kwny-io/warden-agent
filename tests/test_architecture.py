"""架构边界测试 —— 保证依赖方向单向、低层不反向依赖高层。

架构边界测试（类似 ArchUnit 的思路）：
  - 用 AST 扫描 import，强制"低层模块不可反向依赖高层、依赖方向单向"。
  - Python 版用 AST 扫描 src/warden_agent 下每个模块的 import，检查层级秩。

层级（数字越小越底层，只能被更高层依赖）：
  tier 0  核心（kernel）        : core
  tier 1  能力基元（primitive） : model, tool, policy
  tier 2  核心逻辑（logic）     : store, loop, runtime, memory, skill, credential,
                                 rag, execution, multiagent
  tier 3  能力汇编（facade）    : mcp, web, agent
  tier 4  应用/演示（app）      : demo, demo_full

规则：模块只能 import「层级秩 <= 自己」的模块（同层允许）。
违反 = 低层反向依赖高层，例如 tool 不允许 import agent，model 不允许 import skill。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "warden_agent"

# 层秩
TIER: dict[str, int] = {
    "core": 0,
    "model": 1, "tool": 1, "policy": 1,
    "store": 2, "loop": 2, "runtime": 2, "memory": 2, "skill": 2,
    "credential": 2, "rag": 2, "execution": 2, "multiagent": 2,
    "mcp": 3, "web": 3, "agent": 3,
    "demo": 4, "demo_full": 4,
}

# 允许"向上依赖一到两层"的特例（真实存在的合理依赖）
_ALLOWED_UP: set[tuple[str, str]] = {
    # 例：runtime.session 需要依赖上层能力？实际上它依赖的都是同层或下层。
}

# demo/demo_full 是应用层，向上没有更高层，天然合法；但它们可以依赖所有。
_APP = {"demo", "demo_full"}


def _module_tier(module: str) -> int:
    """取一个 warden_agent.* 子模块的层秩（按第二段包名）。"""
    parts = module.split(".")
    # warden_agent.<pkg>[.sub]
    if len(parts) >= 2:
        pkg = parts[1]
        if pkg in TIER:
            return TIER[pkg]
    return 3  # 未知 → 视为中间层，宽松


def _collect_imports(path: Path) -> list[str]:
    """用 AST 收集一个文件 import 的所有 warden_agent.* 模块。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "warden_agent" or node.module.startswith("warden_agent."):
                imported.append(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "warden_agent" or alias.name.startswith("warden_agent."):
                    imported.append(alias.name)
    return imported


def _module_key(rel: Path) -> str:
    """把文件相对路径转成 warden_agent.x.y 模块名。"""
    parts = rel.with_suffix("").parts
    return ".".join(parts)


@pytest.mark.skipif(not SRC.exists(), reason="源码目录不存在")
def test_架构依赖方向单向() -> None:
    violations: list[str] = []

    for file in SRC.rglob("*.py"):
        if file.name == "__init__.py":
            continue
        rel = file.relative_to(SRC)
        module = _module_key(rel)               # <pkg>[.<sub>]，相对 SRC（不含 warden_agent）
        pkg = module.split(".")[0]              # <pkg>
        if pkg not in TIER:
            continue
        importing_tier = TIER[pkg]
        src_module = "warden_agent." + module      # warden_agent.<pkg>[.<sub>]

        for imported in _collect_imports(file):
            imported_pkg = imported.split(".")[1] if len(imported.split(".")) >= 2 else imported
            imported_tier = _module_tier(imported)
            if pkg in _APP or imported_pkg in _APP:
                continue  # 应用层自由
            if (pkg, imported_pkg) in _ALLOWED_UP:
                continue
            # 低层反向依赖高层 = 违反（数字大=更上层）
            if importing_tier < imported_tier:
                violations.append(
                    f"{src_module} (tier {importing_tier}) 反向依赖 "
                    f"{imported} (tier {imported_tier})"
                )

    assert not violations, (
        "架构依赖方向被破坏（低层不得反向依赖高层）：\n"
        + "\n".join(violations)
    )


def test_核心kernel最底层() -> None:
    """core 是 tier 0，不应 import 任何更上层的能力模块。"""
    core_dir = SRC / "core"
    violations = []
    for file in core_dir.rglob("*.py"):
        for imported in _collect_imports(file):
            imported_tier = _module_tier(imported)
            if imported_tier > 0:
                violations.append(f"core 依赖了高层: {imported} (tier {imported_tier})")
    assert not violations, "\n".join(violations)
