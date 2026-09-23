"""阶段规划 —— loop 深度③：复杂任务不闷头一口气做完，而是先拆阶段、分步推进、每步观察。

背景（为什么需要它）：
  最基础的 loop 是"拿到问题 → 一路调工具 → 直到给出回答"。对简单任务（"上海天气"）够用，
  但对复杂任务（"调研 A/B/C 三家公司，写对比报告并给建议"），一口气硬拼容易：
    - 上下文越拉越长，后面的模型容易忘掉最初的全局目标；
    - 没有"阶段感"，模型可能漏掉某一步，或顺序错乱；
    - 中途想改方向时，没有按阶段的检查点。

出色的做法（本模块落地的）：
  1. **任务复杂度判断**：先判断"这个任务是一次性能干完的，还是需要拆阶段"。
     判断靠轻量启发式（是否命中大量子任务/关键词 / 文本长度），不依赖模型，离线可测。
  2. **拆阶段**：复杂任务被拆成若干"阶段"，每阶段带一个"阶段目标"，注入给模型。
     阶段目标是给模型的"导航"，让它知道"这一步我在干嘛、下一步要去哪"。
  3. **分步推进 + 每步观察**：阶段目标压入上下文后，模型每一步都带着"当前阶段"推进；
     阶段之间通过注入的"阶段进度"让模型能看到全局，而不是闷头做。
  4. **规划可审核/可审计**：plan 记录在对话里（role=system），全程留痕，契合整个系统的"可查账"。

设计要点：
  - Planner 不真调用模型去"想"规划，而是用**确定性启发式**产出阶段（简单可靠、离线可测）；
    真实系统可换成"让模型自己规划"（把本类的启发式换成一次模型调用），接口保持一致。
  - 默认"不接管"：只有任务被判定为"复杂"才注入规划；简单任务保持原样走基础 loop。
  - 阶段目标是渐进注入的，避免一上来把 5 个阶段全塞给模型占上下文（贴合"用药需按需"）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from warden_agent.model.model import AgentChatModel

# ---- 常见"复杂任务"信号词：命中就倾向于拆阶段 ----
_COMPLEX_SIGNALS = {
    # 调研/对比类
    "调研", "调查", "研究", "对比", "比较", "分析", "评估", "报告", "汇总", "总结",
    # 多对象/多目标
    "三个", "多家", "分别", "每家公司", "所有", "列出", "逐一", "逐个",
    # 流程类
    "规划", "方案", "计划", "流程", "步骤", "指南", "教程", "落地", "实施",
    # 报告/文档产出
    "文档", "说明", "介绍", "整理成", "写一篇", "写份", "成稿",
}

# 信号命中几个及以上就算"复杂"
_COMPLEX_HIT_THRESHOLD = 2


@dataclass
class PlanStep:
    """规划中的一步：一个阶段 + 它的目标（给模型当导航用）。"""

    title: str
    goal: str  # 注入给模型的阶段目标


@dataclass
class TaskPlan:
    """一次任务的规划结果。"""

    is_complex: bool              # 是否被判定为复杂任务（决定是否拆阶段）
    steps: list[PlanStep] = field(default_factory=list)  # 拆出的阶段
    summary: str = ""             # 一段可给人看的规划简报（审计/演示用）


def _complexity(text: str) -> bool:
    """启发式判断一段任务是否"复杂到需要拆阶段"。

    规则：命中 >= _COMPLEX_HIT_THRESHOLD 个信号词（或同时有"调研+报告"，即使只命中两个词，
    也按信号词计数），或文本长度超过阈值，视为复杂。
    简单、直接的请求（"上海天气"）不会命中，保持基础 loop。
    """
    if len(text) > 160:  # 超长请求本身就倾向复杂
        return True
    hits = sum(1 for signal in _COMPLEX_SIGNALS if signal in text)
    return hits >= _COMPLEX_HIT_THRESHOLD


def build_plan(user_text: str, *, max_steps: int = 5) -> TaskPlan:
    """给定用户任务，判断是否复杂，复杂则拆成阶段。

    目前用固定的一套"通用研究流程"作为阶段模板：
      调研资料 → 梳理要点 → 组织成稿 → 收尾（可覆盖绝大多数"调研/报告/文档"类任务）。
    这是给"通用任务"用的；真实系统可换成领域专属模板或让模型自己规划。

    `max_steps` 给阶段数封顶（防模型/模板拆出几十步占爆上下文）。
    """
    if not _complexity(user_text):
        return TaskPlan(is_complex=False)

    steps = [
        PlanStep(
            title="调研与资料收集",
            goal="先收集与任务相关的资料与事实（可调用知识库/搜索等工具），把关键信息记下来。",
        ),
        PlanStep(
            title="梳理与组织",
            goal="把上一步收集到的资料去重、归类、提炼成要点，明确结论结构。",
        ),
        PlanStep(
            title="撰写与成稿",
            goal="基于梳理好的要点，组织成一份完整、通顺、有结论的输出。",
        ),
        PlanStep(
            title="复核与收尾",
            goal="检查输出是否覆盖了任务要求、有没有遗漏，给出最终回答。",
        ),
 ][: max(0, max_steps)]
    summary = "识别为复杂任务，拆分为阶段：" + " → ".join(s.title for s in steps)
    return TaskPlan(is_complex=True, steps=steps, summary=summary)


def plan_as_context(plan: TaskPlan, current: int) -> str:
    """把规划转成一段注入给模型的上下文（渐进披露当前阶段 + 全局进度）。

    - `current` 是当前应执行的阶段下标（第几步）。
    - 只给出"当前阶段的详细目标"，其余阶段只列标题当"进度条"，
      避免一次性把所有细节灌给模型（省上下文、聚焦当下）。
    """
    if not plan.is_complex or not plan.steps:
        return ""
    lines = ["[任务规划]"]
    total = len(plan.steps)
    for idx, step in enumerate(plan.steps):
        prefix = "▶" if idx == current else ("✔" if idx < current else "·")
        line = f"{prefix} {idx + 1}/{total} {step.title}"
        if idx == current:
            line += f"：{step.goal}"
        lines.append(line)
    return "\n".join(lines)


@dataclass
class PlanState:
    """一次运行中的规划**进度状态** —— 让"分步推进/每步观察"从静态提示变成可推进的状态机。

    此前阶段规划只在 run 开始时注入一次（`current` 恒为 0），所谓"分步推进"名不副实。
    本类承载"进行到第几步"：AgentLoop 在一步成功后 `advance()`、失败时 `mark_failed()`，
    并把最新阶段上下文重新注入给模型（每步观察）。

    边界与取向：
      - **有界**：步数受 `max_steps` 约束，`advance()` 绝不超过总步数（到末步即停）。
      - **失败不前进**：`mark_failed()` 只记录、不推进——后续阶段绝不会被误标为已完成。
      - **可观察/可持久化**：`to_dict()` 给出当前步与每步完成状态，供审计或落盘。
    """

    plan: TaskPlan
    max_steps: int = 5
    current: int = 0          # 当前进行到的阶段下标（0-based）
    failed: bool = False      # 当前阶段是否失败（失败则不推进）
    failure: str = ""         # 失败原因（给观察/审计用）

    def _steps(self) -> list[Any]:
        return list(getattr(self.plan, "steps", None) or [])

    @property
    def total(self) -> int:
        """总步数（受 max_steps 封顶）。"""
        return min(len(self._steps()), max(int(self.max_steps), 0))

    @property
    def done(self) -> bool:
        """是否所有阶段都已完成。"""
        return self.current >= self.total

    @property
    def current_step(self) -> Any | None:
        """当前阶段的 PlanStep（越界/空规划返回 None）。"""
        steps = self._steps()
        return steps[self.current] if 0 <= self.current < self.total else None

    def advance(self) -> int:
        """把当前阶段标记为完成并推进到下一阶段；已到末步则停在末步。

        返回推进后的 `current`（便于测试与观察）。成功推进时清除上一次的失败标记
        （模型重试后成功，不应还背着旧失败）。
        """
        if self.current < self.total:
            self.current += 1
        self.failed = False
        self.failure = ""
        return self.current

    def mark_failed(self, reason: str = "") -> None:
        """标记当前阶段失败：**不推进**，后续阶段不会被静默标为完成。"""
        if not self.done:
            self.failed = True
            self.failure = reason

    def context(self, planner: Any = None) -> str:
        """渲染当前阶段上下文（优先走 planner.context，保持与注入口径一致）。"""
        if planner is not None and hasattr(planner, "context"):
            return str(planner.context(self.plan, self.current))
        return plan_as_context(self.plan, self.current)

    def to_dict(self) -> dict[str, Any]:
        """可观察/可持久化的进度快照（每步标题 + 是否已完成）。"""
        steps = [
            {
                "index": idx,
                "title": str(getattr(step, "title", step)),
                "done": idx < self.current,
            }
            for idx, step in enumerate(self._steps()[: self.total])
        ]
        return {
            "total": self.total,
            "current": self.current,
            "done": self.done,
            "failed": self.failed,
            "failure": self.failure,
            "steps": steps,
        }


def make_plan_state(
    planner: Any, user_text: str, *, max_steps: int = 5,
) -> PlanState | None:
    """让 planner 为任务产出规划，并包成可推进的 `PlanState`。

    简单任务（planner 判为非复杂）或无 planner 返回 None —— 保持基础 loop 不变。
    planner 抛异常也返回 None（规划失败不该拖垮主循环）。
    """
    if planner is None:
        return None
    try:
        plan = planner.build(user_text)
    except Exception:  # noqa: BLE001 - 规划不可用绝不拖垮主循环
        return None
    if plan is None or not getattr(plan, "is_complex", False):
        return None
    if not getattr(plan, "steps", None):
        return None
    return PlanState(plan=plan, max_steps=max_steps)


__all__ = [
    "PlanStep", "TaskPlan", "PlanState", "build_plan", "plan_as_context",
    "is_complex", "make_plan_state",
]

# 对外暴露复杂度判定（供测试 / 其他模块复核用）
def is_complex(text: str) -> bool:
    """公开的复杂度判定入口。"""
    return _complexity(text)


# ---- 模型驱动的阶段规划（深度③升级：阶段内容由模型生成）----

# 结构化输出 schema：让模型按 JSON 返回一组阶段
_PLAN_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "阶段名（简短，如『调研资料』）"},
                    "goal": {"type": "string",
                             "description": "该阶段要完成的目标（给模型当导航）"},
                },
                "required": ["title", "goal"],
            },
        }
    },
    "required": ["steps"],
}


def plan_with_model(user_text: str, model: AgentChatModel,
                    max_steps: int = 5) -> TaskPlan:
    """复杂度判定为"复杂"后，让**模型自己生成阶段**（而非固定模板）。

    - 复杂度判定仍用离线启发式 `is_complex`（不依赖模型、可测、省一次调用）；
      只有"复杂"才值得让模型去细化阶段。
    - 用模型的结构化输出能力（ChatRequest.structured_output）要一个
      `{"steps":[{"title","goal"},...]}` 的 JSON，把阶段交给模型去拆。
    - **降级**：模型没返回合法 JSON / 返回空 / 抛出异常时，回退到 `build_plan`
      的通用模板，保证离线、断网、弱模型下也能跑（不崩、可测）。
    - 阶段数限制 `max_steps`，防止模型拆出几十步占爆上下文。
    """
    fallback = build_plan(user_text)  # 模板兜底
    if not fallback.is_complex:
        return fallback
    try:
        from warden_agent.model.model import ChatRequest, Message

        req = ChatRequest(
            messages=[
                Message(role="system", content=(
                    "把下面这个任务拆成若干可逐步执行的阶段，"
                    "只输出 JSON：{\"steps\":[{\"title\":\"阶段名\",\"goal\":\"阶段目标\"}]}。"
                )),
                Message(role="user", content=user_text),
            ],
            structured_output=_PLAN_JSON_SCHEMA,
        )
        resp = model.chat(req)
    except Exception:  # noqa: BLE001 - 模型不可用就退回模板
        return fallback
    steps = _parse_plan_steps(resp, max_steps)
    if not steps:
        return fallback
    summary = "识别为复杂任务，模型拆分为阶段：" + " → ".join(s.title for s in steps)
    return TaskPlan(is_complex=True, steps=steps, summary=summary)


def _parse_plan_steps(resp: Any, max_steps: int) -> list[PlanStep]:
    """从模型响应里稳健地抽出规划阶段；解析失败返回空列表（触发降级）。"""
    import json

    content = getattr(resp, "content", None)
    if not content or not isinstance(content, str):
        # 结构化输出可能挂在别处，这里兜底尝试 JSON
        return []
    try:
        data = json.loads(content)
        if not isinstance(data, dict):
            return []
        raw = data.get("steps") or []
    except (json.JSONDecodeError, AttributeError):
        return []
    steps: list[PlanStep] = []
    for item in raw[:max_steps]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        goal = str(item.get("goal", "")).strip()
        if title and goal:
            steps.append(PlanStep(title=title, goal=goal))
    return steps


__all__ = [
    "PlanStep", "TaskPlan", "PlanState", "build_plan", "plan_as_context",
    "is_complex", "make_plan_state", "plan_with_model", "ModelPlanner",
]


class ModelPlanner:
    """可挂进 AgentLoop 的"模型驱动规划器"（深度③升级）。

    实现了 AgentLoop 期望的 `build(user_text) / context(plan, current)` 接口，
    内部用 `plan_with_model` 让模型生成阶段，失败自动降级回模板。
    离线时也可传一个返回固定泛化模板的桩（保持可测）。
    """

    def __init__(self, model: AgentChatModel, max_steps: int = 5) -> None:
        self._model = model
        self._max_steps = max_steps

    def build(self, user_text: str) -> TaskPlan:
        return plan_with_model(user_text, self._model, max_steps=self._max_steps)

    def context(self, plan: TaskPlan, current: int) -> str:
        return plan_as_context(plan, current)


