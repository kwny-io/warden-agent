"""技能触发判断测试（skill.trigger）。"""
from warden_agent.skill import SkillCatalog, SkillPackageParser
from warden_agent.skill.trigger import SkillTriggerRouter
from warden_agent.tool.catalog import ToolCatalog

_WEEKLY = """---
name: 周报
description: 写周报
trust: trusted
---

# 周报

先收集本周工作，再列提纲，最后成稿校对。
"""

_DEEP = """---
name: 深调研
description: 深入调研一个话题并整理成结构化简报
trust: trusted
---

# Deep Research

当需要深入调研时先检索资料再汇总。
"""


def _catalog() -> SkillCatalog:
    cat = SkillCatalog()
    cat.load_skill("weekly", SkillPackageParser().parse(_WEEKLY), source="inline")
    cat.load_skill("deep", SkillPackageParser().parse(_DEEP), source="inline")
    return cat


def _router(cat: SkillCatalog | None = None) -> SkillTriggerRouter:
    return SkillTriggerRouter(cat or _catalog())


def test_按意图选中正确技能() -> None:
    router = _router()
    # 问"写周报"应该命中 weekly 技能
    verdicts = router.pick("帮我写这周的周报")
    assert verdicts, "应当有技能候选"
    assert verdicts[0].alias == "weekly"
    assert "周报" in verdicts[0].hits


def test_不同任务选中不同技能() -> None:
    router = _router()
    deep = router.pick("深入调研新能源汽车行业，写一份结构化简报")
    assert deep and deep[0].alias == "deep"


def test_无匹配返回空() -> None:
    router = _router()
    assert router.pick("今天天气如何") == []


def test_describe_candidates_带理由() -> None:
    router = _router()
    text = router.describe_candidates("帮我写周报")
    assert "技能触发建议" in text
    assert "周报" in text


def test_无匹配时describe_提示不需要() -> None:
    router = _router()
    text = router.describe_candidates("你好")
    assert "不需要触发技能" in text


def test_触发器暴露为工具() -> None:
    router = _router()
    tool = router.tool()
    catalog = ToolCatalog()
    catalog.register(tool)
    result = catalog.execute("skill.trigger.pick", {"task": "帮我写周报"})
    assert "周报" in str(result)
