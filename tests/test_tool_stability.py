"""工具调用稳定性层测试：超时 / 指数退避重试 / 降级兜底。"""
import time

from warden_agent.tool.catalog import FunctionToolSpec
from warden_agent.tool.stability import StabilityConfig, StableToolExecutor


def _tool(fn, *, pure: bool = False, name: str = "tool.call") -> FunctionToolSpec:
    return FunctionToolSpec(
        name=name, description="", pure=pure, parameters_schema={}, function=fn)


# ---- 指数退避重试 ----

def test_瞬时故障_指数退避后重试成功() -> None:
    calls = {"n": 0}
    timestamps: list[float] = []

    def flaky() -> str:
        calls["n"] += 1
        timestamps.append(time.monotonic())
        if calls["n"] < 3:
            raise ConnectionError("瞬时抖动")
        return "ok"

    r = StableToolExecutor(StabilityConfig(
        max_attempts=3, backoff_base=0.2, backoff_max=1.0)).execute(_tool(flaky), {})
    assert r.result == "ok"
    assert r.attempts == 3
    assert calls["n"] == 3
    # 退避间隔应递增（第1→2次 ~0.2s, 第2→3次 ~0.4s）；宽松断言"后一个间隔更大"
    gap1 = timestamps[1] - timestamps[0]
    gap2 = timestamps[2] - timestamps[1]
    assert gap2 > gap1, f"退避应递增: gap1={gap1:.2f} gap2={gap2:.2f}"


def test_重试次数逐一递增() -> None:
    calls = {"n": 0}

    def always_fail() -> str:
        calls["n"] += 1
        raise ConnectionError("一直抖动")

    r = StableToolExecutor(StabilityConfig(
        max_attempts=4, backoff_base=0.01)).execute(_tool(always_fail), {})
    assert r.error is not None
    assert calls["n"] == 4
    assert r.attempts == 4


# ---- 非瞬时错误不重试 ----

def test_非瞬时错误_不重试() -> None:
    calls = {"n": 0}

    def bad() -> str:
        calls["n"] += 1
        raise ValueError("参数错误")

    r = StableToolExecutor(StabilityConfig(
        max_attempts=3, backoff_base=0.01)).execute(_tool(bad), {})
    assert calls["n"] == 1  # 只跑一次（ValueError 不是瞬时故障，不重试）
    assert "ValueError" in (r.error or "")


# ---- pure 语义 ----

def test_pure工具_额外允许重试() -> None:
    """pure 工具即使抛任意错误,retryable_pure=True 也会重试（无副作用,安全）。"""
    calls = {"n": 0}

    def flaky_pure() -> str:
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("偶发失败(非瞬时,但 pure 安全)")
        return "纯函数结果"

    r = StableToolExecutor(StabilityConfig(
        max_attempts=3, backoff_base=0.01)).execute(_tool(flaky_pure, pure=True), {})
    assert r.result == "纯函数结果"
    assert calls["n"] == 2


def test_非pure_非瞬时_不重试() -> None:
    calls = {"n": 0}

    def impure_bad() -> str:
        calls["n"] += 1
        raise RuntimeError("有副作用,不重试")

    r = StableToolExecutor(StabilityConfig(
        max_attempts=3, backoff_base=0.01)).execute(_tool(impure_bad, pure=False), {})
    assert calls["n"] == 1
    assert r.error is not None


# ---- 超时 ----

def test_慢工具_超时返回不卡死() -> None:
    def slow() -> str:
        time.sleep(5)
        return "done"

    t0 = time.monotonic()
    r = StableToolExecutor(StabilityConfig(
        timeout_seconds=0.2, max_attempts=1)).execute(_tool(slow), {})
    elapsed = time.monotonic() - t0
    assert r.timed_out is True
    assert "超时" in (r.error or "")
    assert elapsed < 2.0, f"应在时限内返回,实际 {elapsed:.2f}s"


def test_pure工具_超时算瞬时故障_会重试() -> None:
    """pure（无副作用）工具超时可以安全重试。"""
    calls = {"n": 0}

    def slow() -> str:
        calls["n"] += 1
        time.sleep(0.15)
        return "ok"

    r = StableToolExecutor(StabilityConfig(
        timeout_seconds=0.05, max_attempts=3, backoff_base=0.01)).execute(
        _tool(slow, pure=True), {})
    assert r.timed_out is True
    assert calls["n"] == 3, "pure 工具超时应重试到上限"


def test_非pure工具_超时不重试_防高危操作执行两次() -> None:
    """安全底线：超时只是"放弃等待"，被卡的那次调用仍在后台跑。

    若对非 pure 工具重试，一次人工审批会换来两次执行（如 `fs.delete` 删两次）。
    所以非 pure 工具超时必须**只执行一次**。
    """
    calls = {"n": 0}

    def slow_impure() -> str:
        calls["n"] += 1
        time.sleep(0.15)
        return "ok"

    r = StableToolExecutor(StabilityConfig(
        timeout_seconds=0.05, max_attempts=3, backoff_base=0.01)).execute(
        _tool(slow_impure, pure=False), {})
    assert r.timed_out is True
    assert calls["n"] == 1, f"非 pure 工具不得因超时重放，实际执行 {calls['n']} 次"


# ---- 卡死线程的收尾（不阻塞退出、有上限）----


def test_卡死的工作线程是daemon_不拖住进程退出() -> None:
    """ThreadPoolExecutor 的工作线程是非守护的，且会在解释器退出时被 join——
    卡死的工具会拖住进程退出。改用守护线程 + Event 后不应再有这个问题。"""
    import threading

    release = threading.Event()
    before = {t.ident for t in threading.enumerate()}

    def block_forever() -> str:
        release.wait(5)
        return "done"

    try:
        r = StableToolExecutor(StabilityConfig(
            timeout_seconds=0.05, max_attempts=1)).execute(_tool(block_forever, pure=True), {})
        assert r.timed_out is True
        new_threads = [t for t in threading.enumerate() if t.ident not in before]
        assert new_threads, "应当看到一条被放弃的工作线程"
        assert all(t.daemon for t in new_threads), "工作线程必须是 daemon"
    finally:
        release.set()  # 放行，别把线程一直挂着


def test_卡死线程数达上限后拒绝新调用(monkeypatch) -> None:
    """卡死的工具被反复调用时，宁可拒绝新调用，也不让线程无界增长。

    直接把计数置到上限来测这条守卫（不依赖"真的造出卡死线程再数"，
    那样会受先前用例遗留线程的干扰而变得不确定）。
    """
    from warden_agent.tool import stability as st

    monkeypatch.setattr(st, "_MAX_STUCK_THREADS", 2)
    monkeypatch.setattr(st, "_stuck_threads", 2)

    r = StableToolExecutor(StabilityConfig(
        timeout_seconds=0.05, max_attempts=1)).execute(_tool(lambda: "ok", pure=True), {})
    assert r.timed_out is False
    assert "拒绝再启动" in (r.error or "")


def test_超时会把该次记为卡死(monkeypatch) -> None:
    import threading

    from warden_agent.tool import stability as st

    monkeypatch.setattr(st, "_stuck_threads", 0)
    release = threading.Event()

    def block_forever() -> str:
        release.wait(5)
        return "done"

    try:
        r = StableToolExecutor(StabilityConfig(
            timeout_seconds=0.02, max_attempts=1)).execute(_tool(block_forever, pure=True), {})
        assert r.timed_out is True
        assert st._stuck_threads >= 1, "超时放弃的那次应被记为卡死"
    finally:
        release.set()


# ---- 降级兜底 ----

def test_重试耗尽_走fallback降级() -> None:
    calls = {"n": 0}

    def always_fail() -> str:
        calls["n"] += 1
        raise ConnectionError("一直失败")

    r = StableToolExecutor(StabilityConfig(
        max_attempts=2, backoff_base=0.01,
        fallback=lambda: "默认兜底数据")).execute(_tool(always_fail), {})
    assert r.degraded is True
    assert "[降级]" in str(r.result)
    assert "默认兜底数据" in str(r.result)


# ---- 关闭时不改变行为 ----

def test_默认配置_直接执行_不重试不超时() -> None:
    """StabilityConfig() 全部关闭:max_attempts=1、timeout=0 → 就是一次普通调用。"""
    calls = {"n": 0}

    def normal() -> str:
        calls["n"] += 1
        return "普通结果"

    r = StableToolExecutor(StabilityConfig()).execute(_tool(normal), {})
    assert r.result == "普通结果"
    assert calls["n"] == 1
    assert r.error is None


def test_与catalog_execute等价_关闭时() -> None:
    def fn() -> str:
        return "hi"
    spec = _tool(fn)
    assert spec.function is not None
    r = StableToolExecutor(StabilityConfig()).execute(spec, {})
    assert r.result == "hi"


# ---- 接入 AgentLoop ----

def test_loop_启用稳定性_慢工具超时不卡死() -> None:
    from tests.conftest import ScriptedModel

    from warden_agent.loop.loop import AgentLoop
    from warden_agent.model.model import ChatResponse, ToolCall
    from warden_agent.tool.catalog import ToolCatalog, function_tool

    @function_tool("slow.tool", "慢工具", {"type": "object", "properties": {}}, pure=True)
    def slow() -> str:
        time.sleep(5)
        return "done"

    catalog = ToolCatalog()
    catalog.register(slow)
    stability = StableToolExecutor(StabilityConfig(timeout_seconds=0.2, max_attempts=1))
    model = ScriptedModel([
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="c1", name="slow.tool", arguments={})], finish_reason="tool_calls"),
        ChatResponse(content="工具超时,我直接回答。", finish_reason="stop"),
    ])
    t0 = time.monotonic()
    reply = AgentLoop(model=model, catalog=catalog, stability=stability).run("测试")
    elapsed = time.monotonic() - t0
    assert "直接回答" in reply.text
    assert elapsed < 2.0, f"不应被慢工具卡死,实际 {elapsed:.2f}s"
    assert any("超时" in m.content for m in reply.messages if m.role == "tool")


def test_loop_未启用稳定性_行为不变() -> None:
    """不传 stability → 普通执行,无超时无重试。"""
    from tests.conftest import ScriptedModel

    from warden_agent.loop.loop import AgentLoop
    from warden_agent.model.model import ChatResponse, ToolCall
    from warden_agent.tool.catalog import ToolCatalog, function_tool

    @function_tool("ok.tool", "正常", {"type": "object",
                                       "properties": {"x": {"type": "string"}},
                                       "required": ["x"]}, pure=True)
    def ok(x: str) -> str:
        return f"收到{x}"

    catalog = ToolCatalog()
    catalog.register(ok)
    model = ScriptedModel([
        ChatResponse(content=None, tool_calls=[
            ToolCall(id="c1", name="ok.tool", arguments={"x": "hi"})], finish_reason="tool_calls"),
        ChatResponse(content="完成。", finish_reason="stop"),
    ])
    reply = AgentLoop(model=model, catalog=catalog).run("测")
    assert "完成" in reply.text
    assert any("收到hi" in m.content for m in reply.messages if m.role == "tool")


# ---- 熔断器（circuit breaker）----

def _named_tool(fn, name: str, *, pure: bool = True) -> FunctionToolSpec:
    return FunctionToolSpec(
        name=name, description="", pure=pure, parameters_schema={}, function=fn)


def test_连续失败达到阈值_触发熔断并短路() -> None:
    calls = {"n": 0}

    def always_fail() -> str:
        calls["n"] += 1
        raise ConnectionError("一直失败")

    spec = _named_tool(always_fail, "cb.tool")
    ex = StableToolExecutor(StabilityConfig(
        circuit_threshold=2, circuit_cooldown=5.0, max_attempts=1))

    r1 = ex.execute(spec, {})
    r2 = ex.execute(spec, {})
    assert r1.error is not None and r2.error is not None
    # 第 3 次:已熔断,不再真调用(计数没涨),返回熔断降级结果
    r3 = ex.execute(spec, {})
    assert calls["n"] == 2, f"第3次不应真调用,实际 {calls['n']}"
    assert r3.degraded is True
    assert "[熔断]" in str(r3.result)


def test_熔断期间跳过重试() -> None:
    """熔断时不仅不调用,也不走重试/退避——直接返回熔断信号。"""
    calls = {"n": 0}

    def always_fail() -> str:
        calls["n"] += 1
        raise ConnectionError("boom")

    spec = _named_tool(always_fail, "cb.skip")
    # max_attempts=3（本会重试）,但熔断优先:短路期直接返回,不触发那3次尝试
    ex = StableToolExecutor(StabilityConfig(
        circuit_threshold=2, circuit_cooldown=5.0, max_attempts=3, backoff_base=0.01))

    # max_attempts=3 → 每次 execute 内部已重试3次;2次失败达到阈值熔断
    ex.execute(spec, {})
    ex.execute(spec, {})
    assert calls["n"] == 6  # 2 * 3 次内部尝试
    before = calls["n"]
    r = ex.execute(spec, {})                      # 熔断期:不调用、不重试
    assert r.degraded is True
    assert calls["n"] == before, "熔断期不应产生任何尝试"


def test_冷却后半开试探成功_关闭熔断() -> None:
    state = {"fail": True}
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if state["fail"]:
            raise ConnectionError("boom")
        return "恢复了"

    spec = _named_tool(flaky, "cb.halfopen")
    ex = StableToolExecutor(StabilityConfig(
        circuit_threshold=2, circuit_cooldown=0.2, max_attempts=1))

    ex.execute(spec, {})
    ex.execute(spec, {}) # 熔断
    assert calls["n"] == 2
    # 熔断期内仍是熔断
    r_blocked = ex.execute(spec, {})
    assert r_blocked.degraded and "[熔断]" in str(r_blocked.result)
    # 服务恢复 + 冷却期过 → 半开试探成功
    state["fail"] = False
    time.sleep(0.25)
    r_probe = ex.execute(spec, {})
    assert r_probe.result == "恢复了"
    # 熔断已关闭:后续正常调用
    r_after = ex.execute(spec, {})
    assert r_after.result == "恢复了"


def test_半开试探失败_重新熔断() -> None:
    calls = {"n": 0}

    def always_fail() -> str:
        calls["n"] += 1
        raise ConnectionError("boom")

    spec = _named_tool(always_fail, "cb.reopen")
    ex = StableToolExecutor(StabilityConfig(
        circuit_threshold=2, circuit_cooldown=0.2, max_attempts=1))

    ex.execute(spec, {})
    ex.execute(spec, {}) # 熔断
    assert calls["n"] == 2
    time.sleep(0.25)                             # 冷却过 → 半开
    ex.execute(spec, {})                       # 半开试探,仍失败 → 重新熔断
    assert calls["n"] == 3                        # 试探真调用了一次
    r2 = ex.execute(spec, {})                     # 又熔断:不调用
    assert r2.degraded and "[熔断]" in str(r2.result)
    assert calls["n"] == 3


def test_不同工具熔断隔离() -> None:
    calls_a = {"n": 0}
    calls_b = {"n": 0}

    def fail_a() -> str:
        calls_a["n"] += 1
        raise ConnectionError("a炸")

    def ok_b() -> str:
        calls_b["n"] += 1
        return "b正常"

    spec_a = _named_tool(fail_a, "cb.a")
    spec_b = _named_tool(ok_b, "cb.b")
    ex = StableToolExecutor(StabilityConfig(
        circuit_threshold=2, circuit_cooldown=5.0, max_attempts=1))

    ex.execute(spec_a, {})
    ex.execute(spec_a, {}) # a 熔断
    # b 不受影响,正常执行
    r = ex.execute(spec_b, {})
    assert r.result == "b正常"
    assert calls_b["n"] == 1
    # a 已熔断,不调用
    ra = ex.execute(spec_a, {})
    assert ra.degraded and "[熔断]" in str(ra.result)
    assert calls_a["n"] == 2


def test_默认不启用熔断() -> None:
    """circuit_threshold=0(默认)→ 无熔断,连续失败每次都真调用。"""
    calls = {"n": 0}

    def always_fail() -> str:
        calls["n"] += 1
        raise ConnectionError("boom")

    spec = _named_tool(always_fail, "cb.off")
    ex = StableToolExecutor(StabilityConfig(max_attempts=1))  # 未配熔断
    for _ in range(3):
        ex.execute(spec, {})
    assert calls["n"] == 3, "默认不熔断,每次都真调用"
