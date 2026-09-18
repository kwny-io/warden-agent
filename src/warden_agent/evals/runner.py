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
from typing import Any

from warden_agent.loop.intent import ToolIntentRouter
from warden_agent.loop.loop import AgentLoop, AgentReply
from warden_agent.model.model import AgentChatModel, ChatResponse, ToolCall
from warden_agent.policy.policy import (
    Decision,
    Policy,
    PolicyDenied,
    PolicyEngine,
    PolicyResult,
)
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


# ---------------- 类别三：端到端循环能力（脚本化模型驱动真实 AgentLoop） ----------------
#
# ⚠️ **先划清边界（必读）**：这一类的模型是**脚本化的**（按剧本返回固定响应），
#    所以它测的是 **harness（循环本身）的能力**——失败自愈、防打转、意图门禁、
#    策略门禁、迭代上限收口、轨迹不变量。它**测不了"模型好不好"**：
#    提示注入、幻觉、指令遵循这些都需要真实模型；脚本模型照着剧本走，
#    拿它测注入只是自欺（一定要写的话，那是"剧场测试"）。
#    模型能力评测要另配 `--mode real`（尚未实现）。
#
# 断言落在**轨迹与决策**上，不是"回答非空"——后者等于没测。

_HINT_PREFIX = "[意图提示]"
_LOOP_BREAK_HINT = "[注意] 你已调用过"


class _ScriptedModel(AgentChatModel):
    """按剧本走的确定性模型（与 tests/conftest.ScriptedModel 同思路）。

    剧本演完还继续被调用 = 用例本身写错了（说明循环没在该停的地方停），
    所以这里直接抛断言错误，而不是悄悄返回空响应。
    """

    def __init__(self, script: list[ChatResponse]) -> None:
        self.script = list(script)
        self.calls = 0

    def chat(self, request: object) -> ChatResponse:
        if self.calls >= len(self.script):
            raise AssertionError("脚本模型被调用次数超过剧本长度")
        resp = self.script[self.calls]
        self.calls += 1
        return resp


@dataclass(frozen=True)
class _TraceStep:
    """从对话记录里还原出的一步工具调用。"""

    name: str
    arguments: dict[str, Any]
    is_hint: bool   # True = 被门禁拦下、只喂回提醒，**没有真的执行**
    body: str       # 工具结果正文（或提醒文案）


def _trace(reply: AgentReply) -> list[_TraceStep]:
    """把"tool 角色 + 带 tool_call"的消息还原成调用轨迹（按发生顺序）。

    说明：意图拦截、防打转提示也会以 tool 角色喂回，靠文案前缀区分
    "真的执行了" 与 "只提醒"——这两者在能力上完全不同，不能混为一谈。
    """
    steps: list[_TraceStep] = []
    for m in reply.messages:
        if m.role != "tool" or m.tool_call is None:
            continue
        steps.append(_TraceStep(
            name=m.tool_call.name,
            arguments=dict(m.tool_call.arguments),
            is_hint=m.content.startswith((_HINT_PREFIX, _LOOP_BREAK_HINT)),
            body=m.content,
        ))
    return steps


def _executed(trace: list[_TraceStep]) -> list[_TraceStep]:
    """只保留真正执行的步骤（排除门禁提示）。"""
    return [s for s in trace if not s.is_hint]


def _describe(trace: list[_TraceStep]) -> str:
    """把轨迹渲染成一行，便于失败时看清发生了什么。"""
    if not trace:
        return "(无工具调用)"
    return " → ".join(
        f"{s.name}{'(门禁提示,未执行)' if s.is_hint else ''}" for s in trace
    )


def _case(name: str, ok: bool, expected: str, actual: str) -> CaseResult:
    return CaseResult(category="e2e", name=name, passed=ok,
                      expected=expected, actual=actual)


def _e2e_catalog() -> ToolCatalog:
    @function_tool(
        "weather.get", "获取某个城市的实时天气与气温",
        {"type": "object", "properties": {"city": {"type": "string"}},
         "required": ["city"]},
        pure=True,
    )
    def get_weather(city: str) -> str:
        return f"{city}: 晴, 25度"

    @function_tool(
        "boom.run", "总是抛异常的工具（用于验证 harness 不被打穿）",
        {"type": "object", "properties": {}, "required": []},
        pure=False,
    )
    def boom() -> str:
        raise RuntimeError("工具内部炸了")

    catalog = ToolCatalog()
    catalog.register(get_weather)
    catalog.register(boom)
    return catalog


def _drive(
    responses: list[ChatResponse],
    user_text: str,
    *,
    intent: bool = False,
    policy: PolicyEngine | None = None,
    max_iterations: int = 10,
) -> tuple[AgentReply | None, list[_TraceStep], Exception | None]:
    """跑一次真实 AgentLoop，返回 (回复, 轨迹, 异常)。

    异常不吞：有些能力边界（策略 DENY、迭代上限）**就是**抛异常，必须能断言到。
    """
    catalog = _e2e_catalog()
    loop = AgentLoop(
        model=_ScriptedModel(responses),
        catalog=catalog,
        policy_engine=policy,
        max_iterations=max_iterations,
        intent=ToolIntentRouter(catalog) if intent else None,
    )
    try:
        reply = loop.run(user_text)
    except Exception as exc:  # noqa: BLE001 —— 有意捕获：断言的就是这些异常
        return None, [], exc
    return reply, _trace(reply), None


def _deny_tool(tool_name: str) -> Policy:
    """构造一条"拒绝指定工具"的策略。"""
    def _policy(name: str, arguments: dict[str, object]) -> PolicyResult:
        if name == tool_name:
            return PolicyResult(Decision.DENY, f"动作 {name!r} 被策略禁止")
        return PolicyResult(Decision.ALLOW)
    return _policy


def run_e2e_evals() -> list[CaseResult]:
    out: list[CaseResult] = []

    # 1. 单工具任务：调一次工具 → 用工具结果汇总回答
    reply, trace, err = _drive(
        [ChatResponse(content=None, finish_reason="tool_calls",
                      tool_calls=[ToolCall(id="1", name="weather.get",
                                           arguments={"city": "上海"})]),
         ChatResponse(content="上海今天晴,25度")],
        "上海天气怎么样")
    ex = _executed(trace)
    out.append(_case(
        "单工具任务",
        err is None and len(ex) == 1 and ex[0].name == "weather.get"
        and ex[0].arguments.get("city") == "上海"
        and "晴" in (reply.text if reply else ""),
        "恰好执行 1 次 weather.get(city=上海)，且回答含'晴'",
        f"{_describe(trace)}｜回答={reply.text if reply else err!r}"))

    # 2. 无工具直答：不该凭空调工具
    reply, trace, err = _drive([ChatResponse(content="你好呀,我是 Warden")], "你好")
    out.append(_case(
        "直接回答不调工具",
        err is None and len(_executed(trace)) == 0 and bool(reply and reply.text),
        "不执行任何工具，直接给出回答",
        f"{_describe(trace)}｜回答={reply.text if reply else err!r}"))

    # 3. 多步任务：参数与顺序都要对
    reply, trace, err = _drive(
        [ChatResponse(content=None, finish_reason="tool_calls",
                      tool_calls=[ToolCall(id="1", name="weather.get",
                                           arguments={"city": "上海"})]),
         ChatResponse(content=None, finish_reason="tool_calls",
                      tool_calls=[ToolCall(id="2", name="weather.get",
                                           arguments={"city": "北京"})]),
         ChatResponse(content="上海晴,北京也晴")],
        "对比上海和北京的天气")
    cities = [s.arguments.get("city") for s in _executed(trace)]
    out.append(_case(
        "多步任务顺序与参数保真",
        err is None and cities == ["上海", "北京"],
        "按顺序执行 weather.get(上海) → weather.get(北京)",
        f"实际参数序列={cities}｜{_describe(trace)}"))

    # 4. 失败自愈：未注册工具不击穿，错误喂回后模型换工具
    reply, trace, err = _drive(
        [ChatResponse(content=None, finish_reason="tool_calls",
                      tool_calls=[ToolCall(id="1", name="nope.tool", arguments={})]),
         ChatResponse(content=None, finish_reason="tool_calls",
                      tool_calls=[ToolCall(id="2", name="weather.get",
                                           arguments={"city": "上海"})]),
         ChatResponse(content="查到了:上海晴")],
        "上海天气怎么样")
    out.append(_case(
        "失败自愈（未注册工具）",
        err is None and bool(trace) and trace[0].name == "nope.tool"
        and "执行失败" in trace[0].body
        and any(s.name == "weather.get" for s in _executed(trace))
        and "晴" in (reply.text if reply else ""),
        "首调失败→错误喂回→换 weather.get 成功，全程不崩",
        f"{_describe(trace)}｜首个错误={trace[0].body[:40] if trace else '(无)'}"))

    # 5. 意图门禁：信号不足的调用被拦下（只提示、不执行）
    reply, trace, err = _drive(
        [ChatResponse(content=None, finish_reason="tool_calls",
                      tool_calls=[ToolCall(id="1", name="weather.get",
                                           arguments={"city": "公司"})]),
         ChatResponse(content="年假是 5 天")],
        "公司年假有几天", intent=True)
    out.append(_case(
        "意图门禁拦下误调",
        err is None and len(trace) == 1 and trace[0].is_hint
        and len(_executed(trace)) == 0 and "5 天" in (reply.text if reply else ""),
        "该调用被拦为'提示'（未执行），模型改为直接回答",
        f"{_describe(trace)}｜回答={reply.text if reply else err!r}"))

    # 6. 防打转：同一调用重复发送被判定为打转（提示，而非再执行一遍）
    reply, trace, err = _drive(
        [ChatResponse(content=None, finish_reason="tool_calls",
                      tool_calls=[ToolCall(id="1", name="weather.get",
                                           arguments={"city": "上海"})]),
         ChatResponse(content=None, finish_reason="tool_calls",
                      tool_calls=[ToolCall(id="2", name="weather.get",
                                           arguments={"city": "上海"})]),
         ChatResponse(content="还是上海晴,不用再查了")],
        "上海天气怎么样")
    out.append(_case(
        "防打转（重复调用不重复执行）",
        err is None and len(_executed(trace)) == 1 and len(trace) == 2
        and trace[1].is_hint and "不用再查" in (reply.text if reply else ""),
        "同一调用只执行 1 次，第二次转为打转提示",
        f"{_describe(trace)}｜回答={reply.text if reply else err!r}"))

    # 7. 工具内部异常不击穿 harness
    reply, trace, err = _drive(
        [ChatResponse(content=None, finish_reason="tool_calls",
                      tool_calls=[ToolCall(id="1", name="boom.run", arguments={})]),
         ChatResponse(content=None, finish_reason="tool_calls",
                      tool_calls=[ToolCall(id="2", name="weather.get",
                                           arguments={"city": "上海"})]),
         ChatResponse(content="绕过异常,查到上海晴")],
        "上海天气怎么样")
    out.append(_case(
        "工具内部异常不击穿",
        err is None and bool(trace) and trace[0].name == "boom.run"
        and "失败" in trace[0].body
        and any(s.name == "weather.get" for s in _executed(trace)),
        "抛异常的工具被转成错误喂回，循环继续并完成任务",
        f"{_describe(trace)}｜异常={(err or '无')}"))

    # 8. 策略门禁 DENY 生效（fail-closed）：被禁的工具**不能**被执行
    policy = PolicyEngine()
    policy.add(_deny_tool("weather.get"))
    reply, trace, err = _drive(
        [ChatResponse(content=None, finish_reason="tool_calls",
                      tool_calls=[ToolCall(id="1", name="weather.get",
                                           arguments={"city": "上海"})]),
         ChatResponse(content="被拒了")],
        "上海天气怎么样", policy=policy)
    out.append(_case(
        "策略 DENY 不执行（fail-closed）",
        isinstance(err, PolicyDenied) and len(_executed(trace)) == 0,
        "被策略禁止的工具不被执行（抛 PolicyDenied）",
        f"异常={type(err).__name__ if err else '无'}｜{_describe(trace)}"))

    # 9. 迭代上限收口：模型一直要调工具时按上限停住，不无限循环
    reply, trace, err = _drive(
        [ChatResponse(content=None, finish_reason="tool_calls",
                      tool_calls=[ToolCall(id=str(i), name="weather.get",
                                           arguments={"city": f"城{i}"})])
         for i in range(1, 4)],
        "查三个城市", max_iterations=3)
    out.append(_case(
        "迭代上限收口",
        isinstance(err, RuntimeError) and "上限" in str(err),
        "迭代达到 max_iterations 时报错收口（不无限循环）",
        f"异常={(err or '无')}｜{_describe(trace)}"))

    # 10. 轨迹配对不变量：每个 tool_call 恰好一条结果（重复配对会让真实 API 报 400）
    reply, trace, err = _drive(
        [ChatResponse(content=None, finish_reason="tool_calls",
                      tool_calls=[ToolCall(id="1", name="weather.get",
                                           arguments={"city": "上海"})]),
         ChatResponse(content=None, finish_reason="tool_calls",
                      tool_calls=[ToolCall(id="2", name="weather.get",
                                           arguments={"city": "北京"})]),
         ChatResponse(content="都晴")],
        "对比上海北京")
    ids = [m.tool_call.id for m in (reply.messages if reply else [])
           if m.role == "tool" and m.tool_call is not None]
    out.append(_case(
        "轨迹配对不变量",
        err is None and len(ids) == len(set(ids)) and all(ids),
        "每个 tool_call_id 恰好出现一次（不重复、不为空）",
        f"tool_call_id 序列={ids}"))

    return out


# ---------------- 汇总 ----------------

def run_all() -> EvalReport:
    """跑全部黄金集，返回报告。"""
    return EvalReport(results=tuple(
        [*run_intent_evals(), *run_skill_evals(), *run_e2e_evals()]))
