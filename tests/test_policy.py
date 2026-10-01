"""审批策略测试。

变异测试（`[tool.mutmut]` 圈定的三个安全关键模块之一）：本文件守的是
「审批判定写错 = 越权直接执行」这条风险，对应实现 `policy/policy.py`。

**杀不掉的等价变异**（改了与没改行为相同，因此**不是**测试缺口，别去补测）汇总在
`docs/mutation.md` 的「等价变异」表，属于本文件的有 4 条：
`arguments.get("path", "")` 的默认值换成 None / 换成 "XXXX"、`pure = False` 换成 `None`、
`rank[Decision.DENY]` 从 2 换成 3。看到 mutmut 报它们"存活"时，要对照那张表确认再下结论。
"""
import logging

import pytest
from tests.conftest import ScriptedModel, weather_tool

import warden_agent.policy.policy as policy_mod
from warden_agent.loop.loop import AgentLoop
from warden_agent.model.model import ChatResponse, ToolCall
from warden_agent.policy.policy import (
    ApprovalRequired,
    Decision,
    PolicyDenied,
    PolicyEngine,
    PolicyResult,
    _path_parts,
    ask_when_tool_not_pure,
    deny_when_path_in_protected,
)


def test_deny比ask优先级高() -> None:
    """同一动作，一条说 ASK、一条说 DENY，最终取最严的 DENY。"""
    engine = PolicyEngine()
    engine.add(lambda name, args: PolicyResult(Decision.ASK, "ask"))
    engine.add(lambda name, args: PolicyResult(Decision.DENY, "deny"))
    result = engine.evaluate("x", {})
    assert result.decision == Decision.DENY


def test_ask策略_挂起等批准_绝不执行() -> None:
    """ASK 不再被当成 EXECUTE：loop 挂起并抛 ApprovalRequired，工具绝不执行。

    与产品路径（runtime/session.py 的 _hold_for_approval）语义一致。
    """
    engine = PolicyEngine()
    engine.add(lambda name, args: PolicyResult(Decision.ASK, "需人工批准"))
    model = ScriptedModel([
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="c1", name="weather.get", arguments={"city": "上海"})],
            finish_reason="tool_calls"),
        ChatResponse(content="天晴。", finish_reason="stop"),
    ])
    loop = AgentLoop(model=model, catalog=weather_tool(), policy_engine=engine)
    with pytest.raises(ApprovalRequired) as exc:
        loop.run("查天气")
    assert exc.value.tool_name == "weather.get"
    assert exc.value.arguments == {"city": "上海"}
    # 工具没被执行：模型只被调用了一次（挂起后不再继续）
    assert model.calls == 1


def test_deny策略_直接拒绝执行() -> None:
    """模型想调被 DENY 的工具：抛 PolicyDenied，工具绝不执行。"""
    engine = PolicyEngine()
    engine.add(lambda name, args: PolicyResult(Decision.DENY, "禁止"))
    model = ScriptedModel([
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="c1", name="weather.get", arguments={"city": "上海"})],
            finish_reason="tool_calls"),
    ])
    with pytest.raises(PolicyDenied):
        AgentLoop(model=model, catalog=weather_tool(), policy_engine=engine).run("hi")


# ---- 零规则默认 + 逐规则异常隔离 ----

def test_零规则_默认放行() -> None:
    """没有配置任何策略时，门禁默认放行（刻意固定的默认，不是漏配）。"""
    result = PolicyEngine().evaluate("anything", {"x": 1})
    assert result.decision == Decision.ALLOW
    assert "默认放行" in result.reason


def test_单条规则抛异常_不击穿门禁_按最严处理() -> None:
    """某条规则自身写错抛异常时，门禁不崩溃，且按最严(DENY)处理（fail-closed）。"""
    def boom(name: str, args: dict) -> PolicyResult:
        raise ValueError("规则写错了")

    engine = PolicyEngine()
    engine.add(boom)
    engine.add(lambda name, args: PolicyResult(Decision.ALLOW, "ok"))
    result = engine.evaluate("x", {})
    assert result.decision == Decision.DENY
    assert "ValueError" in result.reason


def test_规则异常时日志带异常栈() -> None:
    """规则写坏了要能从日志定位（`exc_info=True`）。

    改坏了会怎样（变异测试抓到的真实情形）：`exc_info` 写成 False/None 后，日志只剩
    一行"策略规则执行异常，按最严(DENY)处理: 规则写错了"——运维知道门禁拦了，
    但**不知道是哪条规则、哪一行抛的**。门禁告警恰恰最需要栈。
    """
    def boom(name: str, args: dict) -> PolicyResult:
        raise ValueError("规则写错了")

    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    policy_logger = logging.getLogger("warden_agent.policy.policy")
    handler = _Collect()
    old_level = policy_logger.level
    policy_logger.addHandler(handler)
    policy_logger.setLevel(logging.WARNING)
    try:
        engine = PolicyEngine()
        engine.add(boom)
        assert engine.evaluate("x", {}).decision == Decision.DENY
    finally:
        policy_logger.removeHandler(handler)
        policy_logger.setLevel(old_level)

    assert records, "规则异常必须留日志"
    rendered = logging.Formatter().format(records[0])
    assert "策略规则执行异常" in rendered
    assert "Traceback" in rendered and "ValueError" in rendered, rendered


# ---- 受保护路径策略：组件级比较 ----

def test_受保护路径_子目录命中() -> None:
    policy = deny_when_path_in_protected(("/data",))
    assert policy("fs.read", {"path": "/data/secret.txt"}).decision == Decision.DENY
    assert policy("fs.read", {"path": "/data"}).decision == Decision.DENY


def test_受保护路径_前缀相似但不命中() -> None:
    """`/data-evil` 不是 `/data` 的子目录，不能被 startswith 误伤。"""
    policy = deny_when_path_in_protected(("/data",))
    assert policy("fs.read", {"path": "/data-evil"}).decision == Decision.ALLOW
    assert policy("fs.read", {"path": "/database"}).decision == Decision.ALLOW


def test_受保护路径_反斜杠也按组件识别() -> None:
    policy = deny_when_path_in_protected(("C:/Users/me",))
    assert policy("fs.read", {"path": "C:\\Users\\me\\.ssh\\id_rsa"}).decision == Decision.DENY


def test_受保护路径_无path参数放行() -> None:
    policy = deny_when_path_in_protected(("/data",))
    assert policy("weather.get", {"city": "上海"}).decision == Decision.ALLOW


def test_受保护路径_点段不参与前缀匹配() -> None:
    """`./data/x` 与 `/data/x` 是同一个位置：点段必须先丢掉，否则前缀能被绕开。

    改坏了会怎样（变异测试抓到的真实情形）：空段过滤写成只丢 `""`、把 `.` 留下时，
    `./data/…` 拆成 `(".", "data", …)`，与 `("data",)` 组件比对失败
    → **受保护目录被放行**（一个 `./` 就绕过了门禁）。
    """
    policy = deny_when_path_in_protected(("/data",))
    assert policy("fs.read", {"path": "/data/./secret.txt"}).decision == Decision.DENY
    assert policy("fs.read", {"path": "./data/secret.txt"}).decision == Decision.DENY


# ---- "非 pure 默认要人工批准"规则：方向的修正（黑名单 → 默认拦）----

_PURE = {"weather.get", "memory.recall"}


def _is_pure(name: str) -> bool:
    return name in _PURE


def test_非pure工具默认要人工批准() -> None:
    """`shell.run` / `git.apply_patch` 这类**没声明 pure** 的工具默认落 ASK。

    这一条守的是那个真实缺陷：改前是黑名单（只拦 `fs.delete`），于是
    `shell.run`（能在沙箱里跑任意命令）和 `git.apply_patch`（写仓库）**直接放行**。
    """
    policy = ask_when_tool_not_pure(_is_pure)
    assert policy("shell.run", {"command": "rm -rf /"}).decision == Decision.ASK
    assert policy("git.apply_patch", {}).decision == Decision.ASK
    assert policy("mcp.some_remote_tool", {}).decision == Decision.ASK


def test_pure工具放行() -> None:
    policy = ask_when_tool_not_pure(_is_pure)
    assert policy("weather.get", {"city": "上海"}).decision == Decision.ALLOW
    assert policy("memory.recall", {"key": "k"}).decision == Decision.ALLOW


def test_查不到工具也按非pure处理() -> None:
    """未注册的名字（或查目录报错）也要 ASK —— fail-closed，不能因"查不到"而放行。"""
    def boom(name: str) -> bool:
        raise KeyError(name)

    policy = ask_when_tool_not_pure(boom)
    assert policy("未知工具", {}).decision == Decision.ASK


def test_exempt_名单里的工具放行() -> None:
    policy = ask_when_tool_not_pure(_is_pure, exempt=frozenset({"memory.remember"}))
    assert policy("memory.remember", {"key": "k", "text": "t"}).decision == Decision.ALLOW
    # 豁免是**收窄**白名单：名单外的非 pure 工具照旧要批
    assert policy("shell.run", {"command": "x"}).decision == Decision.ASK


def test_与其它规则合成时仍然取最严() -> None:
    engine = PolicyEngine()
    engine.add(ask_when_tool_not_pure(_is_pure))
    engine.add(deny_when_path_in_protected(("C:/Windows",)))
    # 非 pure + 落到受保护路径 → DENY（DENY 比 ASK 严）
    assert engine.evaluate("fs.delete", {"path": "C:/Windows/x"}).decision == Decision.DENY
    # 非 pure 但不碰受保护路径 → ASK
    assert engine.evaluate("fs.delete", {"path": "D:/tmp/x"}).decision == Decision.ASK


# =========================================================================== #
# 理由文本、边界与平台差异：这些用例是**变异测试**（scripts/mutation.sh）逼出来的。
# 变异测试把比较符/常量/分支/日志参数逐个改掉再跑测试；测试仍绿 = 没被约束。
# 每一条都注明"改坏了会怎样"。
# =========================================================================== #


def test_零规则的理由文本固定() -> None:
    """改坏了会怎样：这条理由会直接显示在审批历史/运维界面里，
    改动它必须是有意识的决定（比如改措辞）——不能随便被改成一个没人看过的字符串。
    """
    result = PolicyEngine().evaluate("anything", {"x": 1})
    assert result.reason == "没有配置任何策略，默认放行"


def test_同严时保留先命中的那条规则的理由() -> None:
    """改坏了会怎样：`rank[x] > rank[best]` 写成 `>=`——两条规则给出**同样严重**的判定时，
    后者会覆盖前者。虽然决定（ASK）一样，但用户看到的**理由是最后一条**，
    而先命中的通常才是更具体的那条（"命中受保护路径"比"通用兜底"更有信息量）。
    """
    engine = PolicyEngine()
    engine.add(lambda name, args: PolicyResult(Decision.ASK, "第一条：具体原因"))
    engine.add(lambda name, args: PolicyResult(Decision.ASK, "第二条：兜底原因"))
    result = engine.evaluate("shell.run", {})
    assert result.decision == Decision.ASK
    assert result.reason == "第一条：具体原因"


def test_规则拿到的是真实工具名与参数() -> None:
    """改坏了会怎样：传进规则的工具名被写死成 `None`——需要按工具名判断的规则
    （豁免名单、pure 声明、askable 集合）会**全部失效并静默变成"不命中"**。
    """
    seen: list[tuple[str, dict[str, object]]] = []

    def spy(name: str, args: dict[str, object]) -> PolicyResult:
        seen.append((name, args))
        return PolicyResult(Decision.ALLOW)

    engine = PolicyEngine()
    engine.add(spy)
    engine.evaluate("shell.run", {"command": "ls"})
    assert seen == [("shell.run", {"command": "ls"})]


def test_规则异常_日志里带异常原文与调用栈(caplog: pytest.LogCaptureFixture) -> None:
    """改坏了会怎样：异常被吞成 `None`、`exc_info=True` 变成 `False`、或理由文本被改——
    运维只知道"门禁按 DENY 处理了"，**不知道该去修哪条规则**，只能猜。
    另外日志的级别与文案本身也是约定（告警规则/排障手册按它检索）。
    """
    def boom(name: str, args: dict[str, object]) -> PolicyResult:
        raise ValueError("规则写错了")

    engine = PolicyEngine()
    engine.add(boom)
    with caplog.at_level(logging.WARNING, logger="warden_agent.policy.policy"):
        result = engine.evaluate("x", {})
    assert result.decision == Decision.DENY  # fail-closed

    records = [r for r in caplog.records if r.name == "warden_agent.policy.policy"]
    assert records, "异常规则必须留一条 WARNING 日志（否则线上无法定位是哪条规则坏了）"
    message = records[0].getMessage()
    assert message.startswith("策略规则执行异常，按最严(DENY)处理:")
    assert "规则写错了" in message
    assert records[0].exc_info is not None, "不带 exc_info 的日志丢了调用栈"


def test_受保护路径_拒绝理由里写出具体路径() -> None:
    """改坏了会怎样：理由被写成 `None` 或省掉——用户只看到"被拒了"，
    却不知道**哪个路径**触发了策略，只能把整次调用的参数自己猜一遍。
    """
    policy = deny_when_path_in_protected(("/data",))
    result = policy("fs.read", {"path": "/data/secret.txt"})
    assert result.decision == Decision.DENY
    assert result.reason == "禁止访问受保护路径: /data/secret.txt"


def test_受保护路径_点段与空段先归一化再比较() -> None:
    """改坏了会怎样：拆路径时不过滤空段/`.`——`//data/x` 与 `/./data/x` 都会被拆出
    一个额外组件，于是与受保护前缀**逐组件比对时对不上**，受保护目录被绕过（漏拦）。
    """
    policy = deny_when_path_in_protected(("/data",))
    assert policy("fs.read", {"path": "/./data/x"}).decision == Decision.DENY
    assert policy("fs.read", {"path": "//data/x"}).decision == Decision.DENY


def test_路径拆分_忽略空段点段并按平台统一大小写(monkeypatch: pytest.MonkeyPatch) -> None:
    """改坏了会怎样：`os.name == "nt"` 写成 `!=`/大写/换个字符串——
    Windows 上"大小写不敏感"失效：`c:/users/ME` 绕开 `C:/Users/me` 的防护；
    反过来"永远 casefold"也错：POSIX 上路径大小写敏感，`/Data` 与 `/data` 会被混为一谈。

    ⚠️ 这里**必须**用 monkeypatch 注入平台名，不能写成 `if os.name == "nt":` 分支断言：
    那样在 Linux 上那个分支永远不执行，"Windows 语义"就从来没有被验证过
    —— 变异测试报 `x__path_parts__mutmut_13/14` 存活，照出来的正是这件事。
    """
    assert _path_parts("a//b/./c") == ("a", "b", "c")

    # Windows：盘符与每一段都折叠成小写，于是 `C:/Users/ME` 与 `c:/users/me` 是同一路径
    monkeypatch.setattr(policy_mod.os, "name", "nt")
    assert _path_parts("C:/Users") == ("c:", "users")
    assert _path_parts("C:/Users/ME") == _path_parts("c:/users/me")

    # POSIX：大小写**敏感**，不许折叠
    monkeypatch.setattr(policy_mod.os, "name", "posix")
    assert _path_parts("/Data") != _path_parts("/data")


def test_非pure规则的三条理由都写明工具名() -> None:
    """改坏了会怎样：理由被写成 `None`——审批界面只剩"需要批准"四个字，
    用户不知道该批准什么、也不知道是**哪条规则**拦的（pure 声明？豁免名单？）。
    """
    policy = ask_when_tool_not_pure(_is_pure)
    assert policy("shell.run", {}).reason == (
        "'shell.run' 未声明 pure=True，可能有副作用，需要人工批准"
    )
    assert policy("weather.get", {}).reason == "'weather.get' 声明为 pure=True（无副作用）"
    exempt_policy = ask_when_tool_not_pure(_is_pure, exempt=frozenset({"memory.remember"}))
    assert exempt_policy("memory.remember", {}).reason == "'memory.remember' 在无副作用豁免名单里"


def test_ApprovalRequired_默认理由为空且消息里带工具名() -> None:
    """改坏了会怎样：`reason` 的默认值被改成某个占位串（或反过来被写成 `None`）、
    或异常消息被写成 `None`——调用方靠 `str(exc)`/`exc.reason` 生成审批单，
    这些字段错了就是**审批单上没有工具名**（可读性直接塌掉）。
    """
    exc = ApprovalRequired("shell.run", {"command": "rm -rf /"})
    assert exc.reason == ""
    assert exc.tool_name == "shell.run"
    assert "shell.run" in str(exc)

    with_reason = ApprovalRequired("shell.run", {}, reason="非 pure 工具")
    assert with_reason.reason == "非 pure 工具"
    assert "非 pure 工具" in str(with_reason)
