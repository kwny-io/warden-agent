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
       并且 README 自称的那个变量数必须等于登记表条数；
    5. **文档里的覆盖率门槛必须等于 `pyproject.toml` 的 `fail_under`**
       （`docs/operations.md` 曾写 84% 而事实源是 86%——数字只被手抄，就没东西能让它变红）；
    6. **e2e 启动脚本不得继承宿主 shell 的 `WARDEN_*`**（`web/e2e/start-e2e-server.mjs`
       原先把整个 `process.env` 铺进子进程，于是 `WARDEN_SSE_HEARTBEAT_SECONDS=999 node …`
       能让"确定性档"的后端以 999 秒心跳启动——e2e 断言的东西取决于跑之前 shell 里恰好有什么）。
    7. **门控/夹具 docstring 里承诺的「可用环境变量覆盖」必须真的生效**
       （第七个方向，`test_PG门控真的认环境变量里的端口`）：PG 门控的 docstring 写着
       "可用环境变量覆盖（给 CI 用）"，而它当时**不读端口**——后果不是多一条红，
       而是**静默少跑 40 条**（PG 不在默认端口 ⇒ 40 条一律 skip，整套仍报 "0 failed"，
       看起来是绿的）。

  没有这七条，注册表（以及文档里的每个数字）就只是文档——而文档是不会自己变红的。

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
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

import pytest

from warden_agent.core.settings import (
    ENV_SPECS,
    describe,
    external_env_names,
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
#      当时的真实保证只有 `DEEPSEEK_API_KEY=''` + `load_env()` 不覆盖已存在的键；
#      第五十批补上了第二个真旋钮 `WARDEN_IGNORE_DOTENV=1`（在那之前 `.env` 照读，
#      于是 e2e 的档位仍会随开发机的本地 `.env` 变——同一个失败模式的另一个入口）。
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
    # 第五十三批补（顺手抓到的回归）：变量本身是真的，但它属于**测试脚手架**——
    # `tests/test_postgres_integration.py::_pg_params()` 读它，
    # `src/` 里没有任何地方读 ⇒ 登记进 `ENV_SPECS` 会立刻撞上「登记了没人读」那条守卫。
    "WARDEN_TEST_PG_PORT": "测试脚手架旋钮（PG 门控端口，只在 tests/ 里读），故意不进产品注册表",
    # 第五十三批补（同批另一条）：
    # 本批把扫描范围扩到 `deploy/**/*.md`，**先拍到了我自己刚写下的文档行**——
    # `docs/复核索引.md` 在记录“文档曾教人写错键名”时，必须把那个错名字写出来。
    # 它不是变量、也不该被“登记”，但也不等于“可以当真名用”：
    # 真正拦住“README 把 Secret 键写成它”的是 `tests/test_k8s_manifests.py`
    # 的 `test_ServiceMonitor要的Secret键必须被部署文档点名`（变异 M5：换成它 → 红）。
    "WARDEN_METRICS_TOKEN": (
        "反例名（真名 `WARDEN_METRICS_BEARER`）："
        "CHANGELOG/复核索引记录“文档曾教人写它”时要写出来"
    ),
}
_DOC_GLOBS = ("README.md", "docs/**/*.md", "deploy/**/*.md")
# 第五十三批把 `deploy/**/*.md` 也纳进来：部署文档同样会点名变量（它甚至比 README 更接近
# "照者它就能配上"这个场景），而扫描范围漏了它 ⇒ `deploy/k8s/README.md` 里把 Secret 键写成
# `WARDEN_METRICS_TOKEN`（真实键名是 `WARDEN_METRICS_BEARER`，见 `secret.example.yaml:37`
# 与 `deploy/observability/servicemonitor.yaml:48`）一直没被拍。
# 后果不是"名字写错"这么轻：操作者照文档往 Secret 里写 `WARDEN_METRICS_TOKEN`，
# 而 ServiceMonitor 要的是 `WARDEN_METRICS_BEARER` ⇒ 抓取 401 ⇒ `up{job=…}==0`
# 把 WardenServiceDown 打成长期 critical，**真正的告警反而被淹没**（就是 D-B4 那个后果，
# 只不辇 yaml 那对已有守卫、"文档–yaml"这对没有）。
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
    """文档说"可调"的旋钮，登记表里必须真有——否则就是一句空承诺。

    例外（第五十三批）：`external_env_names()` 里的名字是**已声明、但由部署里别的组件读**
    （`WARDEN_METRICS_BEARER` → ServiceMonitor 的 `bearerTokenSecret`）。它们**必须**出现在
    部署文档里（不然运维填不完 Secret），但不该进 `ENV_SPECS`——那会变成「登记了却没人读」。
    """
    registered = registered_env_names() | external_env_names()
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


_COVERAGE_GATE_CLAIM = re.compile(r"覆盖率门槛\s*\*\*(\d+)%\*\*")


def test_文档里的覆盖率门槛与pyproject一致() -> None:
    """文档写"覆盖率门槛 N%"——N 必须真是 `pyproject.toml` 里的 `fail_under`。

    这条守卫是**追着一个真缺陷**加上的：`docs/operations.md` 一直写着门槛 84%，
    而唯一事实源 `pyproject.toml` 里是 86（84% → 86% 是刻意收窄，见 `docs/复核索引.md`）——
    漂移了整整一批，没人发现，因为这类数字只被手抄、从来没东西核对它。
    与 `test_README里的变量计数与注册表一致` 同源：单一事实源自己不能自相矛盾。
    """
    root = Path(__file__).resolve().parents[1]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    gate = re.search(r"^fail_under\s*=\s*(\d+)", pyproject, re.MULTILINE)
    assert gate, "`pyproject.toml` 里找不到 `fail_under = N`（改了写法就同步改这条用例）"
    expected = int(gate.group(1))

    drift: list[str] = []
    for pattern in _DOC_GLOBS:
        for path in sorted(root.glob(pattern)):
            text = path.read_text(encoding="utf-8")
            for found in _COVERAGE_GATE_CLAIM.finditer(text):
                if int(found.group(1)) != expected:
                    line = text[: found.start()].count("\n") + 1
                    rel = str(path.relative_to(root)).replace("\\", "/")
                    drift.append(f"  {rel}:{line} 写 {found.group(1)}%，实际 {expected}%")
    assert not drift, (
        "覆盖率门槛在文档里被手抄成了别的值"
        "（门禁只认 `pyproject.toml` 的 `fail_under`，改门槛就顺手改文档）：\n"
        + "\n".join(drift)
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


# ---------- 第五十三批：占位符是一类特殊的错值 ----------
#
# 为什么不是「小事」：占位符**不是空值**。空值会走「未配置」分支（例如没配
# `WARDEN_TRUSTED_PROXIES` 就不传 `forwarded_allow_ips`、启动日志也会明说「不采信 XFF」）；
# 而占位符会被当成**真实取值**一路用下去，于是表现成「日志说配了、机制没生效」。
# 行为级证据见 `tests/test_proxy_trust.py` 里那条「占位符会让受信代理静默失效」的用例。


def test_占位符会被启动校验判错() -> None:
    """实测过的那一个：`deploy/k8s/configmap.yaml` 就写着这个值。"""
    errors = validate_env({"WARDEN_TRUSTED_PROXIES": "REPLACE_ME-pod-cidr"})
    assert len(errors) == 1, errors
    assert "WARDEN_TRUSTED_PROXIES" in errors[0] and "占位符" in errors[0], errors[0]
    # 报错要把真实取值展示出来（这个变量不敏感），否则运维不知道是哪个值没用
    assert "REPLACE_ME-pod-cidr" in errors[0]
    # 并且要说清后果，而不是只说「格式不对」
    assert "X-Forwarded-For" in errors[0]


@pytest.mark.parametrize("value", ["REPLACE_ME", "replace_me-1", "Replace-Me", "REPLACEME"])
def test_占位符的各种写法都认(value: str) -> None:
    """只认一种写法 = 留下一个漏的（模板里的写法本身就有变体）。"""
    assert validate_env({"WARDEN_PG_HOST": value}), f"{value!r} 没被判错"


def test_占位符判定与变量类型无关() -> None:
    """只修 `proxies` 那一个变量等于留下下一个漏的：
    `WARDEN_ADMIN_PRINCIPALS` 变成「配了但没有管理员」、`WARDEN_PG_HOST` 连不上库。
    """
    for key, value in [
        ("WARDEN_PG_HOST", "REPLACE_ME-pg-host"),  # str
        ("WARDEN_ADMIN_PRINCIPALS", "REPLACE_ME-ops-user"),  # str
        ("WARDEN_TENANT", "REPLACE_ME-tenant"),  # str
        ("WARDEN_AUDIT", "REPLACE_ME"),  # bool
        ("WARDEN_RATE_LIMIT", "REPLACE_ME"),  # rate
    ]:
        errors = validate_env({key: value})
        assert errors and "占位符" in errors[0], (key, value, errors)


def test_真值里包含replace字样不会被误伤() -> None:
    """逃生舱：判据是 `replace-me` 这种**占位符形状**，不是「含 replace 就报错」。"""
    ok = {
        "WARDEN_PG_HOST": "pg-replaces.internal",
        "WARDEN_TRUSTED_PROXIES": "10.0.0.0/8",
        "WARDEN_TENANT": "acme",
    }
    assert validate_env(ok) == []


def test_占位符报错不会泄露敏感值() -> None:
    """敏感变量的报错必须打码（与其它格式错误同一口径）——否则日志会多一条泄露渠道。"""
    errors = validate_env({"WARDEN_CREDENTIAL_KEY": "REPLACE_ME"})
    assert errors, "敏感变量上的占位符也必须判错"
    assert "REPLACE_ME" not in errors[0] and "已打码" in errors[0], errors[0]


# ---------- 第五十三批：应用不读、但部署里别的组件要读的变量 ----------


def test_部署组件读的变量不算拼写错误() -> None:
    """`envFrom: secretRef` 会把 `WARDEN_METRICS_BEARER` 也放进 pod 环境，而应用不读它
    （读它的是 ServiceMonitor 的 `bearerTokenSecret`）。不豁免的话，每个 pod 启动都会
    告警「它不会生效——是不是拼错了？」，运维会去找一个并不存在的拼写错误。
    """
    assert "WARDEN_METRICS_BEARER" in external_env_names()
    assert unknown_warden_variables({"WARDEN_METRICS_BEARER": "sk-x"}) == []
    # 豁免不能顺便把真的拼写错误放过
    assert unknown_warden_variables({"WARDEN_METRICS_BEARERR": "sk-x"}) == [
        "WARDEN_METRICS_BEARERR"
    ]


def test_部署组件读的变量仍然受占位符检查() -> None:
    """豁免的只是「拼写错误」这一条判断：值本身是占位符照样要拦。

    `deploy/k8s/secret.example.yaml:37` 就写着 `WARDEN_METRICS_BEARER: "REPLACE_ME"`——
    如果这条不管，抓取会 401、`up==0`，把 WardenServiceDown 打成长期 critical。
    同时敏感值不得出现在报错里。
    """
    errors = validate_env({"WARDEN_METRICS_BEARER": "REPLACE_ME"})
    assert len(errors) == 1, errors
    assert "WARDEN_METRICS_BEARER" in errors[0] and "占位符" in errors[0], errors[0]
    assert "REPLACE_ME" not in errors[0] and "已打码" in errors[0], errors[0]


def test_部署组件读的变量不登记进ENV_SPECS() -> None:
    """它不能进 ENV_SPECS：那会变成「登记了却没人读」——本仓库正在消灭的另一个形状。

    登记表 = 「应用会读的旋钮」；它只受 `_EXTERNAL_ENV_KEYS` 声明（见那里的三条说明）。
    """
    assert "WARDEN_METRICS_BEARER" not in registered_env_names()


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


# ---------- 口径 6：e2e 的子进程姿态必须自己定，不能吃宿主环境 ----------

_E2E_HARNESS = Path(__file__).resolve().parents[1] / "web" / "e2e" / "start-e2e-server.mjs"


def test_e2e启动脚本不吃宿主环境里的WARDEN变量() -> None:
    """e2e 的子进程姿态必须由脚本自己决定，不能从宿主 shell 继承 `WARDEN_*`。

    这条守卫是**追着一个可复现的真缺陷**加的（本文件口径 6）：
    脚本里原先写的是把整个 `process.env` 铺进子进程，于是

        WARDEN_SSE_HEARTBEAT_SECONDS=999 node e2e/start-e2e-server.mjs

    会让"确定性档"的后端以 999 秒空闲心跳启动（启动日志里能看到，已实测）。
    同一行还把开发者的真凭据一并递进子进程，只靠下一行"显式给空串"兜住。
    失效方式是**静默的**：e2e 要么红在一个和被测代码无关的地方，
    要么在"以为离线"的情况下打真模型。

    口径是**文本级**的（不是行为级）：真跑一次 e2e 要 vite 构建 + 起后端，代价太大；
    这里只保证"没有把整个环境铺进去"这个形状不会回来。行为级证据见 CHANGELOG 第四十九批。
    """
    text = _E2E_HARNESS.read_text(encoding="utf-8")
    # 只认行首的铺开写法（注释里会引用这个历史形状，不该被误伤）。
    assert not re.search(r"^\s*\.\.\.process\.env\b", text, re.MULTILINE), (
        "`web/e2e/start-e2e-server.mjs` 又把整个 `process.env` 铺进子进程了：\n"
        "  宿主 shell 里任何一个 `WARDEN_*` 都会改变 e2e 的档位，实测"
        "`WARDEN_SSE_HEARTBEAT_SECONDS=999 node e2e/start-e2e-server.mjs`"
        " 能让子进程以 999 秒心跳启动。请只保留非 `WARDEN_*` 的键，再显式设置档位。"
    )
    # 摘掉宿主变量的那一步必须在，否则上面那条只是"碰巧没出现那串字"。
    assert "startsWith('WARDEN_')" in text, (
        "找不到摘掉宿主 `WARDEN_*` 的过滤（`!key.startsWith('WARDEN_')`）："
        " 要么被删了，要么改了写法——两种都请同步改这条守卫的判据。"
    )
    # 档位与离线姿态必须仍是**显式**的：入参是显式的，不是环境里碰巧有的。
    for key in (
        "WARDEN_E2E_API_KEY",
        "WARDEN_ALLOW_ANON",
        "WARDEN_API_KEY",
        "DEEPSEEK_API_KEY",
        # `.env` 是第二条进环境的路（宿主 shell 是第一条）：主机上那个文件
        # 会静默盖掉没显式设的键，e2e 的档位于是跟着开发机的本地文件变。
        "WARDEN_IGNORE_DOTENV",
    ):
        assert key in text, f"e2e 启动脚本不再显式处理 {key}（档位/离线姿态会失控）"


# ---------- 第七个方向：门控自己承诺的「环境变量覆盖」必须真的生效 ----------


def test_PG门控真的认环境变量里的端口() -> None:
    """PG 门控的 docstring 写着「可用环境变量覆盖（给 CI 用）」——那就必须真的能覆盖。

    为什么值得一条守卫：踩到的后果**不是「多一条红」，而是静默少跑 40 条**——
    PG 不在默认端口时（远端库 / CI 换端口 / 本地把 5432 映射到 5433），门控连不上 ⇒
    40 条 PG 用例一律 skip，而整套仍然报「0 failed」：**看起来是绿的**。

    第五十二批做「双口径」时就是这么栽的：以为设 `WARDEN_TEST_PG_PORT=5999`
    就切到了「无 PG 口径」，两次跑却都是 PG 口径（两次 `skipped` 都是 4）——
    因为当时的 `_pg_params()` 只读 HOST/DB/USER/PASSWORD，**端口写死**。
    ⇒ 判口径永远看 `skipped` 是 4 还是 44，不看自己设了什么环境变量。

    这里用**子进程探针**（不是读源码文本）：把端口指向一个没人监听的地方，要求
    (1) 连接参数里真的带上这个端口，(2) 门控据此判定「没有可用的库」——
    两条都钉在机制上。注意 `WARDEN_TEST_PG_PORT` 是**测试脚手架**的旋钮，
    不在 `core/settings.py` 的产品注册表里（所以它不属于上面六个方向的登记检查）。
    """
    probe = (
        "import sys;"
        "sys.path.insert(0, 'tests');"
        "import test_postgres_integration as m;"
        "print(m._pg_params().get('port'));"
        "print(m._pg_available())"
    )
    env = {**os.environ, "WARDEN_TEST_PG_PORT": "5999"}
    done = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(Path(__file__).resolve().parents[1]),
        check=True,
        timeout=120,
    )
    lines = done.stdout.split()
    assert lines and lines[0] == "5999", (
        f"门控没把 `WARDEN_TEST_PG_PORT` 读进连接参数（拿到 {lines[:1]!r}）："
        " docstring 承诺「可用环境变量覆盖」，那就必须真的能覆盖——"
        " 覆盖不了时它会连默认端口，PG 不在那里就静默 skip 掉 40 条，而整套仍然是绿的。"
    )
    assert lines[-1] == "False", (
        f"端口指向没人监听的地方时，门控仍报「有可用的库」（拿到 {lines[-1:]!r}）："
        " 要么端口被忽略、要么门控根本没在探活。"
    )
