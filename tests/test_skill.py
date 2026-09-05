"""Skill 技能系统测试：解析、目录、渐进披露、信任快照、Skill→Tool、目录加载。"""
from __future__ import annotations

import tempfile
from pathlib import Path

from warden_agent.skill import (
    SkillCatalog,
    SkillPackageParser,
    load_skills_from_dir,
    skill_to_tool,
)
from warden_agent.tool.catalog import ToolCatalog

_SKILL_MD = """---
name: deep-research
description: 深挖一个话题并整理成结构化简报
version: 1.0.0
author: warden
trust: trusted
---

# Deep Research

当你需要深入调研一个话题时：

1. 先列出现有结论与缺口
2. 逐层检索相关来源
3. 汇总成简报 {{ $scripts/report.py }}
"""


def test_解析SKILL_元数据() -> None:
    content = SkillPackageParser().parse(_SKILL_MD)
    assert content.metadata.name == "deep-research"
    assert content.metadata.description.startswith("深挖")
    assert content.metadata.trust == "trusted"
    assert "逐层检索" in content.body


def test_目录查找与快照() -> None:
    content = SkillPackageParser().parse(_SKILL_MD)
    catalog = SkillCatalog()
    catalog.load_skill("research", content, source="inline")

    binding = catalog.find("research")
    assert binding is not None
    snap = binding.trust_snapshot()
    assert snap.alias == "research"
    assert snap.source == "inline"
    assert snap.digest  # 有内容摘要


def test_渐进披露_激活才给正文() -> None:
    content = SkillPackageParser().parse(_SKILL_MD)
    catalog = SkillCatalog()
    catalog.load_skill("research", content)
    binding = catalog.find("research")
    activated = binding.activate()
    # 激活后能拿到正文指令
    assert "Deep Research" in activated
    assert "逐层检索" in activated


def test_skill转tool_可调用() -> None:
    content = SkillPackageParser().parse(_SKILL_MD)
    catalog = SkillCatalog()
    catalog.load_skill("research", content)
    binding = catalog.find("research")

    toolbar = ToolCatalog()
    toolbar.register(skill_to_tool(binding))
    out = toolbar.execute("skill.deep-research.run", {"goal": "调研新能源"})
    assert "逐层检索" in str(out)
    assert "调研新能源" in str(out)


def test_从目录加载() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "research" / "SKILL.md").parent.mkdir(parents=True)
        (root / "research" / "SKILL.md").write_text(_SKILL_MD, encoding="utf-8")
        (root / "writer" / "SKILL.md").parent.mkdir(parents=True)
        (root / "writer" / "SKILL.md").write_text(
            "---\nname: writer\ndescription: 成稿\n---\n\n# Writer 技能\n把要点写成文章",
            encoding="utf-8",
        )

        catalog = SkillCatalog()
        n = load_skills_from_dir(catalog, str(root))
        assert n == 2
        assert catalog.has("research")
        assert catalog.has("writer")


# ---- 技能版本化（多版本并存 + 默认取最新）----

def _skill_md(version: str, desc: str) -> str:
    front = f"---\nname: 周报\ndescription: {desc}\nversion: {version}\ntrust: trusted\n---"
    return f"{front}\n\n# 周报 {version} 步骤"


def test_同别名多版本并存() -> None:
    catalog = SkillCatalog()
    parser = SkillPackageParser()
    catalog.load_skill("weekly", parser.parse(_skill_md("1.0.0", "v1 步骤")), source="inline")
    catalog.load_skill("weekly", parser.parse(_skill_md("2.0.0", "v2 步骤")), source="inline")
    assert catalog.versions("weekly") == ["1.0.0", "2.0.0"]
    assert len(catalog.snapshot()) == 2  # 两个版本各有独立信任快照


def test_find不传版本默认取最新() -> None:
    catalog = SkillCatalog()
    parser = SkillPackageParser()
    catalog.load_skill("weekly", parser.parse(_skill_md("1.0.0", "旧")), source="inline")
    catalog.load_skill("weekly", parser.parse(_skill_md("3.0.0", "新")), source="inline")
    catalog.load_skill("weekly", parser.parse(_skill_md("2.0.0", "中")), source="inline")
    assert catalog.find("weekly").metadata().version == "3.0.0"


def test_find指定版本() -> None:
    catalog = SkillCatalog()
    parser = SkillPackageParser()
    catalog.load_skill("weekly", parser.parse(_skill_md("1.0.0", "旧")), source="inline")
    catalog.load_skill("weekly", parser.parse(_skill_md("2.0.0", "新")), source="inline")
    assert catalog.find("weekly", "1.0.0").metadata().version == "1.0.0"
    assert catalog.find("weekly", "不存在的版本") is None


def test_版本化目录加载() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # <alias>/<version>/SKILL.md 版本目录约定
        (root / "weekly" / "1.0.0" / "SKILL.md").parent.mkdir(parents=True)
        (root / "weekly" / "1.0.0" / "SKILL.md").write_text(
            _skill_md("", "无frontmatter版本,用目录名"), encoding="utf-8")
        (root / "weekly" / "2.0.0" / "SKILL.md").parent.mkdir(parents=True)
        (root / "weekly" / "2.0.0" / "SKILL.md").write_text(
            _skill_md("", "也走目录名"), encoding="utf-8")
        catalog = SkillCatalog()
        n = load_skills_from_dir(catalog, str(root))
        assert n == 2
        assert catalog.has("weekly")
        assert set(catalog.versions("weekly")) == {"1.0.0", "2.0.0"}
        # 默认取最新版
        assert catalog.find("weekly").metadata().version == "2.0.0"


def test_不同版本信任快照不同() -> None:
    catalog = SkillCatalog()
    parser = SkillPackageParser()
    catalog.load_skill("weekly", parser.parse(_skill_md("1.0.0", "a")), source="inline")
    catalog.load_skill("weekly", parser.parse(_skill_md("2.0.0", "b")), source="inline")
    # 直接比两个 find 的 digest 不同（版本不同 → 摘要不同）
    d1 = catalog.find("weekly", "1.0.0").trust_snapshot().digest
    d2 = catalog.find("weekly", "2.0.0").trust_snapshot().digest
    assert d1 != d2
