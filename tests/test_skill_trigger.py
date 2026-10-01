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


# ---- 产品接线：触发器真的进了产品目录（以前只有 demo/评测注册） ----

def test_技能目录装配后_产品目录里有触发卡() -> None:
    """装配了技能（`skills=`）→ 目录里就该有 `skill.trigger.pick`。

    改之前：`agent.augment_catalog` 只注册 `skill.<alias>.run` 激活卡，
    触发器只在 `demo_e2e.py` 与 `evals/runner.py` 里注册——产品路径上
    “按任务意图选该用哪本 SKILL.md”这件事实际不存在。
    """
    from warden_agent.agent import augment_catalog
    from warden_agent.tool.catalog import ToolCatalog

    catalog = ToolCatalog()
    augment_catalog(catalog, skills={"weekly": _WEEKLY})
    assert "skill.trigger.pick" in catalog.names()


def test_触发卡是只读的_不需要审批() -> None:
    """触发器只做读卡判断（`pure=True`），不该被审批闸门拦下。"""
    from warden_agent.agent import augment_catalog
    from warden_agent.tool.catalog import ToolCatalog

    catalog = ToolCatalog()
    augment_catalog(catalog, skills={"weekly": _WEEKLY})
    assert catalog.get("skill.trigger.pick").pure is True


def test_没装技能就不注册触发卡() -> None:
    """没技能可挑时不注册空触发器：给了也没东西可挑，徒增一张卡。"""
    from warden_agent.agent import augment_catalog
    from warden_agent.tool.catalog import ToolCatalog

    catalog = ToolCatalog()
    augment_catalog(catalog)
    assert "skill.trigger.pick" not in catalog.names()
