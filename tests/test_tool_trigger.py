"""工具自解释增强测试：ToolSpec.triggers + 从描述自动提取触发词。"""
from warden_agent.loop.intent import ToolIntentRouter
from warden_agent.skill import SkillCatalog, SkillPackageParser
from warden_agent.skill.trigger import SkillTriggerRouter
from warden_agent.tool.catalog import ToolCatalog, function_tool
from warden_agent.tool.trigger import extract_triggers, tokens

# ---- 触发词提取本身 ----

def test_从英文描述提取触发词() -> None:
    words = extract_triggers("Get current weather for a city")
    assert "weather" in words
    assert "city" in words


def test_从中文描述提取bigram触发词() -> None:
    words = extract_triggers("获取某城市天气")
    assert "天气" in words  # "市天"+"天气" 双字
    assert "城市" in words


def test_过滤泛词() -> None:
    # "获取/信息/使用" 等太泛，不应被当成触发信号
    words = extract_triggers("获取信息使用工具")
    assert "获取" not in words
    assert "信息" not in words


def test_手工triggers优先于自动() -> None:
    words = extract_triggers("获取某城市天气", extra=("city", "forecast"))
    assert words[0] == "city"
    assert words[1] == "forecast"
    assert "天气" in words


def test_tokens_供技能匹配() -> None:
    assert "周报" in tokens("帮我写这周的周报")
    assert "调研" in tokens("深入调研新能源汽车")


# ---- ToolSpec.triggers 元数据 ----

def test_function_tool_可携带triggers() -> None:
    catalog = ToolCatalog()

    @function_tool(
        "kb.search", "在知识库检索资料",
        {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
        pure=True,
        triggers=("知识库", "资料", "检索"),
    )
    def search(q: str) -> str:
        return q

    catalog.register(search)
    spec = catalog.get("kb.search")
    assert spec.triggers == ("知识库", "资料", "检索")


# ---- intent 路由自动长出触发词 ----

def test_intent_无手配时从描述自动识别中文() -> None:
    """不给 triggers dict，只给 catalog，路由应从"获取某城市天气"自动认出天气话题。"""
    @function_tool(
        "weather.get", "获取某城市天气",
        {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
        pure=True,
    )
    def get_weather(city: str) -> str:
        return f"{city}: 晴"

    catalog = ToolCatalog()
    catalog.register(get_weather)

    router = ToolIntentRouter(catalog=catalog)  # 不手配触发词
    ok = router.relay("weather.get", {"description": "获取某城市天气"},
                      "明天上海天气怎么样", "")
    assert ok.action == "proceed"
    assert "自解释" in ok.reason

    miss = router.relay("weather.get", {"description": "获取某城市天气"},
                        "帮我算一下账", "")
    assert miss.action == "hint"


def test_intent_手配triggers与自动叠加() -> None:
    """手配的 triggers 叠加在自动提取之上；两者任一命中都放行。"""
    catalog = ToolCatalog()
    router = ToolIntentRouter(
        catalog=catalog,
        triggers={"weather.get": ["降雨"]},
    )
    # 手配的"降雨"命中（虽不在描述里）
    ok = router.relay("weather.get", {"description": "获取某城市天气"}, "明天有降雨吗", "")
    assert ok.action == "proceed"
    assert "手配触发词" in ok.reason


def test_intent_从triggers元数据识别() -> None:
    """工具自带 triggers 元数据时，路由直接用它（第一手）识别。"""
    catalog = ToolCatalog()

    @function_tool(
        "kb.search", "在知识库检索资料",
        {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
        pure=True,
        triggers=("知识库", "简历库"),
    )
    def search(q: str) -> str:
        return "ok"

    catalog.register(search)
    router = ToolIntentRouter(catalog=catalog)
    ok = router.relay("kb.search", {"description": "在知识库检索资料"},
                      "帮我在知识库找一份简历", "")
    assert ok.action == "proceed"


def test_intent_无触发信号的通用工具默认放行() -> None:
    """描述提取不出触发词的工具（无自说明/纯泛词），不应被意图路由误拦。"""
    router = ToolIntentRouter()
    v = router.relay("echo.say", {"description": ""}, "随便说点什么", "")
    assert v.action == "proceed"


# ---- skill 路由复用共享提取 + 工具自带 triggers ----

_WEEKLY = """---
name: 周报
description: 写周报
trust: trusted
---

# 周报

先收集本周工作，再列提纲，最后成稿校对。
"""


def _catalog() -> SkillCatalog:
    cat = SkillCatalog()
    cat.load_skill("weekly", SkillPackageParser().parse(_WEEKLY), source="inline")
    return cat


def test_skill_trigger_依旧按意图选中() -> None:
    router = SkillTriggerRouter(_catalog())
    verdicts = router.pick("帮我写这周的周报")
    assert verdicts and verdicts[0].alias == "weekly"
    assert "周报" in verdicts[0].hits


def test_skill_trigger_工具自带triggers元数据() -> None:
    tool = SkillTriggerRouter(_catalog()).tool()
    assert "技能" in tool.triggers
    assert "触发" in tool.triggers


def test_skill_trigger_工具可被intent识别() -> None:
    """把 skill 触发工具注册进 catalog，intent 路由能从它自带 triggers 认出触发信号。"""
    skill_router = SkillTriggerRouter(_catalog())
    tool = skill_router.tool()
    catalog = ToolCatalog()
    catalog.register(tool)

    intent = ToolIntentRouter(catalog=catalog)
    schema = {"description": "根据任务判断该触发哪个技能"}
    ok = intent.relay("skill.trigger.pick", schema,
                      "我不确定该用哪个技能来整理资料", "")
    assert ok.action == "proceed"
