"""配置面：环境变量的**单一事实源**（注册表 + 启动校验 + 冲突检测）。

为什么需要这一层（这是真实踩过的坑）：
  配置读取原先散在 8 个文件、24 处 `os.environ.get(...)`，没有任何地方声明
  "这个变量是干什么的、谁有权读它"。于是长出两类**静默出错**的 bug：

  1. **一名两用**：`WARDEN_API_KEY` 同时被 HTTP 鉴权（`web/run_server.py`）和 custom 模型的
     key（`model/deepseek.py`）读 → 用前者开鉴权、又用 custom 接自建网关时，模型会把
     **服务端的鉴权密钥**发给那个第三方网关。
     `WARDEN_BASE_URL` 同理：CLI 的服务地址 vs custom 模型的端点 → `warden chat` 发错地方。
  2. **拼错静默忽略**：`WARDEN_RATE_LIMIT` 写成 `WARDEN_RATELIMIT` 不报错，悄悄用默认值。

  这两类问题的共同根因是"**没有一份声明**"。所以本模块提供：
    - `ENV_SPECS`：每个变量的登记（用途 / 归属模块 / **允许读它的模块** / 默认值 / 是否敏感）；
    - `validate_env()`：启动时校验格式（错误 → 拒绝启动，和鉴权 fail-closed 一个取向）；
    - `unknown_warden_variables()`：把拼错的 `WARDEN_*` 变量揪出来告警；
    - `tests/test_config_surface.py`：用 AST 扫描代码，强制"代码读的每个变量都已登记，
      且读取它的模块没超出声明的范围"——**没有这条测试，注册表就只是文档**。

层级约束：本模块在 tier 0（core），**不 import 任何上层模块**，所以格式校验是自包含的
（不复用 `web/ratelimit.py` / `web/outbound.py` 的解析器）。为防止两者漂移，
`tests/test_config_surface.py` 里有一条"校验口径与实际解析器一致"的对照测试。
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

# 「次数/窗口秒数」形式（限流/速率类配置），例如 600/60、120/60、0
_RATE_RE = re.compile(r"^\s*\d+\s*/\s*\d+\s*$")
# 布尔型开关的接受取值（其余一律视为"写错了"——避免 silent 忽略）
_BOOL_ON = frozenset({"1", "true", "yes", "on"})
_BOOL_OFF = frozenset({"", "0", "false", "no", "off"})


@dataclass(frozen=True)
class EnvSpec:
    """一个环境变量的登记项。

    `consumers` 是**允许读它的模块**（相对 `src/warden_agent/` 的路径）。
    AST 守卫测试会断言"实际读它的模块 ⊆ consumers"——这就是冲突检测的落点：
    若某天 `cli.py` 去读 model 的变量，测试立刻红。
    """

    name: str
    purpose: str
    owner: str  # 归属模块（主要负责方）
    consumers: tuple[str, ...]
    default: str = ""  # 人类可读的默认值描述（非空字符串表示"有这个默认"）
    kind: str = "text"  # text | bool | int | rate | path
    sensitive: bool = False  # 敏感值：日志/报错里不得打印其取值
    note: str = ""
    aliases: tuple[str, ...] = field(default=())

    def masked(self, value: str | None) -> str:
        """给日志/报错用的安全展示（敏感值一律打码）。"""
        if value is None:
            return "<未设置>"
        return "<已设置，已打码>" if self.sensitive else repr(value)


# ---------------------------------------------------------------------------
# 注册表：所有会被代码读取的环境变量，都必须登记在这里
# ---------------------------------------------------------------------------
ENV_SPECS: tuple[EnvSpec, ...] = (
    # ---- 鉴权与多租户 ----
    EnvSpec(
        "WARDEN_API_KEY",
        "单用户模式的服务端访问密钥（Bearer 鉴权）",
        "web/run_server.py", ("web/run_server.py",),
        kind="text", sensitive=True,
        note="fail-closed：既不设它/API_KEYS、也不设 WARDEN_ALLOW_ANON=1 → 拒绝启动。"
             "**只做鉴权**，不要拿它当模型 key（custom 用 WARDEN_MODEL_API_KEY）",
    ),
    EnvSpec(
        "WARDEN_API_KEYS",
        "多用户模式的密钥表：`alice:k1,bob:k2`，每个 key 绑定一个身份",
        "web/run_server.py", ("web/run_server.py",),
        kind="text", sensitive=True,
    ),
    EnvSpec(
        "WARDEN_API_USER",
        "单 key 模式下的用户名",
        "web/run_server.py", ("web/run_server.py",),
        default="demo-user",
    ),
    EnvSpec(
        "WARDEN_ALLOW_ANON",
        "显式接受「无鉴权」运行（仅本机/回环；对外监听时设它会拒绝启动）",
        "web/run_server.py", ("web/run_server.py",),
        kind="bool", note="不设 = 不无鉴权运行（fail-closed）",
    ),
    EnvSpec(
        "WARDEN_ADMIN_PRINCIPALS", "管理员 principal 名单（逗号分隔；**不配就没有管理员**）",
        "web/run_server.py", ("web/run_server.py", "web/auth.py"),
        note="管理员能看**全局**视图（/audit、/approvals/history、/alerts/stuck、"
             "/recovery/plan 不过滤归属）并切**部署级**默认模型（scope=deployment）。"
             "刻意不支持通配符 `*`——那等于一不小心全网开放；"
             "也刻意默认空（忘了配不该变成人人都是管理员）。"
             "auth.py 也读它：名单解析与角色判定的纯函数放在那一层（可单独测）",
    ),
    EnvSpec(
        "WARDEN_TENANT",
        "租户 id（审计与授权按它隔离）",
        "web/run_server.py", ("web/run_server.py",),
        default="local", note="注意它整租户共用，不能当用户级隔离用",
    ),
    EnvSpec(
        "WARDEN_VIEWER_PRINCIPALS",
        "只读角色名单（逗号分隔）；名单里的人不能发起/修改/审批，只能读",
        "web/auth.py", ("web/run_server.py", "web/auth.py"),
        note="用于给审计方/观察者只读访问。不配就没人被降为只读；"
             "同时出现在 admin 名单里时按 admin 处理（admin 优先）",
    ),
    # ---- 服务与存储 ----
    EnvSpec(
        "WARDEN_HOST", "监听地址（对外应设 0.0.0.0，但无鉴权时会拒绝启动）",
        "web/run_server.py", ("web/run_server.py",), default="127.0.0.1",
    ),
    EnvSpec(
        "PORT", "HTTP 端口", "web/run_server.py", ("web/run_server.py",),
        default="8000", kind="int",
    ),
    EnvSpec(
        "WARDEN_DB_PATH", "SQLite 文件路径（容器 rootfs 只读时必须指向可写卷）",
        "web/run_server.py", ("web/run_server.py", "cli.py"), kind="path",
    ),
    EnvSpec(
        "WARDEN_PG_HOST", "PostgreSQL 主机；**设了它就用 PostgreSQL**（否则用 SQLite）",
        "web/run_server.py", ("web/run_server.py", "cli.py"),
        note="多副本必须用 PostgreSQL（SQLite 是单机文件，跨主机共享文件系统不支持）。"
             "配套 WARDEN_SHARED_STATE=1 才让幂等/事件/限流/Run 锁进共享存储。"
             "cli.py 也读它：`warden audit-verify/export --pg` 要连审计所在的 PG",
    ),
    EnvSpec(
        "WARDEN_PG_PORT", "PostgreSQL 端口", "web/run_server.py",
        ("web/run_server.py", "cli.py"),
        default="5432", kind="int",
    ),
    EnvSpec(
        "WARDEN_PG_DB", "PostgreSQL 库名", "web/run_server.py",
        ("web/run_server.py", "cli.py"),
        default="warden",
    ),
    EnvSpec(
        "WARDEN_PG_USER", "PostgreSQL 用户", "web/run_server.py",
        ("web/run_server.py", "cli.py"),
        default="postgres",
    ),
    EnvSpec(
        "WARDEN_PG_PASSWORD", "PostgreSQL 密码", "web/run_server.py",
        ("web/run_server.py", "cli.py"),
        sensitive=True, note="放 Secret / 环境变量，别进 ConfigMap 或镜像",
    ),
    EnvSpec(
        "WARDEN_SHARED_STATE", "多副本共享协调状态（幂等/事件流/限流计数/Run 锁进数据库）",
        "web/run_server.py", ("web/run_server.py", "cli.py"), kind="bool",
        note="多副本必须开，否则实际限额 ≈ 配置值 × 副本数、且同一 run 会被并发驱动。"
             "cli.py 也读它：`warden recover --apply` 据此决定用共享 Run 锁还是进程内锁",
    ),
    EnvSpec(
        "WARDEN_AUDIT", "开启审计账本（每请求一条，写 audit_log 表）",
        "web/run_server.py", ("web/run_server.py",), kind="bool",
    ),
    # ---- 限流与配额（入站 / 出站 两个方向）----
    EnvSpec(
        "WARDEN_RATE_LIMIT", "【入站】每调用者速率，`次数/窗口秒数`",
        "web/ratelimit.py", ("web/ratelimit.py",), default="600/60", kind="rate",
    ),
    EnvSpec(
        "WARDEN_OUTBOUND_LIMIT", "【出站】全局速率，`次数/窗口秒数`",
        "web/outbound.py", ("web/outbound.py",), default="120/60", kind="rate",
    ),
    EnvSpec(
        "WARDEN_OUTBOUND_HOST_LIMIT", "【出站】单站点速率，`次数/窗口秒数`",
        "web/outbound.py", ("web/outbound.py",), default="20/60", kind="rate",
    ),
    EnvSpec(
        "WARDEN_OUTBOUND_MAX_CONCURRENCY", "【出站】同时在飞的请求上限",
        "web/outbound.py", ("web/outbound.py",), default="8", kind="int",
    ),
    EnvSpec(
        "WARDEN_OUTBOUND_DAILY_QUOTA", "【出站】日配额（0/不设 = 不限，防烧钱）",
        "web/outbound.py", ("web/outbound.py",), default="0", kind="int",
    ),
    # ---- 模型接入 ----
    EnvSpec(
        "DEEPSEEK_API_KEY", "DeepSeek 模型密钥", "model/deepseek.py",
        ("model/deepseek.py", "web/run_server.py", "demo_e2e.py"),
        sensitive=True, note="多模块读取是合理的：都只是把它交给模型客户端",
    ),
    EnvSpec("OPENAI_API_KEY", "OpenAI 模型密钥", "model/deepseek.py",
            ("model/deepseek.py",), sensitive=True),
    EnvSpec("ZHIPU_API_KEY", "智谱 GLM 模型密钥", "model/deepseek.py",
            ("model/deepseek.py",), sensitive=True),
    EnvSpec("DASHSCOPE_API_KEY", "阿里百炼模型密钥", "model/deepseek.py",
            ("model/deepseek.py",), sensitive=True),
    EnvSpec(
        "WARDEN_MODEL_API_KEY", "custom 通用接入的模型密钥",
        "model/deepseek.py", ("model/deepseek.py",), sensitive=True,
        note="**不是** WARDEN_API_KEY（那是鉴权）。本机网关/Ollama 填 not-needed 这类占位串",
    ),
    EnvSpec(
        "WARDEN_BASE_URL", "custom 通用接入的 OpenAI 兼容端点",
        "model/deepseek.py", ("model/deepseek.py",),
        note="**不是** CLI 要连的服务地址（那是 WARDEN_SERVER_URL）",
    ),
    EnvSpec("WARDEN_MODEL", "custom 通用接入的模型名", "model/deepseek.py",
            ("model/deepseek.py",), default="deepseek-chat"),
    # ---- 能力开关 ----
    EnvSpec("WARDEN_STABILITY", "工具稳定性层（超时/重试/降级/熔断），0 关",
            "web/run_server.py", ("web/run_server.py",),
            default="1（产品入口默认开）", kind="bool"),
    EnvSpec("WARDEN_PLANNER", "阶段规划（会多花一次模型调用，默认关）",
            "web/run_server.py", ("web/run_server.py",), kind="bool"),
    EnvSpec("WARDEN_MAX_CONTEXT_CHARS", "上下文裁剪的字符上限（0 = 不裁剪）",
            "web/run_server.py", ("web/run_server.py",), kind="int"),
    EnvSpec("WARDEN_KNOWLEDGE", "RAG 知识来源：1 = 内置离线语料 / 目录路径",
            "web/run_server.py", ("web/run_server.py",), kind="text"),
    EnvSpec("WARDEN_WEB_FETCH", "开启真实联网抓取（默认离线 mock 不发请求）",
            "web/search.py", ("web/search.py",), kind="bool"),
    EnvSpec("GIT_WORKDIR", "把指定 git 仓库暴露为 git.apply_patch 工具",
            "web/run_server.py", ("web/run_server.py",), kind="path"),
    EnvSpec("SKILLS_DIR", "技能目录（加载 SKILL.md）",
            "web/run_server.py", ("web/run_server.py",), kind="path"),
    EnvSpec("MCP_SERVER", "MCP server 启动命令（需 node；工具先审查再导入）",
            "web/run_server.py", ("web/run_server.py",)),
    # ---- 向量检索 ----
    EnvSpec("WARDEN_EMBED_BASE_URL", "真语义嵌入端点（不配则用词频哈希，非语义检索）",
            "rag/knowledge.py", ("rag/knowledge.py",)),
    EnvSpec("WARDEN_EMBED_API_KEY", "嵌入端点密钥", "rag/knowledge.py",
            ("rag/knowledge.py",), sensitive=True),
    EnvSpec("WARDEN_EMBED_MODEL", "嵌入模型名", "rag/knowledge.py", ("rag/knowledge.py",)),
    # ---- 凭证 ----
    EnvSpec(
        "WARDEN_CREDENTIAL_KEY", "凭证加密的密钥材料（只从环境变量读）",
        "credential/crypto.py", ("credential/crypto.py", "credential/broker.py",
                                  "credential/kms.py"),
        sensitive=True,
        note="不设则用进程内临时密钥（能加密，但重启后解不开）并告警",
    ),
    EnvSpec(
        "WARDEN_CREDENTIAL_OLD_KEYS", "凭证密钥轮换期的历史密钥（逗号分隔，仅用于解密）",
        "credential/broker.py", ("credential/broker.py", "credential/kms.py"),
        sensitive=True,
        note="配新主密钥 + 旧密钥放这里 → 跑 `warden rotate-credentials` 重加密 → 摘掉旧密钥。"
             "不配它而直接换主密钥 = 存量密文全部解不开。"
             "kms.py 也读它：env 模式的 provider 需要解析历史密钥做解密兜底",
    ),
    # ---- 密钥托管（KMS/HSM 信封加密）----
    EnvSpec(
        "WARDEN_KMS_PROVIDER", "凭证主密钥的托管方式：env（默认）/ aws-kms / vault-transit",
        "credential/kms.py", ("credential/kms.py",),
        default="env",
        note="env = 材料直接来自 WARDEN_CREDENTIAL_KEY（演进前行为）。配成 KMS 后，"
             "根密钥待在 KMS/HSM 里，应用只解开被包装的 DEK（信封加密）。取值不认识会直接报错，"
             "**不静默回落**——否则会「以为在托管、其实没托管」",
    ),
    EnvSpec(
        "WARDEN_KMS_WRAPPED_KEY",
        "被 KMS 包装过的数据密钥（DEK）：aws-kms 为 base64，vault-transit 为密文串",
        "credential/kms.py", ("credential/kms.py",),
        sensitive=True,
        note="生成方式见 docs/operations.md（AWS CLI / vault CLI 各一条命令）",
    ),
    EnvSpec(
        "WARDEN_VAULT_ADDR", "HashiCorp Vault 地址（vault-transit 模式）",
        "credential/kms.py", ("credential/kms.py",),
    ),
    EnvSpec(
        "WARDEN_VAULT_TOKEN", "Vault 访问令牌（vault-transit 模式）",
        "credential/kms.py", ("credential/kms.py",), sensitive=True,
    ),
    EnvSpec(
        "WARDEN_VAULT_KEY_NAME", "Vault transit 的密钥名（vault-transit 模式）",
        "credential/kms.py", ("credential/kms.py",),
    ),
    EnvSpec(
        "WARDEN_EVENT_BUS", "事件总线实现：poll（轮询，默认）或 notify（LISTEN/NOTIFY 唤醒）",
        "web/run_server.py", ("web/run_server.py",),
        default="poll",
        note="notify 仅对 Postgres 有效（需要另开一条 LISTEN 连接）；不满足时回落为轮询并告警。"
             "它只降低延迟，正确性仍靠落表+读表——丢通知不会丢事件",
    ),
    EnvSpec(
        "WARDEN_AUDIT_KEY", "审计链的 HMAC 密钥材料（防篡改）",
        "web/audit.py", ("web/audit.py",),
        sensitive=True,
        note="不设则审计链退化为**不带密钥**的哈希链并告警：仍能发现「手改/删行」，"
             "但挡不住会重算整条链的人。审计链要跨重启校验，所以不生成临时密钥",
    ),
    EnvSpec(
        "WARDEN_SHUTDOWN_GRACE_S", "收到 SIGTERM 后等多久把在飞请求跑完（秒），默认 30",
        "web/run_server.py", ("web/run_server.py",),
        default="30", kind="int",
        note="超时后强制退出并关资源。不设上限时，一个卡住的请求会让停机无限期挂住"
             "（滚动升级时表现为旧副本不退）",
    ),
    EnvSpec(
        "WARDEN_EVENT_KEEP", "【进程内事件总线】每个 run 最多保留的最近事件数（默认 500）",
        "web/run_server.py", ("web/run_server.py",),
        default="500", kind="int",
        note="这是**保留条数**上限：消费者在两次轮询之间积累超过它会丢最早的几条"
             "（只影响进度展示；结果与消息走 messages/存档，不受影响）。丢弃时会打警告，"
             "消费者侧也会收到「事件流存在缺口」的提示。多副本用 SqlEventBus 时此项不生效",
    ),
    # ---- 可观测性 ----
    EnvSpec(
        "WARDEN_TRACING", "链路追踪（W3C traceparent 传播 + span 日志），默认开，0 关",
        "core/tracing.py", ("core/tracing.py",),
        default="1", kind="bool",
        note="关掉后 span() 不碰 contextvar、开销接近零；出站与入站的链路上下文也随之不再生成",
    ),
    # ---- 前端与沙箱 ----
    EnvSpec("WARDEN_WEB_DIST", "前端静态文件目录", "web/server.py",
            ("web/server.py",), kind="path"),
    EnvSpec("WARDEN_ISOLATION_PREFIX", "沙箱隔离档前缀（容器/内核档探测）",
            "execution/sandbox.py", ("execution/sandbox.py",)),
    # ---- CLI ----
    EnvSpec(
        "PGPASSWORD", "PostgreSQL 密码（`warden backup-pg` 传给 pg_dump 用）",
        "cli.py", ("cli.py",), sensitive=True,
        note="**不放命令行**（命令行会进 ps/history）；这是 libpq 的标准变量名",
    ),
    EnvSpec(
        "WARDEN_SERVER_URL", "CLI 要连的 Warden 服务地址",
        "cli.py", ("cli.py",), default="http://127.0.0.1:8000",
        note="**不是** WARDEN_BASE_URL（那是 custom 模型的端点）",
    ),
    # ---- OTLP 链路导出（标准 OTEL 变量名，交给运维熟悉的约定）----
    EnvSpec(
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTLP 收集器基地址（如 http://localhost:4318）；给了它即开启 span 导出",
        "core/otel.py", ("core/otel.py",),
        note="标准 OTEL 变量名。未设且未设 TRACES_ENDPOINT → 不导出（只打结构化日志）",
    ),
    EnvSpec(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTLP traces 专用完整地址（优先于基地址；如 http://localhost:4318/v1/traces）",
        "core/otel.py", ("core/otel.py",),
    ),
    EnvSpec(
        "OTEL_SERVICE_NAME", "OTLP resource 里的 service.name",
        "core/otel.py", ("core/otel.py",), default="warden-agent",
    ),
    EnvSpec(
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTLP 请求头（`k=v,k2=v2`；如接托管后端带鉴权头）",
        "core/otel.py", ("core/otel.py",), sensitive=True,
        note="可能含鉴权 token，按敏感值处理（日志里打码）",
    ),
)

_BY_NAME: dict[str, EnvSpec] = {spec.name: spec for spec in ENV_SPECS}


def registered_env_names() -> frozenset[str]:
    """已登记的全部变量名（供 AST 守卫测试使用）。"""
    return frozenset(_BY_NAME)


def spec_of(name: str) -> EnvSpec | None:
    return _BY_NAME.get(name)


def env_flag(raw: str | None) -> bool:
    """把一个布尔型开关的**原文**解析成 bool（`1/true/yes/on` 为真，其余为假）。

    统一放在这里，是为了避免"同一个变量在几个模块里各写一套解析"——那正是本项目出过
    问题的地方（`WARDEN_API_KEY` / `WARDEN_BASE_URL` 各被两个模块读）。

    刻意接收**原文**而不是 `(env, name)`：这样变量名在调用处是字面量（`env.get("X")`），
    `tests/test_config_surface.py` 的 AST 守卫才扫得到"谁读了哪个变量"。
    （写成本函数时先写成 `(env, name)`，守卫立刻报"WARDEN_SHARED_STATE 已登记却没人读"——
    这正是那条守卫的用处。）
    """
    return (raw or "").strip().lower() in _BOOL_ON


# ---------------------------------------------------------------------------
# 类型化访问器：各模块读配置的统一入口
# ---------------------------------------------------------------------------
# 为什么要有它们：此前各模块自己 `os.environ.get("X")` 再手写 `int(...)` / 布尔判断，
# 于是"同一个变量在不同模块解析口径不一致""写错了报错信息五花八门"这类问题迟早出现。
# 访问器把**解析**收口到一处，各模块只声明"我要哪个变量、默认值是什么"。
#
# 关键约定：变量名在**调用处是字面量**（`env_int("PORT", 8000)`），
# `tests/test_config_surface.py` 的 AST 守卫据此仍然能回答"谁读了哪个变量"。
# 所以**不要**把变量名放进变量再传进来（`env_int(name_var)` 守卫会扫不到）。

def _source(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def env_str(name: str, default: str = "", env: Mapping[str, str] | None = None) -> str:
    """读一个字符串配置；**未设置或空串**都返回 default（env 里空串通常等于"没配"）。"""
    raw = _source(env).get(name)
    return default if raw is None or raw == "" else raw


def env_opt(name: str, env: Mapping[str, str] | None = None) -> str | None:
    """读一个字符串配置，但**区分"未设置"(None) 与"空串"**（需要这个区分时用它）。"""
    return _source(env).get(name)


def env_int(name: str, default: int = 0, env: Mapping[str, str] | None = None) -> int:
    """读一个整数配置；未设置/空串返回 default，写错则**报错并点名变量**。"""
    raw = _source(env).get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError as e:
        raise ValueError(f"{name} 应为整数，实际为 {raw!r}") from e


def env_bool(name: str, default: bool = False, env: Mapping[str, str] | None = None) -> bool:
    """读一个布尔开关；未设置返回 default，否则按 `env_flag` 的口径判定。"""
    raw = _source(env).get(name)
    if raw is None:
        return default
    return env_flag(raw)


def env_positive_int(name: str, default: int, env: Mapping[str, str] | None = None) -> int:
    """读一个**正整数**配置；<=0 视为"用默认"（很多开关用 0 表示"关/不限"，不适用这里）。"""
    value = env_int(name, default, env)
    return value if value > 0 else default


def _is_truthy(value: str) -> bool:
    """布尔开关取值的宽松判定（与各模块运行时的判定保持一致的口径）。"""
    return value.strip().lower() in _BOOL_ON


def validate_env(env: Mapping[str, str]) -> list[str]:
    """启动时校验已登记变量的**格式**。返回错误清单（空 = 通过）。

    只查格式，不查"该不该设"（那是各模块自己的语义，例如鉴权 fail-closed 在 `resolve_auth`）。
    格式写错就该在启动时暴露，而不是悄悄按默认值跑——这跟"鉴权 fail-closed"是同一个取向。
    """
    errors: list[str] = []
    for spec in ENV_SPECS:
        raw = env.get(spec.name)
        if raw is None or raw == "":
            continue
        value = raw.strip()
        problem = _format_problem(spec, value, raw)
        if problem:
            errors.append(problem)
    return errors


def _format_problem(spec: EnvSpec, value: str, raw: str) -> str | None:
    """按登记的类型检查格式，返回错误描述（None = 通过）。"""
    if spec.kind == "int" and not _is_int(value):
        return f"{spec.name} 格式错误：{spec.masked(raw)}。应为整数（用途：{spec.purpose}）"
    if spec.kind == "bool" and value.lower() not in _BOOL_ON | _BOOL_OFF:
        return (
            f"{spec.name} 取值可疑：{spec.masked(raw)}。"
            "应为 1/0（或 true/false、yes/no、on/off）"
        )
    if (
        spec.kind == "rate"
        and value.lower() not in _BOOL_OFF
        and not _RATE_RE.match(value)
    ):
        return (
            f"{spec.name} 格式错误：{spec.masked(raw)}。"
            "应为 `次数/窗口秒数`，例如 600/60（或 0 关闭）"
        )
    return None


def _is_int(value: str) -> bool:
    """非负整数（配置里没有负数语义；`-1` 之类应当被判错）。"""
    return value.isdigit()


def unknown_warden_variables(env: Mapping[str, str]) -> list[str]:
    """找出"看起来是我们家的、但没登记"的变量名——**拼写错误检测**。

    典型：`WARDEN_RATE_LIMIT` 敲成 `WARDEN_RATELIMIT`，原先会被静默忽略、悄悄用默认值，
    是最难查的一类问题。这里把它拎出来告警。
    """
    known = registered_env_names()
    suspects: list[str] = []
    for name in env:
        if name in known or not name.startswith("WARDEN_"):
            continue
        # 未登记的 WARDEN_* 一律可疑（本项目的 WARDEN_ 前缀都被登记完了）
        suspects.append(name)
    return sorted(suspects)


def describe(env: Mapping[str, str], *, sensitive_shown: bool = False) -> list[str]:
    """给启动日志用的"当前生效配置"清单（敏感值打码）。"""
    lines: list[str] = []
    for spec in ENV_SPECS:
        raw = env.get(spec.name)
        shown = spec.masked(raw) if not sensitive_shown else (raw or "<未设置>")
        if raw is None and spec.default:
            shown = f"<未设置，用默认 {spec.default}>"
        lines.append(f"  {spec.name} = {shown}   # {spec.purpose}")
    return lines
