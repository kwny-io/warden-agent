"""Agent 评测集（Evals）—— 用黄金集离线度量 Agent 各层的行为质量。

单元测试回答"实现是否符合设计"；评测集回答另一个问题——**设计本身表现如何**：
意图路由判得准不准、技能触发选得对不对、端到端任务能不能完成。

三类黄金集（全部确定性、离线可重复、可进 CI 回归）：
  1. intent —— ToolIntentRouter 的"该不该调"判定（proceed / hint）
  2. skill  —— SkillTriggerRouter 的 top-1 技能选择
  3. e2e    —— 脚本化模型驱动真实 AgentLoop 完成任务
               （循环 / 失败自愈 / 防打转 / 意图提示）

运行：`python -m warden_agent.evals`（打印报告；类别通过率低于阈值时退出码 1）。
"""

from __future__ import annotations

from dataclasses import dataclass

from warden_agent.loop.intent import ToolIntentRouter
from warden_agent.loop.loop import AgentLoop, AgentReply
from warden_agent.model.model import AgentChatModel, ChatResponse, ToolCall
from warden_agent.skill import SkillCatalog, SkillPackageParser
from warden_agent.skill.trigger import SkillTriggerRouter
from warden_agent.tool.catalog import ToolCatalog, function_tool

# ---------------- 评测结果与报告 ----------------

THRESHOLDS: dict[str, float] = {"intent": 0.9, "skill": 0.9, "e2e": 1.0}


@dataclass(frozen=True)
class CaseResult:
    """一条黄金用例的判定结果。"""

    category: str
    name: str
    passed: bool
    expected: str
    actual: str


@dataclass(frozen=True)
class CategorySummary:
    """一个类别的通过率。"""

    category: str
    passed: int
    total: int

    @property
    def rate(self) -> float:
        return self.passed / self.total if self.total else 0.0


@dataclass(frozen=True)
class EvalReport:
    """一次完整评测的报告。"""

    results: tuple[CaseResult, ...]

    def summaries(self) -> tuple[CategorySummary, ...]:
        order = ["intent", "skill", "e2e"]
        out: list[CategorySummary] = []
        for cat in order:
            rows = [r for r in self.results if r.category == cat]
            out.append(CategorySummary(
                category=cat,
                passed=sum(1 for r in rows if r.passed),
                total=len(rows),
            ))
        return tuple(out)

    def overall_rate(self) -> float:
        total = len(self.results)
        if not total:
            return 0.0
        return sum(1 for r in self.results if r.passed) / total

    def failures(self) -> tuple[CaseResult, ...]:
        return tuple(r for r in self.results if not r.passed)

    def meets_thresholds(self) -> bool:
        return all(s.rate >= THRESHOLDS.get(s.category, 1.0) for s in self.summaries())


def format_report(report: EvalReport) -> str:
    """渲染成对齐的文本报告（CLI / CI 日志友好）。"""
    lines = ["category   passed/total   rate", "-" * 34]
    for s in report.summaries():
        lines.append(
            f"{s.category:<10} {s.passed:>6}/{s.total:<7} {s.rate:>6.1%}")
    lines.append("-" * 34)
    lines.append(f"overall    {report.overall_rate():>15.1%}")
    if report.failures():
        lines.append("")
        lines.append("failures:")
        for f in report.failures():
            lines.append(
                f"  [{f.category}] {f.name}: 期望 {f.expected}, 实际 {f.actual}")
    return "\n".join(lines)


# ---------------- 类别一：意图路由（该不该调） ----------------

_INTENT_CASES: tuple[tuple[str, str, str], ...] = (
    # (tool_name, user_text, expected_action)
    ("weather.get", "上海今天天气怎么样", "proceed"),
    ("weather.get", "北京现在气温多少度", "proceed"),
    ("weather.get", "查查杭州的天气再顺便看看气温", "proceed"),
    ("weather.get", "公司年假有几天", "hint"),
    ("knowledge.search", "在公司知识库里找一下报销制度", "proceed"),
    ("knowledge.search", "帮我检索一下差旅相关的文档", "proceed"),
    ("knowledge.search", "今天适合穿什么衣服", "hint"),
    ("code.read", "把 config.yaml 文件里的内容读出来", "proceed"),
    ("code.read", "看一下工作区里 main.py 写了什么", "proceed"),
    ("code.read", "上海明天下雨吗", "hint"),
    ("shell.run", "运行一下测试脚本", "proceed"),
    ("shell.run", "帮我把这段话翻译成英文", "hint"),
)


def _intent_catalog() -> ToolCatalog:
    @function_tool(
        "weather.get", "获取某个城市的实时天气与气温",
        {"type": "object", "properties": {"city": {"type": "string"}},
         "required": ["city"]},
        pure=True,
    )
    def get_weather(city: str) -> str:
        return f"{city}: 晴, 25度"

    @function_tool(
        "knowledge.search", "在企业知识库中检索制度与文档",
        {"type": "object", "properties": {"query": {"type": "string"}},
         "required": ["query"]},
        pure=True,
    )
    def knowledge_search(query: str) -> str:
        return "(无相关文档)"

    @function_tool(
        "code.read", "读取工作区内某个文件的内容",
        {"type": "object", "properties": {"path": {"type": "string"}},
         "required": ["path"]},
        pure=True,
    )
    def code_read(path: str) -> str:
        return "(文件内容)"

    @function_tool(
        "shell.run", "在受控沙箱中运行一条命令",
        {"type": "object", "properties": {"command": {"type": "string"}},
         "required": ["command"]},
        pure=False,
        triggers=("运行", "命令", "脚本", "执行"),
    )
    def shell_run(command: str) -> str:
        return "(命令输出)"

    catalog = ToolCatalog()
    for spec in (get_weather, knowledge_search, code_read, shell_run):
        catalog.register(spec)
    return catalog


def run_intent_evals() -> list[CaseResult]:
    router = ToolIntentRouter(_intent_catalog())
    out: list[CaseResult] = []
    for tool_name, query, expected in _INTENT_CASES:
        spec = router.catalog.get(tool_name)
        verdict = router.relay(
            tool_name,
            {"description": spec.description, "parameters": spec.parameters_schema},
            query,
        )
        out.append(CaseResult(
            category="intent",
            name=f"{tool_name} ← {query}",
            passed=verdict.action == expected,
            expected=expected,
            actual=verdict.action,
        ))
    return out


# ---------------- 类别二：技能触发（top-1 选择） ----------------

_SKILL_LIBRARY: tuple[tuple[str, str, str], ...] = (
    # (alias, SKILL.md 正文, 说明)
    ("weekly-report",
     "---\nname: 周报撰写\ndescription: 撰写团队周报:汇总本周进展、列出下周计划、标注风险\n---\n"
     "先汇总本周进展,再列出下周计划,最后标注风险,成稿发给团队。",
     "周报撰写"),
    ("ops-triage",
     "---\nname: 运维排障\ndescription: 线上故障排查:看日志、定位服务、给出处理建议\n---\n"
     "先看日志定位报错,再检查服务状态,给出处理建议。",
     "运维排障"),
    ("research-deep",
     "---\nname: 深度调研\ndescription: 多来源调研并输出对比报告:检索、整理、成稿\n---\n"
     "多来源检索资料,整理成对比要点,输出调研报告。",
     "深度调研"),
    ("code-review",
     "---\nname: 代码审查\ndescription: 审查代码改动:找 bug、风格问题、安全隐患\n---\n"
     "逐段审查代码,标记 bug 与安全隐患,给出修改建议。",
     "代码审查"),
)

_SKILL_CASES: tuple[tuple[str, str], ...] = (
    ("帮我写一下这周的周报,重点是项目进展", "weekly-report"),
    ("把本周工作总结成周报发给团队", "weekly-report"),
    ("线上服务挂了,帮我排查一下日志", "ops-triage"),
    ("看看日志里为什么一直报错", "ops-triage"),
    ("调研一下三款向量数据库,写个对比报告", "research-deep"),
    ("多来源检索资料,整理成一份调研报告", "research-deep"),
    ("审查一下这段代码有没有 bug 和安全隐患", "code-review"),
    ("帮我看看这次改动的代码质量", "code-review"),
)


def _skill_catalog() -> SkillCatalog:
    catalog = SkillCatalog()
    parser = SkillPackageParser()
    for alias, text, _label in _SKILL_LIBRARY:
        catalog.load_skill(alias, parser.parse(text), source="inline")
    return catalog


def run_skill_evals() -> list[CaseResult]:
    router = SkillTriggerRouter(_skill_catalog())
    out: list[CaseResult] = []
    for task, expected_alias in _SKILL_CASES:
        picks = router.pick(task)
        actual = picks[0].alias if picks else "(无候选)"
        out.append(CaseResult(
            category="skill",
            name=task,
            passed=actual == expected_alias,
            expected=expected_alias,
            actual=actual,
        ))
    return out


# ---------------- 类别三：端到端任务（脚本化模型驱动真实循环） ----------------

class _ScriptedModel(AgentChatModel):
    """按剧本走的确定性模型（与 tests/conftest.ScriptedModel 同思路）。"""

    def __init__(self, script: list[ChatResponse]) -> None:
        self.script = list(script)
        self.calls = 0

    def chat(self, request: object) -> ChatResponse:
        if self.calls >= len(self.script):
            raise AssertionError("脚本模型被调用次数超过剧本长度")
        resp = self.script[self.calls]
        self.calls += 1
        return resp


def _e2e_catalog() -> ToolCatalog:
    @function_tool(
        "weather.get", "获取某个城市的实时天气与气温",
        {"type": "object", "properties": {"city": {"type": "string"}},
         "required": ["city"]},
        pure=True,
    )
    def get_weather(city: str) -> str:
        return f"{city}: 晴, 25度"

    catalog = ToolCatalog()
    catalog.register(get_weather)
    return catalog


def _run_e2e_case(name: str, responses: list[ChatResponse], user_text: str,
                  *, intent: bool = False) -> tuple[CaseResult, AgentReply]:
    """跑一个端到端用例，返回 (判定, AgentReply) 供进一步断言。"""
    catalog = _e2e_catalog()
    loop = AgentLoop(
        model=_ScriptedModel(responses),
        catalog=catalog,
        intent=ToolIntentRouter(catalog) if intent else None,
    )
    reply = loop.run(user_text)
    passed = bool(reply.text) and len(reply.text) > 0
    return CaseResult(
        category="e2e", name=name, passed=passed,
        expected="非空回答", actual=reply.text or "(空)",
    ), reply


def run_e2e_evals() -> list[CaseResult]:
    out: list[CaseResult] = []

    # 1. 单工具任务：调一次工具 → 汇总回答
    res, reply = _run_e2e_case(
        "单工具任务",
        [ChatResponse(content=None, finish_reason="tool_calls",
                      tool_calls=[ToolCall(id="1", name="weather.get",
                                           arguments={"city": "上海"})]),
         ChatResponse(content="上海今天晴,25度")],
        "上海天气怎么样")
    ok = res.passed and "晴" in (reply.text or "")
    out.append(CaseResult(res.category, res.name, ok, res.expected, res.actual))

    # 2. 无工具直答
    out.append(_run_e2e_case(
        "直接回答",
        [ChatResponse(content="你好呀,我是 Warden")],
        "你好")[0])

    # 3. 多步任务：连续两次工具调用再汇总
    res, reply = _run_e2e_case(
        "多步任务",
        [ChatResponse(content=None, finish_reason="tool_calls",
                      tool_calls=[ToolCall(id="1", name="weather.get",
                                           arguments={"city": "上海"})]),
         ChatResponse(content=None, finish_reason="tool_calls",
                      tool_calls=[ToolCall(id="2", name="weather.get",
                                           arguments={"city": "北京"})]),
         ChatResponse(content="上海晴,北京也晴")],
        "对比上海和北京的天气")
    ok = res.passed and "北京" in (reply.text or "")
    out.append(CaseResult(res.category, res.name, ok, res.expected, res.actual))

    # 4. 失败自愈：先调不存在的工具 → 错误喂回 → 模型纠错换正确工具
    res, reply = _run_e2e_case(
        "失败自愈",
        [ChatResponse(content=None, finish_reason="tool_calls",
                      tool_calls=[ToolCall(id="1", name="nope.tool",
                                           arguments={})]),
         ChatResponse(content=None, finish_reason="tool_calls",
                      tool_calls=[ToolCall(id="2", name="weather.get",
                                           arguments={"city": "上海"})]),
         ChatResponse(content="查到了:上海晴")],
        "上海天气怎么样")
    ok = res.passed and "晴" in (reply.text or "")
    out.append(CaseResult(res.category, res.name, ok, res.expected, res.actual))

    # 5. 意图提示：误调被路由器拦下 → 模型改为直答
    res, reply = _run_e2e_case(
        "意图提示防误调",
        [ChatResponse(content=None, finish_reason="tool_calls",
                      tool_calls=[ToolCall(id="1", name="weather.get",
                                           arguments={"city": "公司"})]),
         ChatResponse(content="年假是 5 天")],
        "公司年假有几天", intent=True)
    hint_seen = any("意图" in (m.content or "") for m in reply.messages)
    ok = res.passed and hint_seen and "5 天" in (reply.text or "")
    out.append(CaseResult(res.category, res.name, ok, res.expected, res.actual))

    # 6. 防打转：同一成功调用原样重发 → 提示换策略 → 模型直答
    res, reply = _run_e2e_case(
        "防打转",
        [ChatResponse(content=None, finish_reason="tool_calls",
                      tool_calls=[ToolCall(id="1", name="weather.get",
                                           arguments={"city": "上海"})]),
         ChatResponse(content=None, finish_reason="tool_calls",
                      tool_calls=[ToolCall(id="2", name="weather.get",
                                           arguments={"city": "上海"})]),
         ChatResponse(content="还是上海晴,不用再查了")],
        "上海天气怎么样")
    ok = res.passed and "不用再查" in (reply.text or "")
    out.append(CaseResult(res.category, res.name, ok, res.expected, res.actual))

    return out


# ---------------- 汇总 ----------------

def run_all() -> EvalReport:
    """跑全部黄金集，返回报告。"""
    return EvalReport(results=tuple(
        [*run_intent_evals(), *run_skill_evals(), *run_e2e_evals()]))
