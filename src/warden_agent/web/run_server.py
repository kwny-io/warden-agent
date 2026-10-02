"""启动 HTTP/SSE 服务：把 Agent 对外暴露成可调用的 API。

用法：
    cd /d/warden-agent
    py -m warden_agent.web.run_server

可选环境变量（不设则用假模型，不花真实费用）：
    DEEPSEEK_API_KEY=sk-xxx         启用真实 DeepSeek
    PORT=8000                       端口（默认 8000）
    WARDEN_HOST=127.0.0.1           监听地址（默认只本机；容器里要设 0.0.0.0）
    WARDEN_API_KEY=sk-xxx           **访问密钥（对外部署必须设）** → 开启 Bearer 鉴权
    WARDEN_API_KEYS=alice:k1,bob:k2 **多用户隔离**：逗号分隔 `用户id:密钥`，各自独立身份
    WARDEN_API_USER=demo-user       单 key 模式下的身份（默认 demo-user）
    WARDEN_TENANT=local             租户 id（审计与授权按它隔离）
    WARDEN_ALLOW_ANON=1             **仅本机开发**：显式声明接受"无鉴权"
    WARDEN_AUDIT=1                   开启审计（写进 SQLite 审计表，重启不丢）
    WARDEN_RATE_LIMIT=600/60         限流：每 60 秒最多 600 次（默认；设 0 关闭）
    WARDEN_MAX_REQUEST_BYTES=1048576 请求体上限（字节）；超限返 413（默认 1 MiB；0 关闭）
    WARDEN_SSE_MAX_CONNECTIONS=100   /chat/stream 并发上限；超限返 503（默认 100；0 关闭）
    WARDEN_STABILITY=0              关闭工具稳定性层（**默认开启**：超时 + 退避重试 + 熔断）
    GIT_WORKDIR=path                指定 git 仓库目录 → 注册 git.apply_patch 工具
    SKILLS_DIR=path                 启用技能系统（SKILL.md 目录）
    WARDEN_KNOWLEDGE=1|path         RAG 知识库：1=内置离线语料，或给一个文档目录（.md/.txt）
    WARDEN_WEB_FETCH=1              开启**真实联网抓取**（web.fetch 真发 HTTP；默认离线 mock）
    MCP_SERVER=cmd                  启用 MCP（需 node）

鉴权是 **fail-closed** 的：既没有 `WARDEN_API_KEY`、也没有显式 `WARDEN_ALLOW_ANON=1`
时**拒绝启动**；监听非本机地址（如 0.0.0.0）时若无鉴权，同样拒绝启动。
理由：`/approve` 和 `/reject` 是"人工审批"这道闸门的入口——如果接口本身无鉴权，
调用方就能批准自己的高危操作，门禁形同虚设。

启动后：
    - 打开 http://127.0.0.1:8000/docs 可看交互式 API 文档（设了 WARDEN_API_KEY 后需带 Bearer key）
    - POST /chat/run-1  送一句话给 Agent
    - GET  /approvals   看等待审批的请求
    - POST /approve/run-1 / /reject/run-1  处理审批
    - GET  /health/live / /health/ready     存活/就绪探针
    - GET  /audit                           查看审计轨迹
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import uvicorn

from warden_agent.core.config import load_env
from warden_agent.core.logging_setup import get_logger, setup_logging
from warden_agent.core.settings import (
    describe,
    env_bool,
    env_int,
    env_opt,
    env_positive_int,
    env_str,
    unknown_warden_variables,
    validate_env,
)
from warden_agent.execution.sandbox import sandbox_isolation_policy
from warden_agent.loop.intent import ToolIntentRouter
from warden_agent.loop.planner import ModelPlanner
from warden_agent.model.deepseek import DeepSeekModel
from warden_agent.model.fake import FakeModel
from warden_agent.model.model import AgentChatModel
from warden_agent.policy.policy import PolicyEngine, ask_when_tool_not_pure
from warden_agent.runtime.locking import run_lock_for
from warden_agent.store.sqlite import SqliteStore
from warden_agent.tool.catalog import ToolCatalog, function_tool
from warden_agent.web.audit import audit_store_for_backend
from warden_agent.web.auth import (
    ROLE_USER,
    TrustedCaller,
    admin_principals,
    role_for,
    viewer_principals,
)
from warden_agent.web.coordination import coordination_for
from warden_agent.web.outbound import outbound_from_env
from warden_agent.web.ratelimit import limiter_from_env
from warden_agent.web.search import providers_from_env
from warden_agent.web.server import (
    DEFAULT_MAX_REQUEST_BYTES,
    DEFAULT_SESSION_CACHE_MAX,
    DEFAULT_SSE_MAX_CONCURRENCY,
    build_app,
)

logger = get_logger("run_server")

# 上下文裁剪阈值（用字符数近似 token）：约 60k 字符 —— 够长会话用，又不至于把上下文撑爆
DEFAULT_MAX_CONTEXT_CHARS = 60000


def _build_catalog(env: Mapping[str, str] | None = None) -> ToolCatalog:
    """造工具目录：**演示卡默认不注册**，要显式 `WARDEN_DEMO_TOOLS=1`。

    这里原先无条件注册两张"演示卡"，而 `run_server` 是**真被启动的那个入口**：

      · `weather.get` → 固定返回「晴, 25 度」，不是任何真实天气接口；
      · `fs.delete`   → 函数体只 `return f"已删除 {path}"`，**根本没碰文件系统**。

    最坏的地方不是"没实现"，而是**副作用声明与实际行为相反**：模型和调用方拿到
    「已删除」的成功文本，磁盘上什么都没发生。真工具的目录（memory / web / skill /
    mcp / git，其中 `git.apply_patch`、`shell.run` 才真有副作用）是 `build_app(...)`
    用 `augment_catalog` 注进去的——**演示卡从来不是产品能力**。

    现在：默认目录里只有 `augment_catalog` 注入的真工具；演示卡必须显式打开，打开后
    工具描述与返回值都写明"演示"（返回值不再谎称"已删除"），并在启动日志里喊一声。
    演示卡仍然 `pure=False`，所以"审批闸门真的会拦"这件事照样能演示。
    """
    catalog = ToolCatalog()
    if not env_bool("WARDEN_DEMO_TOOLS", False, env):
        return catalog

    logger.warning(
        "WARDEN_DEMO_TOOLS=1：注册演示工具 weather.get（固定值，非真实天气接口）"
        "与 fs.delete（**不会真的删文件**）。它们只为演示审批闸门而存在，"
        "对外服务前请关掉这个开关。",
    )

    @function_tool(
        "weather.get",
        "获取某城市的天气（**演示用固定值**，不是真实天气接口）",
        {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
        pure=True,
    )
    def get_weather(city: str) -> str:
        return f"{city}: 晴, 25 度（演示固定值）"

    catalog.register(get_weather)

    # 演示"需要审批的高危工具"：删除文件必须人工批准。
    # 注意这里**不做真删除**——演示卡没有 workspace_root 兜底，真删一个任意路径反而是风险。
    # 返回值写清"未真正删除"，不让模型/调用方拿到假的成功。
    @function_tool(
        "fs.delete",
        "删除一个文件（高危，需要审批）——**演示卡：不会真的删文件**",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        pure=False,
    )
    def delete_file(path: str) -> str:
        return f"[演示] 未真正删除 {path}：这是演示卡，只用来演示审批闸门"

    catalog.register(delete_file)
    return catalog


# 无副作用豁免名单：只写**本项目自己**的数据（租户内、可回滚、不碰外部系统），
# 把它们也堵在审批上只会把日常操作变成"点确认"的体力活。
# 想更严的部署：把这个集合改成 frozenset()（连 memory.remember 也要人工批准）。
#
# 为什么不豁免 memory.confirm：现在两阶段分得很清——
# **memory.remember / memory.remember_text 只往候选区写一条待确认**（不生效、可回滚），
# 所以豁免它们没有风险（`remember_text` 多做的只是"用规则从原话里抽句子"，
# 抽完仍然只是一条候选）；
# **memory.confirm 才真的把记忆生效**（还会替掉冲突的旧条目），那一步必须有人在场，
# 所以它不在名单里，会被默认策略拦成待审批。
_POLICY_BENIGN_TOOLS: frozenset[str] = frozenset({"memory.remember", "memory.remember_text"})


def _is_pure_tool(catalog: ToolCatalog, name: str) -> bool:
    """查目录：该工具是否**显式声明** `pure=True`。

    查不到（未注册/名字不对）→ False，即按"可能有副作用"处理（fail-closed）。
    """
    try:
        return bool(catalog.get(name).pure)
    except KeyError:
        return False


def _build_policy(catalog: ToolCatalog) -> PolicyEngine:
    """门禁：**没声明 `pure=True` 的工具默认需要人工批准**，声明了才放行。

    这里曾经是 `ask_when_tool_in(frozenset({"fs.delete"}))` —— 黑名单式，只有被
    点名的工具才拦。实测后果是**双向错的**：

      - `shell.run`（能在沙箱里跑任意命令）、`git.apply_patch`（写仓库）、
        MCP 导入的工具全部**直接放行** —— 它们才是有真实副作用的那些；
      - 唯一被拦住的 `fs.delete` 恰恰是演示用的空壳（函数体只 `return "已删除 xxx"`），
        连真实删除都没做。

    也就是"门是上了锁的，但锁在了没有门的那面墙上"。现在按 `pure` 声明默认拦：
    新增工具忘标记 = 要审批，而不是 = 放行。

    `catalog` 必须是**同一个会被 `build_app` 继续 augment 的目录对象**（见
    web/server.py 里 `augment_catalog(catalog, ...)`）—— 这样后注册的
    memory/web/skill/mcp/git 工具也在门禁覆盖范围内（闭包读的是实时目录）。
    """
    engine = PolicyEngine()
    engine.add(
        ask_when_tool_not_pure(
            lambda name: _is_pure_tool(catalog, name),
            exempt=_POLICY_BENIGN_TOOLS,
        )
    )
    return engine


class AuthConfigError(RuntimeError):
    """鉴权配置缺失。fail-closed：宁可起不来，也不要无鉴权地对外服务。"""


def is_loopback_host(host: str) -> bool:
    """是否只监听本机。

    用 `ipaddress` 判断整个回环网段（**127.0.0.0/8** 与 `::1`），而不是只认
    `127.0.0.1` 这一个字符串——绑到 `127.0.0.5` 同样是"只监听本机"，
    按字符串比对会把它误判成"对外"（方向的后果是要求鉴权，属 fail-closed、
    不是安全洞，但口径不对会让本地开发莫名其妙起不来）。
    """
    import ipaddress

    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip()).is_loopback
    except ValueError:      # 不是 IP 字面量（域名等）→ 不能认定为本机
        return False


def _caller_for(user_id: str, tenant: str, role: str = ROLE_USER) -> TrustedCaller:
    """把一个用户 id 包成调用者身份。principal_id 即会话归属用户（见 TrustedCaller.user_id）。

    `role` 由 `WARDEN_ADMIN_PRINCIPALS` 决定（**不来自请求**）——见 auth.role_for。
    """
    return TrustedCaller(
        tenant_id=tenant,
        principal_type="user",
        principal_id=user_id,
        product_id="http",
        role=role,
    )


def resolve_auth(env: Mapping[str, str]) -> tuple[dict[str, TrustedCaller] | None, str]:
    """决定鉴权模式，返回 (api_keys, mode)。

    mode：
      "bearer"   —— 配了 `WARDEN_API_KEYS`（多用户）或 `WARDEN_API_KEY`（单用户）
      "anon-dev" —— 未配 key，但**显式**设了 `WARDEN_ALLOW_ANON=1`，声明接受无鉴权

    既没有 key、也没有显式声明 → 抛 `AuthConfigError`（拒绝启动）。
    这是刻意的 fail-closed：无鉴权时任何人都能调 `/audit`（读全部审计）、
    以及 `/approve`（批准高危操作）——审批门禁会被自己人绕过。

    **多用户（多租户）用 `WARDEN_API_KEYS`**：逗号分隔的 `用户id:密钥`，例如
    `alice:sk-aaa,bob:sk-bbb`。每个 key 绑定一个用户身份——**身份由 key 决定，
    客户端无法自称**。这是会话归属与越权拦截能成立的前提：单 key 模式下所有调用者
    都是同一个身份，谈不上隔离。

    **密钥轮换（零停机）**：同一个用户写两次即可，两份 key 同时有效——
    `alice:新key,alice:旧key`：先把新 key 发给部署、确认新 key 能用，再把旧 key 从配置里
    删掉、重启。区间内新旧都能用，不会因为“换 key 的那一秒”把在飞的客户端打成 401。
    同一个密钥写给**两个不同用户**会被拒绝（见下），因为那会让一个人的调用以另一个
    人的身份记账；同一个用户给两个 key 则是轮换，合法。

    单 key 模式（`WARDEN_API_KEY`）的身份取 `WARDEN_API_USER`（默认 `demo-user`，
    与控制台默认账号一致）。租户取 `WARDEN_TENANT`（默认 `local`）。
    """
    tenant = env_str("WARDEN_TENANT", "local", env).strip() or "local"
    # 管理员/只读名单来自配置（不配 ⇒ 没有管理员、没有只读；见 auth 的 fail-closed 说明）。
    admins = admin_principals(env)
    viewers = viewer_principals(env)

    raw_keys = env_str("WARDEN_API_KEYS", "", env).strip()
    if raw_keys:
        keys: dict[str, TrustedCaller] = {}
        for pair in raw_keys.split(","):
            pair = pair.strip()
            if not pair:
                continue
            user_id, sep, key = pair.partition(":")
            if not sep or not user_id.strip() or not key.strip():
                raise AuthConfigError(
                    f"WARDEN_API_KEYS 格式错误：{pair!r}。应为逗号分隔的 `用户id:密钥`，"
                    "例如 alice:sk-aaa,bob:sk-bbb"
                )
            uid = user_id.strip()
            clean_key = key.strip()
            # 同一个密钥挂在两个不同用户上 = 配置错误，而且错得很险：后一个会把前一个
            # **静默替掉**，于是 alice 拿着自己的 key 却以 bob 的身份出现（或反之）。
            # 密钥轮换是合法的“同一个用户两个 key”（alice:old,alice:new），那不是冲突。
            existing = keys.get(clean_key)
            if existing is not None and existing.principal_id != uid:
                raise AuthConfigError(
                    f"WARDEN_API_KEYS 里同一个密钥被配给了两个用户："
                    f"{existing.principal_id!r} 与 {uid!r}。"
                    "这会让其中一个人的调用以另一个人的身份记账（会话归属与越权拦截全部失效）。"
                    "同一个用户配两个 key（轮换用）是允许的：alice:旧,alice:新"
                )
            keys[clean_key] = _caller_for(uid, tenant, role_for(uid, admins, viewers))
        if keys:
            return keys, "bearer"

    single_key = env_opt("WARDEN_API_KEY", env)
    if single_key:
        user_id = env_str("WARDEN_API_USER", "demo-user", env).strip() or "demo-user"
        role = role_for(user_id, admins, viewers)
        return {single_key: _caller_for(user_id, tenant, role)}, "bearer"

    if env_bool("WARDEN_ALLOW_ANON", False, env):
        return None, "anon-dev"

    raise AuthConfigError(
        "未设置 WARDEN_API_KEY —— 拒绝启动（fail-closed）。\n"
        "  · 对外 / 容器部署：必须设置 WARDEN_API_KEY=<强随机串>\n"
        "  · 多用户隔离：设置 WARDEN_API_KEYS=alice:key1,bob:key2\n"
        "  · 仅本机开发：显式设 WARDEN_ALLOW_ANON=1 表示接受无鉴权"
    )


def ensure_listen_is_safe(host: str, auth_mode: str) -> None:
    """校验「监听地址 × 鉴权模式」这个组合是否安全。不安全则抛 AuthConfigError。

    单独抽出来是为了可测：main() 里只负责把异常变成退出码。
    """
    if auth_mode != "bearer" and not is_loopback_host(host):
        raise AuthConfigError(
            f"WARDEN_HOST={host} 是对外监听地址，但未开启鉴权 —— 拒绝启动。"
            "请设置 WARDEN_API_KEY，或把 WARDEN_HOST 改回 127.0.0.1。"
        )


class DemoModelRefusedError(RuntimeError):
    """非本机开发场景却要拿假模型对外服务。"""


def ensure_model_is_real(
    *, is_fake: bool, host: str, auth_mode: str, env: Mapping[str, str]
) -> None:
    """假模型 + 「真的会给别人用」的组合 → 拒绝启动（fail-closed）。

    为什么这算 P0 而不是「一个小提醒」：FakeModel 的回复是固定脚本，但它**看起来像真能力**
    （会选工具、会答话、事件流完整）。把它跑在容器/对外端口上，评审者、集成方、
    演示观众都会把「假模型的输出」当成系统真实能力的证据——这类「能力错觉」
    比直接报错危险得多，所以要把「到底是真模型还是演示」变成一个**必须显式声明**的事实。

    判定用的是**能不能被机器外的人连上**，而不是"配置看起来多正式"：
      · 回环地址（默认 `127.0.0.1`）→ 允许。本机怎么跑都行；这里即使开了鉴权也不报，
        因为**没有任何远端能连进来**，而为了"试一下鉴权"就得先声明是个演示会很烦。
        仅在「开鉴权 + 假模型」时给一条 warning（这种组合多半是照着生产配置改的）。
      · 已**双重显式声明** `WARDEN_ALLOW_FAKE_MODEL=1` **且** `WARDEN_DEMO_INSTANCE=1`
        → 允许（演示实例；启动日志与 `/capabilities` 会标明）
      · 其余（对外监听 0.0.0.0 / 具体网卡地址）→ 拒绝启动，并给出去路

    为什么要**两个**变量而不是一个：像生产配置里残留一句 `ALLOW_FAKE_MODEL=1`（本来是
    为了跑一次演示）这种事故，一个变量拦不住。第二个变量要的是一个**只可能故意写**的词
    ——把"我这就是个演示实例"讲出口，而不是顺手留个开关。

    容器里的坑：容器内必须 `WARDEN_HOST=0.0.0.0` 才能被端口映射进来，那是"对外"，
    所以容器跑演示实例必须把两个都写上——这正是我们想要的。
    """
    if not is_fake:
        return
    if is_loopback_host(host):
        if auth_mode == "bearer":
            logger.warning(
                "假模型 + 鉴权模式同时开着（%s）：回复是固定脚本，别当成系统真实能力。"
                "对外暴露前请配真实模型凭据。", host,
            )
        return
    declared_demo = env_bool("WARDEN_ALLOW_FAKE_MODEL", False, env) and env_bool(
        "WARDEN_DEMO_INSTANCE", False, env
    )
    if declared_demo:
        logger.warning(
            "以演示实例启动（%s）：模型是假模型，回复是写死脚本；"
            "/chat 响应会带 X-Warden-Model-Kind: fake 与 Warning 标头。",
            host,
        )
        return
    raise DemoModelRefusedError(
        f"未配置任何真实模型凭据，却要以假模型对外服务 {host}（鉴权模式 {auth_mode}）——"
        "拒绝启动（fail-closed）。假模型的回复是固定脚本，看起来却像真能力。\n"
        "  · 演示实例：显式设 WARDEN_ALLOW_FAKE_MODEL=1 **且** WARDEN_DEMO_INSTANCE=1"
        "（会被当作演示并如实标注）\n"
        "  · 真实部署：设 DEEPSEEK_API_KEY，或改用 custom provider（WARDEN_BASE_URL + "
        "WARDEN_MODEL_API_KEY）\n"
        "  · 仅本机开发：WARDEN_HOST=127.0.0.1（回环地址无需任何声明，开鉴权也不拦）"
    )


def _knowledge_from_env(env: Mapping[str, str]) -> Any:
    """RAG 知识来源开关。返回 `True`（内置离线语料）/ 目录路径 / `None`（不用）。

    `WARDEN_KNOWLEDGE=1` → 索引内置离线语料；给一个目录路径 → 索引该目录下的
    `.md` / `.txt`；不设或设 `0` → 不注册 `knowledge.search`。

    **默认不开**：RAG 要额外索引成本（真语义嵌入还要网络与费用），不该替用户悄悄决定。
    开了会在启动日志里写明用的是**哪种嵌入器**——词频嵌入与语义嵌入质量差别很大，
    不写清楚就有"过度声称语义检索"的风险。
    """
    raw = env_str("WARDEN_KNOWLEDGE", "", env).strip()
    if not raw or raw.lower() in ("0", "false", "no", "off"):
        return None
    if raw.lower() in ("1", "true", "yes", "on"):
        return True
    return raw


def _stability_from_env(env: Mapping[str, str]) -> bool:
    """工具稳定性层是否开启。**产品路径默认开启**，`WARDEN_STABILITY=0` 可关。

    为什么默认开：没有它，一个卡住的工具会拖住整个会话；有了它，"防卡死 + 抗瞬时故障 +
    熔断"这三件事统一兜在工具调用这一层，不用每个工具自己写。想"原样执行、不做任何
    重试"时（例如审计复现）显式关掉即可。
    重试是安全的：稳定性层按 `ToolSpec.pure` 判定，**非纯工具只对瞬时错误重试**，
    不会把 `fs.delete` 这类有副作用的操作重放。
    """
    # 统一走访问器：此前这里用 `not in off` 判定，**口径与其它布尔开关不一致**
    # （未知取值会被当成"开"；而 ALLOW_ANON 那边又漏了 "on"）。启动校验已经拦住非法取值，
    # 所以统一口径不会改变合法配置的行为。
    return env_bool("WARDEN_STABILITY", True, env)


def _shared_state_from_env(env: Mapping[str, str]) -> bool:
    """是否把协调状态（幂等 / 事件流 / 限流计数 / Run 锁）放进共享存储。

    `WARDEN_SHARED_STATE=1` 开启 —— **多副本部署必须开**，否则每个副本各算一份：
    同一 Idempotency-Key 打到不同副本会重复执行、SSE 事件收不到、限流总额度翻倍。
    单副本默认关（进程内实现更快、无轮询）。

    解析统一走 `core.settings.env_bool`，避免同一个变量出现多个解析口径。
    """
    return env_bool("WARDEN_SHARED_STATE", False, env)


def trusted_proxies_from_env(env: Mapping[str, str] | None = None) -> str | None:
    """前置代理的地址/网段（`WARDEN_TRUSTED_PROXIES`）；未配置或空串返回 None。

    None 的含义是「谁也不信」：uvicorn 于是完全忽略 `X-Forwarded-For`，
    `request.client.host` 就是 TCP 对端（= 代理自己的 IP）。这是**安全的那一侧**默认，
    但它有一个副作用：在 Ingress 后面，所有匿名调用者的 `client.host` 全一样，
    「按调用者分桶」的限流与幂等作用域会塔成一个桶（一个客户端把别人挤掉）。
    所以放在反代后面必须配它，取值 = **代理自己的地址**。

    返回原字符串交给 uvicorn 解析（它的 `_TrustedHosts` 支持 IP / CIDR / 字面量列表）；
    格式把关在 `core.settings.validate_env` 里做（启动时就挑出非法值与全信任值）。
    """
    return env_str("WARDEN_TRUSTED_PROXIES", "", env).strip() or None


def _db_path() -> str:
    """SQLite 存档路径。

    容器里用 `WARDEN_DB_PATH` 指向可写卷 —— rootfs 设成只读（read_only: true）时，
    写 WORKDIR 会失败，必须显式给一个挂载卷里的路径。
    """
    return env_str("WARDEN_DB_PATH", "warden-agent-local.db")


def _store_from_env(env: Mapping[str, str]) -> Any:
    """按环境造存储：**配了 `WARDEN_PG_HOST` 就用 PostgreSQL，否则 SQLite**。

    为什么必须有这个开关：文档里"多副本部署：存储用 PostgreSQL"这句话此前**不可执行**——
    `run_server` 写死了 SQLite。于是"支持多副本"只存在于文档里（SQLite 是单机文件，
    两个副本各自一个库，幂等/事件/限流/Run 锁全部各自为政）。
    多副本要配套 `WARDEN_SHARED_STATE=1`（把协调状态也放进同一张库）。

    ⚠️ **已知边界**：审计与记忆目前**仍只有 SQLite 实现**，多副本下每个副本一份、彼此不一致。
    所以"多副本 + 审计/记忆"这个组合要么关掉这两项，要么等它们的 PG 实现（见运维手册已知边界）。
    """
    host = env_str("WARDEN_PG_HOST", "", env).strip()
    if not host:
        return SqliteStore(_db_path())

    from warden_agent.store.postgres import PostgresStore

    logger.info("存储：PostgreSQL（host=%s db=%s）", host, env_str("WARDEN_PG_DB", "warden", env))
    try:
        return PostgresStore(
            host=host,
            port=env_int("WARDEN_PG_PORT", 5432, env),
            dbname=env_str("WARDEN_PG_DB", "warden", env),
            user=env_str("WARDEN_PG_USER", "postgres", env),
            password=env_opt("WARDEN_PG_PASSWORD", env) or "",
        )
    except Exception as e:  # noqa: BLE001 - 连不上要**明确拒绝启动**，别悄悄退回 SQLite
        raise RuntimeError(
            f"无法连接 PostgreSQL（host={host}）：{e}\n"
            "  · 检查 WARDEN_PG_* 配置与网络连通性；\n"
            "  · **刻意不退回 SQLite**：那会让'以为在多副本共享、其实是各自一个库'，"
            "比直接起不来危险得多。"
        ) from e


def _event_keep_from_env(env: Mapping[str, str]) -> int:
    """进程内事件总线每个 run 保留的最近事件数（`WARDEN_EVENT_KEEP`，默认 500）。

    统一走 `core.settings` 的访问器：解析口径只有一处，写错会点名变量报错。
    """
    return env_positive_int("WARDEN_EVENT_KEEP", 500, env)


def _audit_key_status(env: Mapping[str, str], *, audit_enabled: bool, has_key: bool) -> str:
    """审计链密钥的启动口径：`off` / `keyed` / `unkeyed` / `required-missing`。

    默认（不设 `WARDEN_AUDIT_REQUIRE_KEY`）时未配密钥只告警、非致命；设了它则
    “审计开着却没密钥”直接拒绝启动——让“忘了配密钥”变成起不来，而不是静默降级。
    """
    if not audit_enabled:
        return "off"
    if has_key:
        return "keyed"
    return "required-missing" if env_bool("WARDEN_AUDIT_REQUIRE_KEY", False, env) else "unkeyed"


def _session_cache_max_from_env(env: Mapping[str, str]) -> int:
    """HTTP 会话内存缓存上限（`WARDEN_SESSION_CACHE_MAX`，默认 1000；<=0 = 不限）。

    统一走 `env_int`：解析口径只有一处；SessionRegistry 侧把 <=0 当“不限”。
    """
    return env_int("WARDEN_SESSION_CACHE_MAX", DEFAULT_SESSION_CACHE_MAX, env)


def _cognition_from_env(
    env: Mapping[str, str], catalog: ToolCatalog, model: AgentChatModel,
) -> tuple[Any, Any, int]:
    """按环境变量决定认知能力开关，返回 `(planner, intent, max_context_chars)`。

    产品路径的默认值（按"是否多花钱/是否只赚不赔"来定）：

      · **意图路由**：`ToolIntentRouter` 纯离线确定性、不让模型多跑一次 → **默认开**。
        调用前校验"该不该调"，疑似误调就提示模型而不是直接执行。
      - **上下文裁剪**：防长会话把上下文撑爆，纯收益 → **默认开**（阈值可覆盖）。
      · **阶段规划**：`ModelPlanner` 会为复杂任务**多花一次模型调用** → **默认关**，
        要开就设 `WARDEN_PLANNER=1`（值得，因为复杂任务本来就贵）。
    """
    intent = ToolIntentRouter(catalog)
    planner = (
        ModelPlanner(model)
        if env_bool("WARDEN_PLANNER", False, env)
        else None
    )
    limit = env_int("WARDEN_MAX_CONTEXT_CHARS", DEFAULT_MAX_CONTEXT_CHARS, env)
    return planner, intent, limit


def main() -> None:
    load_env()  # 先读 .env（可选），密钥从环境变量取，不硬编码
    setup_logging()
    # 存储：配了 WARDEN_PG_HOST 就是 PostgreSQL，否则 SQLite（见 _store_from_env）。
    # 多副本必须走 PostgreSQL + WARDEN_SHARED_STATE=1，否则各副本一个库、协调状态各自为政。
    store = _store_from_env(os.environ)

    # 模型：有 key 用真 DeepSeek，否则用假模型（离线可跑）。
    # 注意：假模型 + 对外服务 = 拒绝启动（见下面的 ensure_model_is_real）——
    # 所以这里不直接构造模型，而是把它放到「鉴权与监听地址都定下来之后」。

    # 配置面校验（fail-fast）：格式写错、或把 WARDEN_* 拼错，都在启动时暴露，
    # 而不是悄悄按默认值跑——与"鉴权 fail-closed"同一个取向。
    env_errors = validate_env(os.environ)
    if env_errors:
        for message in env_errors:
            logger.error("配置错误：%s", message)
        raise SystemExit(2)
    for suspect in unknown_warden_variables(os.environ):
        logger.warning(
            "环境变量 %s 未被识别——是不是拼错了？它不会生效（清单见 core/settings.py 或文档 15）",
            suspect,
        )
    # 生效配置全量清单（敏感值已打码），排查问题时开 DEBUG 即可看到
    logger.debug("生效配置：\n%s", "\n".join(describe(os.environ)))

    # 鉴权（fail-closed）。监听地址与鉴权要一起决定：对外监听却不鉴权 = 直接拒绝启动。
    host = env_str("WARDEN_HOST", "127.0.0.1")
    try:
        api_keys, auth_mode = resolve_auth(os.environ)
    except AuthConfigError as exc:
        logger.error("%s", exc)
        raise SystemExit(2) from exc

    try:
        ensure_listen_is_safe(host, auth_mode)
    except AuthConfigError as exc:
        logger.error("%s", exc)
        raise SystemExit(2) from exc

    if auth_mode == "bearer":
        logger.info("已开启 API 鉴权（请求需带 Authorization: Bearer <key>）")
    else:
        logger.warning(
            "WARDEN_ALLOW_ANON=1：未启用鉴权，且只监听本机 %s（开发用）。"
            "对外暴露端口前必须设置 WARDEN_API_KEY。", host,
        )

    # 模型：有真实凭据用真 DeepSeek，否则用假模型（离线可跑）。
    model: AgentChatModel
    api_key = env_opt("DEEPSEEK_API_KEY")
    if api_key:
        model = DeepSeekModel(api_key=api_key)
        logger.info("使用真实 DeepSeek 模型")
    else:
        model = FakeModel()
        # 降级到 info 是错的：假模型的回复**看起来像真能力**（会选工具、会给答案），
        # 日志里必须留下明显的痕迹，而且非本机开发场景直接拒绝启动。
        logger.warning(
            "未设置任何真实模型凭据 → 使用**离线假模型**：回复是固定脚本，不是真实推理。"
            "实例会被标记为演示（/capabilities 里 demo_instance=true）"
        )
    try:
        ensure_model_is_real(
            is_fake=isinstance(model, FakeModel),
            host=host, auth_mode=auth_mode, env=os.environ,
        )
    except DemoModelRefusedError as exc:
        logger.error("%s", exc)
        raise SystemExit(2) from exc

    # 沙箱隔离声明也要在这里 fail-fast：取值写错（如 kernel 拼错）不该等到
    # 「某次真的要用 shell.run」才报错——那是运气，不是治理。
    try:
        sandbox_isolation_policy(os.environ)
    except ValueError as exc:
        logger.error("配置错误：%s", exc)
        raise SystemExit(2) from exc

    audit_store: Any = None
    # 统一走 env_bool：此前这里是手写的 `in ("1","true","yes")`——**漏了 "on"**，
    # 与其它布尔开关的口径不一致（同一个变量的两种写法本来是这轮要消灭的东西）。
    if env_bool("WARDEN_AUDIT", False):
        # 审计后端**跟着主存储走**：PG 主存储 → PG 审计（多副本共享同一账本，链写入跨副本串行）；
        # 否则 SQLite。这样存储换 PG 时审计自动跟过去，不再需要"多副本记得关审计"这种部署约定。
        audit_store = audit_store_for_backend(store, sqlite_db_path=_db_path())
        logger.info("已开启审计（后端=%s）", type(audit_store).__name__)
    # 审计密钥：`WARDEN_AUDIT_REQUIRE_KEY=1` 时“开着审计却没密钥”直接拒绝启动（fail-closed）；
    # 默认仍非致命（只告警），但 /health/ready 会降级、/metrics 会暴露 warden_audit_unkeyed。
    if audit_store is not None:
        from warden_agent.web.audit import audit_chain_key

        _audit_status = _audit_key_status(
            os.environ, audit_enabled=True, has_key=audit_chain_key() is not None
        )
        if _audit_status == "required-missing":
            logger.error(
                "WARDEN_AUDIT_REQUIRE_KEY=1 但未配置 WARDEN_AUDIT_KEY：审计链会退化为"
                "不带密钥的哈希链（挡不住会重算整条链的人），拒绝启动。"
                "请配置 WARDEN_AUDIT_KEY，或取消该开关。"
            )
            raise SystemExit(2)
        if _audit_status == "unkeyed":
            logger.warning(
                "审计链未配置 WARDEN_AUDIT_KEY：退化为不带密钥的哈希链并继续运行"
                "（可设 WARDEN_AUDIT_REQUIRE_KEY=1 改为拒绝启动）。"
            )

    catalog = _build_catalog(os.environ)
    planner, intent, ctx_chars = _cognition_from_env(os.environ, catalog, model)
    # RAG：按环境变量决定要不要挂 knowledge.search，以及索引什么
    knowledge = _knowledge_from_env(os.environ)
    if knowledge is not None:
        from warden_agent.rag.knowledge import embedder_from_env

        if knowledge is not True and not os.path.isdir(str(knowledge)):
            logger.error("WARDEN_KNOWLEDGE 指向的目录不存在: %s", knowledge)
            raise SystemExit(2)
        logger.info(
            "RAG 知识库：%s（嵌入器=%s）",
            "内置离线语料" if knowledge is True else str(knowledge),
            embedder_from_env(os.environ)[1],
        )
    # 记忆落盘：USER 作用域是"跨会话的用户级记忆"，进程内实现会让它在语义上成立、
    # 实现上落空（重启即丢）。产品路径默认给一个落盘记忆库；后端同样跟着主存储走——
    # PG 主存储 → PG 记忆（多副本共享同一份记忆，否则 A 副本写的记忆 B 副本读不到）。
    from warden_agent.memory import PostgresMemoryStore, SqliteMemoryStore

    memory_repository: Any
    if getattr(store, "backend", "") == "postgres":
        memory_repository = PostgresMemoryStore(conn=store.new_connection())
        logger.info("记忆：PostgresMemoryStore（多副本共享同一份记忆）")
    else:
        memory_repository = SqliteMemoryStore(_db_path())
        logger.info("记忆：SqliteMemoryStore（落盘 %s，重启不丢）", _db_path())

    # Web 工具：默认离线 mock；WARDEN_WEB_FETCH=1 才换成真实联网抓取
    web_providers = providers_from_env(os.environ)
    if type(web_providers[1]).__name__ == "HttpFetchProvider":
        logger.warning(
            "web.fetch 已开启**真实联网抓取**：Agent 可主动访问公网。"
            "每跳都过 WebUrlPolicy（拒内网/环回/云元数据地址），但请确认这是你要的出口策略。"
        )
    else:
        logger.info("web 工具为离线 mock（设 WARDEN_WEB_FETCH=1 可开启真实联网抓取）")
    if type(web_providers[0]).__name__ == "HttpSearchProvider":
        logger.warning(
            "web.search 已开启**真实联网搜索**：Agent 可主动向搜索 API 发请求。"
            "已计入出站限速/配额（WARDEN_OUTBOUND_*）。"
        )
    else:
        logger.info("web.search 为离线 mock（设 WARDEN_SEARCH_PROVIDER 可接真实搜索 API）")
    # 多副本部署：把幂等 / 事件流 / 限流计数放进共享存储（同一个库），
    # 否则每个副本各算一份 —— 幂等失效、SSE 丢事件、限额翻倍。
    shared_state = _shared_state_from_env(os.environ)
    # 事件总线：poll（默认，轮询）或 notify（LISTEN/NOTIFY 唤醒，仅 Postgres 有效）。
    # notify 不改变正确性——事件仍落表、订阅仍读表，通知只把"睡满间隔"变成"变化即醒"。
    event_bus_mode = env_str("WARDEN_EVENT_BUS", "poll", os.environ).strip().lower()
    # 事件总线保留条数只解析一次，并且**同时**喂给 coordination_for 与 build_app：
    # 否则 build_app 会用默认值另建一套总线/保留策略，run_server 打的日志（下面的
    # `type(coordination[1])`）就与实际生效的那条总线对不上——日志在撒谎。
    event_keep = _event_keep_from_env(os.environ)
    coordination = coordination_for(
        store, shared=shared_state, event_bus=event_bus_mode,
        event_keep=event_keep,
    )
    logger.info(
        "协调状态：%s（事件总线 %s）",
        "共享存储（多副本）"
        if shared_state
        else "进程内（单副本；多副本请设 WARDEN_SHARED_STATE=1）",
        type(coordination[1]).__name__,
    )
    try:
        limiter = limiter_from_env(os.environ, store=coordination[2])
    except ValueError as exc:
        logger.error("%s", exc)
        raise SystemExit(2) from exc
    if limiter is None:
        logger.warning("限流已关闭（WARDEN_RATE_LIMIT=0）——仅建议在受信任的内网/网关后使用")
    else:
        logger.info(
            "限流：每 %s 秒最多 %s 次（按调用者/来源 IP；WARDEN_RATE_LIMIT 可调）",
            limiter.config.window_seconds, limiter.config.max_requests,
        )
    logger.info(
        "认知能力：意图路由=%s｜阶段规划=%s｜上下文裁剪=%s 字符",
        "开" if intent is not None else "关",
        "开" if planner is not None else "关（设 WARDEN_PLANNER=1 可开）",
        ctx_chars or "不裁剪",
    )
    # 链路追踪出口：配了 OTLP endpoint 才会把 span 送出去（否则只打结构化日志）。
    from warden_agent.core import otel

    if otel.otlp_config_from_env(os.environ) is not None:
        logger.info("链路追踪：span 将导出到 OTLP 收集器（OTEL_EXPORTER_OTLP_*）")
    else:
        logger.info("链路追踪：仅结构化日志（设 OTEL_EXPORTER_OTLP_ENDPOINT 可导出到 OTLP）")
    # 出站闸门：单次请求的超时/体积上限只界定"一次"，不界定"多少次"。
    # 计数与入站限流共用同一套存储接缝 → 多副本下把 store 换成存储实现，限额才是全局的。
    try:
        outbound = outbound_from_env(os.environ, store=coordination[2])
    except ValueError as exc:
        logger.error("%s", exc)
        raise SystemExit(2) from exc
    _oc = outbound.config
    logger.info(
        "出站限速：全局 %s/%ss｜单 host %s/%ss｜并发 %s%s"
        "（WARDEN_OUTBOUND_* 可调）",
        _oc.max_requests, _oc.window_seconds,
        _oc.host_max_requests, _oc.host_window_seconds,
        _oc.max_concurrency,
        f"｜日配额 {_oc.daily_quota}" if _oc.daily_quota else "｜日配额不限",
    )
    # Run 级锁：多副本下同一个环绕开"两个副本同时驱动"（会后写覆盖前写）。
    # 与协调状态共用同一个开关：开了 WARDEN_SHARED_STATE 就用存储里的共享锁。
    run_lock = run_lock_for(store, shared=shared_state)
    logger.info(
        "Run 锁：%s（HTTP 对话 / 审批路径；抢不到返回 423，让客户端稍后重试）",
        type(run_lock).__name__,
    )
    # 后台维护清扫（保留策略）：按 TTL 清理幂等表 / 限流表 / 过期记忆，避免库无界增长。
    from warden_agent.runtime.maintenance import MaintenanceSweeper

    maintenance = MaintenanceSweeper(
        store,
        interval_s=env_int("WARDEN_MAINTENANCE_INTERVAL_S", 300, os.environ),
        memory_repository=memory_repository,
        idempotency_ttl_s=float(env_int("WARDEN_IDEMPOTENCY_TTL_S", 86400, os.environ)),
    )
    maintenance.start()
    logger.info(
        "维护清扫：%s（间隔 %s 秒；WARDEN_MAINTENANCE_INTERVAL_S=0 可关）",
        "开" if maintenance.interval > 0 else "关",
        maintenance.interval,
    )
    # 入站请求保护：体积上限（413）与 SSE 长连接并发上限（503）。
    # 与限流（WARDEN_RATE_LIMIT，管“次数”）互补：一个管“一次能送多大”，一个管“同时多少条流”。
    max_request_bytes = env_int(
        "WARDEN_MAX_REQUEST_BYTES", DEFAULT_MAX_REQUEST_BYTES, os.environ
    )
    sse_max_concurrency = env_int(
        "WARDEN_SSE_MAX_CONNECTIONS", DEFAULT_SSE_MAX_CONCURRENCY, os.environ
    )
    session_cache_max = _session_cache_max_from_env(os.environ)
    # 多 Agent（主管 + 子 Agent 卡）：默认关。它不是零成本的装饰——子 Agent 会真的
    # 多花模型调用，所以由部署方显式打开（`WARDEN_MULTIAGENT=1`）。
    multiagent = env_bool("WARDEN_MULTIAGENT", False, os.environ)
    if multiagent:
        logger.info(
            "多 Agent：已注册 research / write 两张子 Agent 卡（子 Agent 只拿只读工具）；"
            "它们会额外发起模型调用——WARDEN_MULTIAGENT=0 可关。",
        )
    logger.info(
        "入站保护：请求体上限 %s｜SSE 并发上限 %s（WARDEN_MAX_REQUEST_BYTES / "
        "WARDEN_SSE_MAX_CONNECTIONS，0 = 关闭）",
        f"{max_request_bytes} 字节" if max_request_bytes > 0 else "关闭",
        sse_max_concurrency if sse_max_concurrency > 0 else "关闭",
    )
    app = build_app(
        model=model,
        catalog=catalog,
        policy=_build_policy(catalog),
        store=store,
        # 默认启用记忆与 Web 搜索（离线可跑）；技能/MCP/Git 按环境变量开启
        memory=True,
        memory_repository=memory_repository,
        knowledge=knowledge,
        web=True,
        web_providers=web_providers,
        skills=env_opt("SKILLS_DIR"),
        mcp_server=env_opt("MCP_SERVER"),
        git_workdir=env_opt("GIT_WORKDIR"),
        api_keys=api_keys,
        audit_store=audit_store,
        model_id=("deepseek" if api_key else "fake"),
        model_api_key=api_key,
        stability=_stability_from_env(os.environ),
        planner=planner,
        intent=intent,
        max_context_chars=ctx_chars,
        rate_limiter=limiter,
        multiagent=multiagent,
        shared_state=shared_state,
        # 把已经建好的事件总线与保留策略透传进去：`notify` 模式建的是
        # PostgresNotifyEventBus，若这里不传，build_app 会另建一条默认轮询总线，
        # 于是 LISTEN/NOTIFY 根本不生效，而启动日志却写着 notify。
        event_bus=coordination[1],
        event_keep=event_keep,
        outbound_limiter=outbound,
        run_lock=run_lock,
        maintenance=maintenance,
        max_request_bytes=max_request_bytes,
        sse_max_concurrency=sse_max_concurrency,
        session_cache_max=session_cache_max,
    )
    port = env_int("PORT", 8000)
    # 受信前置代理：**设了才透传**。不设时完全不传这个 kwarg，让 uvicorn 用它自己的默认
    # （只信回环）——默认值写死在这里就会跟 uvicorn 漂移。
    trusted_proxies = trusted_proxies_from_env(os.environ)
    if trusted_proxies:
        logger.info(
            "受信前置代理: %s（只采信来自它们的 X-Forwarded-For / X-Forwarded-Proto）",
            trusted_proxies,
        )
    else:
        logger.info(
            "未配 WARDEN_TRUSTED_PROXIES：不采信 X-Forwarded-For。"
            "在 Ingress/反代后面必须配置它，否则限流按调用者分桶会塌成一个桶"
        )
    logger.info("可视化控制台: http://127.0.0.1:%s/  (演示网页)", port)
    logger.info("OpenAPI 文档:  http://127.0.0.1:%s/docs", port)
    logger.info("健康检查:      /health/live  /health/ready")
    # `timeout_graceful_shutdown`：收到 SIGTERM 后**最多等 30 秒**把在飞的请求跑完，
    # 然后强制退出（app 的 lifespan 随后关闭存储/事件总线/记忆库）。
    # 不设这个上限的话，一个卡住的请求能让停机无限期挂住——滚动升级时表现为"旧副本不退"。
    #
    # 受信代理只在**配了**才交下去：不配时这个 kwarg 必须整个不出现，
    # 让 uvicorn 自己回退到 `FORWARDED_ALLOW_IPS` 环境变量/默认的"只信回环"
    # （tests/test_run_server_main.py 把这条钉成了断言）。
    #
    # 注解写成 `dict[str, Any]` 不是偷懒：`**` 解包转发 kwargs 时，mypy --strict
    # 会拿字典的值类型去逐参对 `uvicorn.run` 的整个签名，写 `str` 会直接报 10 个
    # `Argument 5 … **dict[str, str]` 错——而值本身就是"有就传、没有就没有"。
    proxy_kwargs: dict[str, Any] = (
        {"forwarded_allow_ips": trusted_proxies} if trusted_proxies else {}
    )
    uvicorn.run(
        app, host=host, port=port,
        timeout_graceful_shutdown=env_positive_int("WARDEN_SHUTDOWN_GRACE_S", 30, os.environ),
        **proxy_kwargs,
    )


if __name__ == "__main__":
    main()
