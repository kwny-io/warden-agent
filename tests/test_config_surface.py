"""配置面守卫：用 AST 扫描源码，强制"环境变量必须登记、且只能被授权模块读"。

为什么需要这条测试（这是真实踩坑换来的）：
  配置读取原先散在 8 个文件、24 处 `os.environ.get(...)`，**没有任何地方声明
  "这个变量谁有权读"**。于是长出两类静默出错的 bug：

  - **一名两用**：`WARDEN_API_KEY` 被 HTTP 鉴权与 custom 模型同时读 →
    模型把服务端鉴权密钥发给第三方网关；`WARDEN_BASE_URL` 被 CLI 与 custom 模型同时读 →
    `warden chat` 把请求发到模型网关。两个都已修（拆成不同变量），但**根因是缺一份声明**。
  - **拼错静默忽略**：`WARDEN_RATE_LIMIT` 敲成 `WARDEN_RATELIMIT` → 悄悄用默认值。

  所以本文件把 `core/settings.py` 的注册表变成**可执行的约束**：
    1. 代码里读的每个环境变量，都必须在注册表里（否则新增变量会漏登记）；
    2. 读它的模块，不能超出注册表声明的 `consumers`（否则就是"越权读别人的变量"，
       也就是上面「一名两用」的形状）；
    3. 拼错的 `WARDEN_*` 变量要能被识别出来；
    4. **文档里写的 `WARDEN_*` 必须都登记过**（反向：文档不能承诺一个不存在的旋钮），
       并且 README 自称的那个变量数必须等于登记表条数。

  没有这四条，注册表就只是文档——而文档是不会自己变红的。

口径说明：扫描规则是「`.get("字面量")` / `["字面量"]` / `env_bool("字面量")`」，
且字面量形如环境变量名。
**模块级字符串常量也算**（`ENV_X = "WARDEN_X"` 然后 `env_bool(ENV_X)`）——这条能力是补上的：
原先只认调用处的**字面量**，于是一个把变量名写成常量的模块能静默绕过登记与越权检查
（`web/server.py` 的 `ENV_APPROVAL_ALLOW_SELF` 就是这么漏掉的），而**能静默绕过的守卫**
比没有守卫更坑（它让人相信"每个变量都登记了"）。
局部变量仍扫不到（`name = os.environ.get("K"); env_bool(name)` 这种就别写）。
若将来出现同形状、但**不是**环境变量的全大写常量，加进 `_NON_ENV_CONSTANTS` 即可。

注：本文件里的"密钥"全是**运行时拼出来的假值**，源码里刻意不出现任何像凭据的字面量。
"""

from __future__ import annotations

import ast
import re
import secrets
from pathlib import Path

import pytest

from warden_agent.core.settings import (
    ENV_SPECS,
    describe,
    registered_env_names,
    unknown_warden_variables,
    validate_env,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "warden_agent"

# 形如环境变量名的字符串常量
_ENV_LIKE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")
# 同形状但确实不是环境变量的常量（逃生舱；目前为空）
_NON_ENV_CONSTANTS: frozenset[str] = frozenset()
# `core/settings.py` 里的类型化访问器：`env_int("PORT", 8000)` 也是"读了这个变量"，
# 守卫必须认（否则模块改用访问器之后，守卫就"看不见"它读了什么，等于自废武功）。
_ENV_ACCESSORS = frozenset({
    "env_str", "env_opt", "env_int", "env_bool", "env_positive_int", "secret_bytes",
})


def _fake_value(tag: str) -> str:
    """运行时拼出的假值（避免在源码里写任何像凭据/密钥的字面量）。"""
    return f"faux-{tag}-" + secrets.token_hex(8)


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """收集模块级字符串常量：`ENV_X = "WARDEN_X"` / `ENV_X: str = "WARDEN_X"`。

    为什么要收：把变量名写成常量再传给 helper（`env_bool(ENV_APPROVAL_ALLOW_SELF)`）
    曾经能从守卫眼底滑过去——它当时只看调用处的字面量。

    前缀式常量（`PROJECT_ENV_PREFIX = "WARDEN_"`）刻意不算：它是前缀不是变量名，
    当成变量名去登记表里找只会得到误报。
    """
    consts: dict[str, str] = {}
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            continue
        if not _ENV_LIKE.match(value.value) or value.value.endswith("_"):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                consts[target.id] = value.value
    return consts


def _read_literal(node: ast.AST, consts: dict[str, str] | None = None) -> str | None:
    """判断这个 AST 节点是否在"读某个名字的环境变量"，是则返回变量名字面量。

    覆盖三种读法：
      - `x.get("NAME")`（`x` 可能是 `os.environ`，也可能是注入进来的 Mapping）
      - `x["NAME"]`
      - 类型化访问器 `env_int("NAME", ...)` / `settings.env_bool("NAME")`
    第一个参数也可以是**模块级字符串常量**（传 `consts` 时才能解析）。
    """
    if isinstance(node, ast.Call) and node.args:
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            literal = first.value
        elif isinstance(first, ast.Name) and consts is not None and first.id in consts:
            literal = consts[first.id]
        else:
            return None
        func = node.func
        is_get = isinstance(func, ast.Attribute) and func.attr == "get"
        is_accessor = (
            (isinstance(func, ast.Name) and func.id in _ENV_ACCESSORS)
            or (isinstance(func, ast.Attribute) and func.attr in _ENV_ACCESSORS)
        )
        if is_get or is_accessor:
            return literal
        return None
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    ):
        return node.slice.value
    return None


def _collect_env_reads() -> dict[str, set[str]]:
    """扫描源码，返回 {变量名: {读它的模块相对路径}}（读法见 `_read_literal`）。"""
    reads: dict[str, set[str]] = {}
    for path in SRC.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - 语法错误由 ruff/mypy 先拦
            continue
        rel = path.relative_to(SRC).as_posix()
        consts = _module_constants(tree)
        for node in ast.walk(tree):
            literal = _read_literal(node, consts)
            if literal and _ENV_LIKE.match(literal) and literal not in _NON_ENV_CONSTANTS:
                reads.setdefault(literal, set()).add(rel)
    return reads


@pytest.fixture(scope="module")
def env_reads() -> dict[str, set[str]]:
    return _collect_env_reads()


def test_代码读取的环境变量都已登记(env_reads: dict[str, set[str]]) -> None:
    """新增环境变量必须在 `core/settings.py` 的 ENV_SPECS 里登记。

    没登记 = 没人知道它存在、没人知道谁该读它 —— 正是 bug 的温床。
    """
    unregistered = sorted(set(env_reads) - registered_env_names())
    assert not unregistered, (
        "以下环境变量被代码读取，但没在 core/settings.py 的 ENV_SPECS 里登记：\n"
        + "\n".join(f"  - {name}（读取模块：{sorted(env_reads[name])}）" for name in unregistered)
        + "\n请补上登记（用途 / 归属模块 / 允许读取的模块）。"
    )


def test_只有授权模块能读某个变量(env_reads: dict[str, set[str]]) -> None:
    """**冲突检测的落点**：读某变量的模块必须 ⊆ 注册表声明的 consumers。

    这条就是用来拦住「一名两用」的——当初 `cli.py` 去读 model 的 `WARDEN_BASE_URL`、
    `deepseek.py` 去读鉴权的 `WARDEN_API_KEY`，都会在这里被拦下。
    """
    violations: list[str] = []
    for spec in ENV_SPECS:
        actual = env_reads.get(spec.name, set())
        allowed = set(spec.consumers)
        extra = sorted(actual - allowed)
        if extra:
            violations.append(
                f"  - {spec.name}（归属 {spec.owner}）：被未授权的 {extra} 读取；"
                f"已声明的 consumers={sorted(allowed)}"
            )
    assert not violations, (
        "有模块读取了「不属于它」的环境变量——这就是同名两用 bug 的形状。\n"
        "要么改代码（用正确的变量），要么在 ENV_SPECS 里显式加进 consumers 并说明理由：\n"
        + "\n".join(violations)
    )


def test_注册表里声明的变量确实被代码用到(env_reads: dict[str, set[str]]) -> None:
    """反向检查：登记了却没人读，说明要么拼错了、要么代码已经不用了。"""
    stale = sorted(name for name in registered_env_names() if name not in env_reads)
    assert not stale, f"以下变量已登记但代码里没有任何读取点（拼错？或已废弃？）：{stale}"


def test_注册表无重复且消费者非空() -> None:
    names = [spec.name for spec in ENV_SPECS]
    assert len(names) == len(set(names)), "ENV_SPECS 里有重复登记"
    for spec in ENV_SPECS:
        assert spec.consumers, f"{spec.name} 没有声明任何 consumers"
        assert spec.purpose and spec.owner, f"{spec.name} 缺 purpose/owner"


# ---------- 第四个方向：文档 ↔ 注册表 ----------
#
# 前面三条守卫都是"代码 ↔ 注册表"（代码读的必须已登记 / 不能越权读 / 登记了得有人读），
# 它们**盖不住**"文档承诺了一个不存在的旋钮"：
#   · 登记表里没有它 ⇒ 方向一（代码读的必须已登记）不适用；
#   · 也没人读，所以方向三（登记了没用）同样不适用。
#
# 两个真实踩到的例子（都是"照着文档去查、什么也查不到"）：
#   1. `README.md` 与 `docs/deployment-boundaries.md` 把 `WARDEN_SSE_HEARTBEAT_SECONDS`
#      写成"可调"，而它在代码里**读都没被读过**（只是 `web/server.py` 的一个模块常量）：
#      操作者设了不生效、启动日志也不提，代理把长连接当死连接掉的时候只会觉得"偶尔断"。
#   2. `docs/复核索引.md` 曾把 e2e 的离线保证写成"子进程带 `WARDEN_SKIP_DOTENV=1`"，
#      而这个变量**在 `src/` 与 `web/` 里从未存在过**（看代码面：`git log -S WARDEN_SKIP_DOTENV`
#      `-- src/ web/` 为空 —— **必须带路径限定**，不带路径会命中两个只动文档的提交）；
#      真实保证是 `DEEPSEEK_API_KEY=''` + `load_env()` 不覆盖已存在的键）。
#
# 口径：扫 README.md 与 docs/**/*.md 里形如 `WARDEN_XXX` 的记号；
#   · 以 `*` 结尾的（`WARDEN_OUTBOUND_*` 这类**变量族**写法）放行，它本就不是单个变量名；
#   · 其余必须在注册表里；文档里刻意举的假名进 `_DOC_ONLY_NAMES` 并写明理由。
_DOC_ONLY_NAMES: dict[str, str] = {
    # README 用它演示"把变量名写成模块级常量"的 AST 形状（`ENV_X = "WARDEN_X"`）
    # 是个**刻意不存在**的名字。
    "WARDEN_X": "README 里 `ENV_X = \"WARDEN_X\"` 的示例常量，故意不对应真变量",
    # 这一条是**反向**用法：文档写它，恰恰是为了记录"它从未存在过"。
    "WARDEN_SKIP_DOTENV": "`docs/复核索引.md` 用它做更正说明（曾被误写成 e2e 的挡法）",
    # 同上：第四十八批的启动探针**自己发明**了这个名字（服务没凭据时 fail-closed，
    # 探针拿一个自造变量名去测，于是"没看到日志"成了假象）。文档写它同样是为了记录这个坑。
    "WARDEN_STORE": "`docs/复核索引.md` 用它记录探针自造变量名的坑（从未存在过）",
}
_DOC_GLOBS = ("README.md", "docs/**/*.md")
_DOC_TOKEN = re.compile(r"WARDEN_[A-Z0-9_]+\*?")


def _doc_mentions() -> dict[str, set[str]]:
    """文档里提到的 `WARDEN_*` 记号 → 它出现在哪些文档。"""
    root = Path(__file__).resolve().parents[1]
    mentions: dict[str, set[str]] = {}
    for pattern in _DOC_GLOBS:
        for path in sorted(root.glob(pattern)):
            for found in _DOC_TOKEN.findall(path.read_text(encoding="utf-8")):
                if found.endswith("*"):
                    continue  # 变量族写法
                mentions.setdefault(found, set()).add(
                    str(path.relative_to(root)).replace("\\", "/")
                )
    return mentions


def test_文档提到的环境变量都已登记() -> None:
    """文档说"可调"的旋钮，登记表里必须真有——否则就是一句空承诺。"""
    registered = registered_env_names()
    unknown = {
        name: sorted(where)
        for name, where in _doc_mentions().items()
        if name not in registered and name not in _DOC_ONLY_NAMES
    }
    assert not unknown, (
        "以下名字只出现在文档里、没在 `core/settings.py` 登记"
        "（要么登记 + 真的接线，要么改文档；文档里刻意举的假名进 `_DOC_ONLY_NAMES` 并写明理由）：\n"
        + "\n".join(f"  {n}  ← {', '.join(w)}" for n, w in sorted(unknown.items()))
    )


def test_文档放行名单里的名字都不该是真变量() -> None:
    """逃生舱不能反过来用：放行的名字若哪天真登记了，就该从名单里删掉。"""
    both = sorted(set(_DOC_ONLY_NAMES) & registered_env_names())
    assert not both, (
        f"这些名字既在登记表里、又在 _DOC_ONLY_NAMES 里，删掉名单里的：{both}"
    )


def test_README里的变量计数与注册表一致() -> None:
    """README 自称"全部 N 个环境变量登记在一张表里"——N 必须真的是登记表条数。

    这条计数本来是**手写**的：加一个变量就悄悄过期（发现时已经差 2 个），
    而"单一事实源"这句话本身就在那一行，恰恰最不该自相矛盾。
    """
    root = Path(__file__).resolve().parents[1]
    text = (root / "README.md").read_text(encoding="utf-8")
    found = re.search(r"全部 \*\*(\d+) 个环境变量\*\*", text)
    assert found, "README 里找不到『全部 **N 个环境变量**』这句话（改了措辞就同步改这条用例）"
    assert int(found.group(1)) == len(ENV_SPECS), (
        f"README 写 {found.group(1)} 个，登记表实际 {len(ENV_SPECS)} 个"
    )


# ---------- 校验与拼写检测 ----------


def test_限流格式写错会在启动时暴露() -> None:
    errors = validate_env({"WARDEN_RATE_LIMIT": "600次/分钟"})
    assert len(errors) == 1 and "WARDEN_RATE_LIMIT" in errors[0]


def test_整数写错会报错() -> None:
    errors = validate_env({"WARDEN_OUTBOUND_MAX_CONCURRENCY": "many"})
    assert len(errors) == 1 and "整数" in errors[0]


def test_布尔写错会报错() -> None:
    errors = validate_env({"WARDEN_AUDIT": "ture"})  # 拼错的 true
    assert len(errors) == 1 and "WARDEN_AUDIT" in errors[0]


def test_合法配置不报错() -> None:
    ok = {
        "WARDEN_RATE_LIMIT": "600/60",
        "WARDEN_OUTBOUND_MAX_CONCURRENCY": "8",
        "WARDEN_OUTBOUND_DAILY_QUOTA": "0",
        "WARDEN_AUDIT": "1",
        "WARDEN_ALLOW_ANON": "off",
        "WARDEN_STABILITY": "0",
        "PORT": "8000",
    }
    assert validate_env(ok) == []


def test_拼错的WARDEN变量会被识别出来() -> None:
    """`WARDEN_RATELIMIT` 少一个下划线 —— 原先会被静默忽略、悄悄用默认值。"""
    suspects = unknown_warden_variables(
        {"WARDEN_RATE_LIMIT": "600/60", "WARDEN_RATELIMIT": "10/60"}
    )
    assert suspects == ["WARDEN_RATELIMIT"]


def test_第三方变量不误报() -> None:
    """DEEPSEEK_API_KEY 这类非 WARDEN_ 前缀的不该被当成拼写错误。"""
    assert unknown_warden_variables({"DEEPSEEK_API_KEY": "placeholder", "PATH": "/usr/bin"}) == []


def test_k8s注入的服务变量不误报() -> None:
    """k8s 为 Service 注入 `<SVC>_SERVICE_HOST` / `<SVC>_PORT_80_TCP*` 等——
    带 WARDEN_ 前缀但不是用户拼错，不该在每个 pod 启动时刷告警。"""
    injected = {
        "WARDEN_AGENT_SERVICE_HOST": "10.96.0.1",
        "WARDEN_AGENT_SERVICE_PORT": "80",
        "WARDEN_AGENT_SERVICE_PORT_HTTP": "80",
        "WARDEN_AGENT_PORT": "tcp://10.96.0.1:80",
        "WARDEN_AGENT_PORT_80_TCP": "tcp://10.96.0.1:80",
        "WARDEN_AGENT_PORT_80_TCP_ADDR": "10.96.0.1",
        "WARDEN_AGENT_PORT_80_TCP_PORT": "80",
        "WARDEN_AGENT_PORT_80_TCP_PROTO": "tcp",
    }
    assert unknown_warden_variables(injected) == []
    # 真正的拼写错误照抓
    assert unknown_warden_variables({"WARDEN_RATELIMIT": "10/60"}) == ["WARDEN_RATELIMIT"]


# ---------- 类型化访问器（各模块读配置的统一入口）----------


def test_守卫能识别类型化访问器() -> None:
    """守卫必须认 `env_int("X", ...)` 这种读法——否则模块改用访问器后守卫就"看不见"了。"""
    node = ast.parse('v = env_int("WARDEN_TEST_ONE", 1)').body[0].value
    assert _read_literal(node) == "WARDEN_TEST_ONE"
    node2 = ast.parse('v = settings.env_bool("WARDEN_TEST_TWO")').body[0].value
    assert _read_literal(node2) == "WARDEN_TEST_TWO"
    # 把变量名放进变量再传 → 扫不到。这是**约定**：变量名必须在调用处是字面量。
    node3 = ast.parse("v = env_int(name_var)").body[0].value
    assert _read_literal(node3) is None


def test_守卫能识别模块级常量别名() -> None:
    """把变量名写成模块级常量再传，也必须能扫到（`server.py` 曾靠这招漏过登记）。"""
    tree = ast.parse('ENV_SELF = "WARDEN_APPROVAL_ALLOW_SELF"\nv = env_bool(ENV_SELF)')
    consts = _module_constants(tree)
    assert consts == {"ENV_SELF": "WARDEN_APPROVAL_ALLOW_SELF"}
    node = tree.body[1].value
    assert _read_literal(node, consts) == "WARDEN_APPROVAL_ALLOW_SELF"
    # 不传常量表时依旧扫不到——保证"漏登记"不会被悄悄兼容掉
    assert _read_literal(node) is None


def test_前缀式常量不当成变量名() -> None:
    """`PROJECT_ENV_PREFIX = "WARDEN_"` 是前缀不是变量名，不能因此误报“未登记”。"""
    tree = ast.parse('PREFIX = "WARDEN_"')
    assert _module_constants(tree) == {}


def test_本仓实际读到的审批逃生门已登记(env_reads: dict[str, set[str]]) -> None:
    """这是**修的那个具体盲区**的回归：常量别名读法现在会被算进 env_reads。"""
    assert "WARDEN_APPROVAL_ALLOW_SELF" in env_reads
    # 读取点随 server.py 拆分搬到了 http_kit.py（SoD 判定与它的“逃生门”在同一个模块）；
    # 这里**故意写死模块名**：写死才能发现“读取点悄悄搬走、而登记表没跟着改”。
    assert env_reads["WARDEN_APPROVAL_ALLOW_SELF"] == {"web/http_kit.py"}


def test_访问器解析口径统一() -> None:
    from warden_agent.core.settings import (
        env_bool,
        env_int,
        env_opt,
        env_positive_int,
        env_str,
    )

    assert env_str("K", "d", {}) == "d"
    assert env_str("K", "d", {"K": ""}) == "d"          # 空串按"没配"处理
    assert env_opt("K", {}) is None
    assert env_opt("K", {"K": ""}) == ""                # 需要区分时才用 env_opt

    assert env_int("K", 7, {}) == 7
    assert env_int("K", 7, {"K": " 9 "}) == 9
    with pytest.raises(ValueError, match="K"):
        env_int("K", 7, {"K": "many"})                  # 写错要点名变量，别静默用默认

    assert env_bool("K", True, {}) is True
    assert env_bool("K", True, {"K": "0"}) is False
    assert env_bool("K", False, {"K": "yes"}) is True

    assert env_positive_int("K", 5, {"K": "0"}) == 5    # <=0 → 用默认
    assert env_positive_int("K", 5, {"K": "3"}) == 3
    assert env_positive_int("K", 5, {"K": "-1"}) == 5


def test_敏感值在日志里打码() -> None:
    fake = _fake_value("token")  # 运行时生成，源码里没有像凭据的字面量
    joined = "\n".join(describe({"WARDEN_API_KEY": fake}))
    assert fake not in joined, "敏感值不得出现在给日志用的配置描述里"
    assert "WARDEN_API_KEY" in joined


def test_校验口径与运行时解析器一致() -> None:
    """防止两处漂移：settings 的格式校验不能比真正的解析器更严或更松。

    自包含校验是为了守住 core 的层级约束（不能 import 上层 web 模块），
    但两边对同一批取值的"接受/拒绝"必须一致。
    """
    from warden_agent.web.ratelimit import parse_rate_limit

    samples = ["600/60", "0", "off", "", "120/60", "10/60", "abc", "600", "600/", "1/2/3"]
    for value in samples:
        accepted_by_settings = not validate_env({"WARDEN_RATE_LIMIT": value})
        try:
            parse_rate_limit(value)
            accepted_by_runtime = True
        except ValueError:
            accepted_by_runtime = False
        assert accepted_by_settings == accepted_by_runtime, (
            f"取值 {value!r}：settings 校验={'接受' if accepted_by_settings else '拒绝'}，"
            f"但运行时解析器={'接受' if accepted_by_runtime else '拒绝'}"
        )
