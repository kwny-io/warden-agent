"""配置面：环境变量的**单一事实源**（注册表 + 启动校验 + 冲突检测）。

为什么需要这一层（这是真实踩过的坑）：
  配置读取原先散在 8 个文件、24 处 `os.environ.get(...)`，没有任何地方声明
  "这个变量是干什么的、谁有权读它"。于是长出两类**静默出错**的 bug：

  1. **一名两用**：`WARDEN_API_KEY` 同时被 HTTP 鉴权（`web/run_server.py`）和 custom 模型的
     key（`model/deepseek.py`）读 → 用前者开鉴权、又用 custom 接自建网关时，模型会把
     **服务端的鉴权密钥**发给那个第三方网关。
     `WARDEN_BASE_URL` 同理：CLI 的服务地址 vs custom 模型的端点 → `warden chat` 发错地方。
  2. **拼错静默忽略**：`WARDEN_RATE_LIMIT` 写成 `WARDEN_RATELIMIT` 不报错，悄悄用默认值。
  3. **占位符静默生效**（第五十三批补）：`deploy/k8s/configmap.yaml` 带着 `REPLACE_ME-pod-cidr`
     这种占位符上线时，它**不是空值**——空值走默认分支，占位符会被当成真实取值一路用下去。
     最贵的一例：`WARDEN_TRUSTED_PROXIES=REPLACE_ME-pod-cidr` 会让 uvicorn 把非法 IP
     当字面量（它自己的源码注释就写着"用户无法知道自己写错了"）⇒ 没有任何对端能匹配
     ⇒ `X-Forwarded-For` 静默丢弃，而启动日志还在说"受信前置代理: …（只采信来自它们的 XFF）"。
     所以占位符在启动校验里直接判错（fail-closed，与鉴权同一取向）。

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

import ipaddress
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

# 「次数/窗口秒数」形式（限流/速率类配置），例如 600/60、120/60、0
_RATE_RE = re.compile(r"^\s*\d+\s*/\s*\d+\s*$")

# 部署模板里的占位符（`deploy/k8s/*.yaml`、`secret.example.yaml` 一律用 `REPLACE_ME[-_]…`）。
# 这是一类**特殊的错值**：看起来"填了"，其实没填。它比空值更危险——空值会走"未配置"分支，
# 占位符会被当成真实取值一路用下去（最贵的一例见模块 docstring 第 3 条：
# `WARDEN_TRUSTED_PROXIES=REPLACE_ME-pod-cidr` ⇒ XFF 静默丢弃 + 日志声称已配置）。
# 变体一起认：`REPLACE_ME` / `replace_me` / `replace-me` / `REPLACEME`。
_PLACEHOLDER_RE = re.compile(r"replace[ _-]?me", re.IGNORECASE)
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
        "WARDEN_APPROVAL_ALLOW_SELF",
        "单操作员部署的逃生门：显式设 1 才允许「自己批自己」（默认不允许）",
        "web/http_kit.py", ("web/http_kit.py",), kind="bool",
        note="职责分离（SoD）默认开启：发起人不能批自己的单，**admin 也不豁免**——"
             "否则拿到一个 user 凭证就能 `shell.run(rm -rf …)` 然后自己点批准。"
             "只为了单操作员/本机部署留这个口，失败方向选严格（默认撞 403，报错里告诉你这个开关）",
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
        "WARDEN_TRUSTED_PROXIES",
        "前置代理的地址/网段（逗号分隔）——**只有列在这里的对端**的 X-Forwarded-For 才被采信",
        "web/run_server.py", ("web/run_server.py",),
        kind="proxies",
        note="不配 = 不信任 XFF（uvicorn 默认只信回环）。于是 `request.client.host` 永远是"
             "代理的 IP，三件事一起退化：①按调用者分桶的限流会**塌成一个桶**（一个客户端"
             "把别人挤掉）；②幂等键的调用者作用域相撞——鉴权开启时作用域用 user_id，"
             "只在**鉴权关闭 + 反代部署**时才会串响应体；③审计里的来源 IP 只剩代理。"
             "写在 k8s Ingress / Nginx 后面就必须配它，取值 = 代理自身的地址（集群内网段），"
             "**不是客户端网段**。",
    ),
    EnvSpec(
        "WARDEN_DB_PATH", "SQLite 文件路径（容器 rootfs 只读时必须指向可写卷）",
        "web/run_server.py", ("web/run_server.py", "cli.py", "rag/loader.py"), kind="path",
        note="rag/loader.py 也读它：默认把 RAG 索引持久化到主库同目录"
             "（<db>-rag.db），避免重启重建向量。",
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
    EnvSpec(
        "WARDEN_AUDIT_REQUIRE_KEY",
        "审计开启但未配置 `WARDEN_AUDIT_KEY` 时**拒绝启动**（默认仅告警、非致命）",
        "web/run_server.py", ("web/run_server.py",), kind="bool",
        note="默认关：不配密钥时审计链退化为不带密钥的哈希链，仍能发现手改/删行，"
             "但挡不住会重算整条链的人。生产环境应配密钥并打开本开关，"
             "让\"忘了配密钥\"变成起不来，而不是静默降级",
    ),
    EnvSpec(
        "WARDEN_SESSION_CACHE_MAX",
        "HTTP 会话内存缓存上限（LRU 淘汰，防任意 run_id 把内存撑爆）；默认 1000，<=0 = 不限",
        "web/run_server.py", ("web/run_server.py",), default="1000", kind="int",
        note="任何 GET /status/<随机 id> 都会建一个 AgentSession；不透顶的话内存可无界增长。"
             "到上限按 LRU 淘汰最久未访问的会话（淘汰时关闭会话对象）",
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
        ("model/deepseek.py", "web/run_server.py", "demo_e2e.py", "evals/real_model.py"),
        sensitive=True,
        note=("多模块读取是合理的：都只是把它交给模型客户端（evals/real_model.py 用它跑"
              "真实模型质量评测，取不到就明确报错退出，不退回假模型）"),
    ),
    EnvSpec("OPENAI_API_KEY", "OpenAI 模型密钥", "model/deepseek.py",
            ("model/deepseek.py", "evals/real_model.py"), sensitive=True,
            note="evals/real_model.py：OPENAI_API_BASE 指向本机/兼容端点时的质量评测"),
    EnvSpec("ZHIPU_API_KEY", "智谱 GLM 模型密钥", "model/deepseek.py",
            ("model/deepseek.py",), sensitive=True),
    EnvSpec("DASHSCOPE_API_KEY", "阿里百炼模型密钥", "model/deepseek.py",
            ("model/deepseek.py",), sensitive=True),
    EnvSpec(
        "WARDEN_MODEL_API_KEY", "custom 通用接入的模型密钥",
        "model/deepseek.py", ("model/deepseek.py", "evals/real_model.py"), sensitive=True,
        note="**不是** WARDEN_API_KEY（那是鉴权）。本机网关/Ollama 填 not-needed 这类占位串",
    ),
    EnvSpec(
        "WARDEN_BASE_URL", "custom 通用接入的 OpenAI 兼容端点",
        "model/deepseek.py", ("model/deepseek.py", "evals/real_model.py"),
        note="**不是** CLI 要连的服务地址（那是 WARDEN_SERVER_URL）",
    ),
    EnvSpec("WARDEN_MODEL", "custom 通用接入的模型名", "model/deepseek.py",
            ("model/deepseek.py", "evals/real_model.py"), default="deepseek-chat"),
    EnvSpec(
        "WARDEN_ALLOW_FAKE_MODEL",
        "显式接受用「假模型」对外服务（回复是固定脚本，不是真实推理）",
        "web/run_server.py", ("web/run_server.py",), kind="bool",
        note="fail-closed 取向：非本机开发场景下用假模型会**拒绝启动**——"
             "假模型的回复看起来像真能力（会调工具、会答话），被当成真实部署会让人误判系统能力。"
             "演示实例请显式声明自己就是演示（启动日志与 /capabilities 都会如实标明）",
    ),
    EnvSpec(
        "WARDEN_DEMO_INSTANCE",
        "对外监听时的第二重演示声明（与 WARDEN_ALLOW_FAKE_MODEL 同时设才放行）",
        "web/run_server.py", ("web/run_server.py",), kind="bool",
        note="为什么要两个变量：生产配置里残留一句 ALLOW_FAKE_MODEL=1 这种事故，"
             "一个开关拦不住；第二个变量要的是一个只可能故意写出口的词",
    ),
    # ---- 能力开关 ----
    EnvSpec("WARDEN_STABILITY", "工具稳定性层（超时/重试/降级/熔断），0 关",
            "web/run_server.py", ("web/run_server.py",),
            default="1（产品入口默认开）", kind="bool"),
    EnvSpec("WARDEN_PLANNER",
            "阶段规划（默认开；**只对判定为复杂的任务**多发一次模型调用）",
            "web/run_server.py", ("web/run_server.py",),
            default="1（产品入口默认开）", kind="bool",
            note="简单任务零成本：plan_with_model 先跑离线复杂度启发式，不复杂就直接返回、"
                 "一次模型调用都不发（tests/test_planner_default.py 数调用次数钉住）。"
                 "规划调用失败/返回非 JSON 时降级成模板，不崩。设 0 关闭"),
    EnvSpec("WARDEN_MAX_CONTEXT_CHARS", "上下文裁剪的字符上限（0 = 不裁剪）",
            "web/run_server.py", ("web/run_server.py",), kind="int"),
    EnvSpec("WARDEN_UNTRUSTED_FENCE",
            "工具输出套\"这是数据不是指令\"的围栏（默认开；0 = 关闭，仅用于对照实验）",
            "loop/loop.py", ("loop/loop.py",), default="1", kind="bool",
            note="围栏是提示不是边界：它让模型知道这段是外部内容，但不能阻止模型照做"),
    EnvSpec("WARDEN_INJECTION_ESCALATE",
            "本轮工具输出命中注入话术后，高危工具退回人工拍板（默认开）",
            "loop/loop.py", ("loop/loop.py",), default="1", kind="bool"),
    EnvSpec("WARDEN_GATED_TOOLS",
            "被注入污染后需要人工拍板的工具名单（逗号分隔，覆盖默认集合）",
            "loop/loop.py", ("loop/loop.py",), kind="text",
            note="默认 shell.run / git.apply_patch / fs.write / fs.delete / fs.move / code.run；"
                 "读类工具**故意不在此列**——污染后连读都拦，会把正常排查也堵死"),
    EnvSpec("WARDEN_KNOWLEDGE", "RAG 知识来源：1 = 内置离线语料 / 目录路径",
            "web/run_server.py", ("web/run_server.py",), kind="text"),
    EnvSpec(
        "WARDEN_RAG_MODE",
        "RAG 检索模式：`hybrid`（默认）/ `lexical` / `semantic`",
        "rag/knowledge.py", ("rag/knowledge.py",),
        default="hybrid",
        note="hybrid = BM25 字面 + 向量两路 RRF 融合；lexical/semantic 是消融开关，"
             "用来单独看一条腿的贡献。**离线词频嵌入下 semantic 与 lexical 几乎等价**"
             "（词频哈希本身就是字面匹配），想看语义那一列的增益必须配 WARDEN_EMBED_*。"
             "取值写错抛 ValueError（构造 VectorStore 时即失败，不静默退回默认）",
    ),
    EnvSpec("WARDEN_WEB_FETCH", "开启真实联网抓取（默认离线 mock 不发请求）",
            "web/search.py", ("web/search.py",), kind="bool"),
    EnvSpec("WARDEN_SEARCH_PROVIDER",
            "真实联网搜索 provider：`tavily` / `brave` / `custom`；不设 = 离线 mock",
            "web/search.py", ("web/search.py",),
            note="tavily/brave 需配 WARDEN_SEARCH_API_KEY；custom 需配 WARDEN_SEARCH_ENDPOINT。"
                 "配不全如实退回 mock 并告警（不假装能搜）"),
    EnvSpec("WARDEN_SEARCH_API_KEY", "搜索 API 密钥（tavily/brave）",
            "web/search.py", ("web/search.py",), sensitive=True),
    EnvSpec("WARDEN_SEARCH_ENDPOINT", "自定义搜索端点（provider=custom 必填）",
            "web/search.py", ("web/search.py",)),
    EnvSpec("WARDEN_ALERT_WEBHOOK_URL",
            "告警投递 webhook（不配则不投递，只保留 CLI/HTTP 查询式告警）",
            "runtime/notify.py", ("runtime/notify.py",),
            note="`warden stuck --notify` 把'等待人工超时'的 Run POST 到这里"),
    EnvSpec("WARDEN_ALERT_WEBHOOK_HEADERS", "告警 webhook 请求头（`k=v,k2=v2`）",
            "runtime/notify.py", ("runtime/notify.py",), sensitive=True),
    EnvSpec("WARDEN_ALERT_WEBHOOK_TRUST_PROXY",
            "投递告警时是否让 httpx 读系统代理；默认 0（内网 webhook 不被代理转发）",
            "runtime/notify.py", ("runtime/notify.py",), default="0", kind="bool",
            note="设 1 才能经系统代理出网；失败日志会自己提示这个开关（见 runtime/notify.py）"),
    EnvSpec("WARDEN_MAINTENANCE_INTERVAL_S",
            "后台维护清扫间隔（秒）；默认 300，0 = 关闭（保留策略见 runtime/maintenance.py）",
            "web/run_server.py", ("web/run_server.py",), default="300", kind="int"),
    EnvSpec("WARDEN_IDEMPOTENCY_TTL_S",
            "幂等记录保留时长（秒）；默认 86400（超期由维护清扫删除）",
            "web/run_server.py", ("web/run_server.py",), default="86400", kind="int"),
    EnvSpec("GIT_WORKDIR", "把指定 git 仓库暴露为 git.apply_patch 工具",
            "web/run_server.py", ("web/run_server.py",), kind="path"),
    EnvSpec("WARDEN_GIT_PROBE",
            "git 基准探测实现：`direct`（直连 git）/ `broker`（经能力校验的受控探测）",
            "git/tools.py", ("git/tools.py",), default="direct",
            note="取值非法时 fail-closed 拒绝启动（不静默退回 direct）——"
                 "否则部署方以为在用受控探测、实际走直连，是静默降级"),
    EnvSpec("SKILLS_DIR", "技能目录（加载 SKILL.md）",
            "web/run_server.py", ("web/run_server.py",), kind="path"),
    EnvSpec("WARDEN_MULTIAGENT",
            "是否注册多 Agent 子 Agent 卡（research / write，主管模式）",
            "web/run_server.py", ("web/run_server.py",), default="0", kind="bool",
            note="默认关：子 Agent 会真的多花模型调用，所以不做默认开；"
                 "子 Agent 只拿 pure=True 的只读工具（带副作用的工具要过人工审批，"
                 "而子 Agent 跑在一次工具调用内部，没有审批对话通道）"),
    EnvSpec("MCP_SERVER", "MCP server 启动命令（需 node；工具先审查再导入）",
            "web/run_server.py", ("web/run_server.py",)),
    EnvSpec(
        "WARDEN_MCP_TIMEOUT_S",
        "单次 MCP 操作超时（秒）；默认 120，<=0 用默认",
        "mcp/client.py", ("mcp/client.py",),
        default="120", kind="int",
        note="冷启动（npx 解析/下载 + node 启动）在负载下可能超过 60s，所以默认放宽；"
             "只读的 list 超时后允许重试一次，call 绝不重试（可能有副作用）",
    ),
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
        "WARDEN_AUDIT_KEY", "审计链 / 归档清单的 HMAC 密钥材料（防篡改）",
        "core/settings.py", ("web/audit.py", "runtime/archive.py"),
        sensitive=True,
        note="不设则审计链退化为**不带密钥**的哈希链并告警：仍能发现「手改/删行」，"
             "但挡不住会重算整条链的人。审计链要跨重启校验，所以不生成临时密钥。"
             "读取走 core/settings.secret_bytes（tier 0），web/audit 与 runtime/archive 共用",
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
    # ---- 入站请求保护（体积上限 / SSE 并发上限）----
    EnvSpec(
        "WARDEN_MAX_REQUEST_BYTES",
        "【入站】请求体大小上限（字节）；超过返 413。默认 1048576（1 MiB），0 = 关闭",
        "web/run_server.py", ("web/run_server.py",),
        default="1048576", kind="int",
        note="只对带 body 的方法（POST/PUT/PATCH）生效：先看 Content-Length，"
             "没有（如 chunked）就边读边计数。防的是超大 body 把内存/解析打爆，"
             "与 WARDEN_RATE_LIMIT（限次数）互补。0 = 关闭（不建议对外监听时关）",
    ),
    EnvSpec(
        "WARDEN_SSE_MAX_CONNECTIONS",
        "【入站】/chat/stream（SSE 长连接）并发上限；超过返 503。默认 100，0 = 关闭",
        "web/run_server.py", ("web/run_server.py",),
        default="100", kind="int",
        note="长连接会一直占着线程/连接，不限并发时少量客户端就能耗尽连接池。"
             "到上限**立刻 503**（不排队，避免请求堆着一起超时）；流结束（含客户端断开）才释放名额",
    ),
    EnvSpec(
        "WARDEN_SSE_HEARTBEAT_SECONDS",
        "【入站】/chat/stream（SSE 长连接）空闲心跳间隔（秒）；默认 10，0 = 关心跳",
        "web/run_server.py", ("web/run_server.py",),
        default="10", kind="int",
        note="工具执行/长模型调用期间可能几十秒一个字节都不吐，中间层（nginx/CDN/浏览器）"
             "会把手里的连接当死连接掐掉，于是那一整轮回复白跑——心跳就是发一条不带的 data "
             "注释行保活。负数不需要单独判：kind=int 的格式校验用 `str.isdigit()`，"
             "`-1` 之类在启动时就被 `validate_env` 拦下（见 `_is_int`）",
    ),
    # ---- 可观测性 ----
    EnvSpec(
        "WARDEN_LOG_FORMAT",
        "日志格式：json（一行一个 JSON 对象，给采集端）/ text（人看的单行文本）；"
        "不配则按 stdout 是否终端自动选",
        "core/logging_setup.py", ("core/logging_setup.py",),
        default="auto",
        note="默认 auto = stdout 是终端给 text（开发机）、不是终端给 json（容器/CI/重定向）。"
             "为什么要分两档：结构化日志的价值在采集端（按 trace_id 串一次请求、按 level 告警），"
             "但把 JSON 强塞给人看是自伤。取值写错**拒绝启动**，不按默认值猜——"
             "否则上线后才发现采集端一无所获",
    ),
    EnvSpec(
        "WARDEN_TRACING", "链路追踪（W3C traceparent 传播 + span 日志），默认开，0 关",
        "core/tracing.py", ("core/tracing.py",),
        default="1", kind="bool",
        note="关掉后 span() 不碰 contextvar、开销接近零；出站与入站的链路上下文也随之不再生成",
    ),
    # ---- 配置来源 ----
    EnvSpec(
        "WARDEN_IGNORE_DOTENV",
        "不读 .env 文件，配置只从环境变量来（容器/k8s 与 e2e 用）",
        "core/config.py", ("core/config.py",),
        kind="bool",
        note="不设 = 照旧读 .env（开发默认）；设 1 = `load_env()` 根本不打开文件。"
             "为什么要它：`.env` 是**静默改行为**的一条路——工作目录里恰好有一个（容器镜像漏进去、"
             "或开发机本地文件）就会盖掉你没显式设的键，而 e2e 这种「确定性档」最怕这个："
             "档位会随跑的人的本地文件变。它只管「要不要读文件」，不管覆盖语义（后者永远是「不覆盖」）",
    ),
    # ---- 演示与开发（默认关，打开要显式声明） ----
    EnvSpec(
        "WARDEN_DEMO_TOOLS",
        "注册演示工具 weather.get / fs.delete（默认关；开了才能在工具目录里看到这两张演示卡）",
        "web/run_server.py", ("web/run_server.py",), default="0", kind="bool",
        note="这两张卡只为演示「审批闸门真的会拦」而存在：天气返回固定值、fs.delete "
             "**不会真的删文件**（返回值里也写明是演示卡）。它们原先是无条件注册的，"
             "于是任何启动 run_server 的实例都会拿到「已删除 xxx」的成功文本、磁盘却毫无变化——"
             "副作用声明与实际行为相反。现在默认不注册；打开时启动日志会打 WARNING",
    ),
    # ---- 前端与沙箱 ----
    EnvSpec("WARDEN_WEB_DIST", "前端静态文件目录", "web/routes/spa.py",
            ("web/routes/spa.py",), kind="path"),
    EnvSpec("WARDEN_ISOLATION_PREFIX", "沙箱隔离档前缀（容器/内核档探测）",
            "execution/sandbox.py", ("execution/sandbox.py",)),
    EnvSpec(
        "WARDEN_SANDBOX_ISOLATION",
        "沙箱隔离档声明：auto（默认，能上内核档就上）/ kernel（要求内核档，给不了就拒绝）/ "
        "semantic（显式接受「只有语义层把关」）",
        "execution/sandbox.py", ("execution/sandbox.py",), default="auto",
        note="为什么要显式声明：语义档**不是隔离**——它按命令长相的正则拦网络命令，"
             "python -c 里写一句 socket 就绕过去了。生产部署若对外声称「有隔离」，"
             "就该声明 kernel：声明与事实不符时是**启动失败**（fail-closed），"
             "而不是悄悄降级成看起来隔离。取值写错同样拒绝启动（不按默认值猜）",
    ),
    EnvSpec(
        "WARDEN_SANDBOX_REQUIRE_KERNEL",
        "布尔写法的「要求内核级隔离」，语义等同 WARDEN_SANDBOX_ISOLATION=kernel",
        "execution/sandbox.py", ("execution/sandbox.py",), kind="bool",
        note="给偏好布尔开关的部署方式留的口子；内核档不可得时同样 refuse（注册 shell.run 时抛错）",
    ),
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


def secret_bytes(name: str, env: Mapping[str, str] | None = None) -> bytes | None:
    """把一个环境变量当**密钥材料**读成 bytes；未配置（或空串）返回 None。

    放在 tier-0 是为了让不同层的模块共用同一份"密钥材料怎么读"的口径，
    又不破坏分层（审计链在 `web/audit.py`、归档清单在 `runtime/archive.py`，
    两者都要用同一个 `WARDEN_AUDIT_KEY`——低层不能反向 import 高层）。
    """
    material = env_str(name, "", env).strip()
    return material.encode("utf-8") if material else None


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
    """启动时校验已登记变量的**格式**（返回空 = 通过）。

    只查格式与"是不是还没填"，不查"该不该设"（那是各模块自己的语义，例如鉴权 fail-closed
    在 `resolve_auth`）。格式写错就该在启动时暴露，而不是悄悄按默认值跑——这跟"鉴权
    fail-closed"是同一个取向。

    占位符（`REPLACE_ME…`）在这一层统一判错，且**与变量类型无关**：它是"部署模板忘了
    替换"这件事的专用判据，只修某一个变量等于留下一个漏的（`WARDEN_TRUSTED_PROXIES` 静默
    丢 XFF、`WARDEN_ADMIN_PRINCIPALS` 变成"配了但没有管理员"、`WARDEN_PG_HOST` 连不上库）。
    """
    errors: list[str] = []
    for spec in ENV_SPECS:
        raw = env.get(spec.name)
        if raw is None or raw == "":
            continue
        value = raw.strip()
        if _PLACEHOLDER_RE.search(value):
            errors.append(
                f"{spec.name} 还是部署模板里的占位符：{spec.masked(raw)}。"
                "占位符**不是空值**：空值会走「未配置」分支，占位符会被当成真实取值用下去，"
                "于是表现成「日志说配了、机制没生效」。请换成真实取值，或整行删掉。"
                "（例：WARDEN_TRUSTED_PROXIES 留着占位符时，X-Forwarded-For 会被静默丢弃，"
                "限流按调用者分桶又塌成一个桶、Idempotency-Key 可跳调用者串响应体。）"
            )
            continue
        problem = _format_problem(spec, value, raw)
        if problem:
            errors.append(problem)
    # 已声明但不登记的键（见 `_EXTERNAL_ENV_KEYS`）：只受占位符检查约束——
    # 「给别的组件读」不代表留占位符就无所谓：`/metrics` 抓取认证失败会让
    # `up==0` 把 WardenServiceDown 打成长期 critical，真正该报的告警被淹没。
    for name in sorted(_EXTERNAL_ENV_KEYS):
        raw = env.get(name)
        if raw is None or raw == "":
            continue
        if _PLACEHOLDER_RE.search(raw.strip()):
            errors.append(
                f"{name} 还是部署模板里的占位符（值已打码）。它{_EXTERNAL_ENV_KEYS[name]}，"
                "留占位符会让 Prometheus 抓取认证失败（`/metrics` 401 ⇒ `up==0` ⇒ "
                "WardenServiceDown 打成长期 critical）。请换成真实取值，或整行删掉。"
            )
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
    if spec.kind == "proxies":
        bad = _proxies_problem(value)
        if bad:
            return (
                f"{spec.name} 取值可疑：{spec.masked(raw)}——{bad}。"
                "应为 IP 或网段（逗号分隔），例如 10.0.0.0/8,172.17.0.1。"
                "**取值是代理自己的地址，不是客户端网段**：写宽了等于信任任意客户端"
                "自带的 X-Forwarded-For（可伪造来源 IP，绕过按调用者分桶的限流）；"
                "写成 `*` / /0 这类全信任值一律拒绝。"
            )
    return None


def _proxies_problem(value: str) -> str | None:
    """检查 `WARDEN_TRUSTED_PROXIES` 的每一项，返回第一处问题的描述（None = 通过）。

    ⚠️ 占位符（`REPLACE_ME…`）不在这里判——那是"部署模板没替换"的通用判据，在
    `validate_env` 里对**所有**变量统一判（见 `_PLACEHOLDER_RE`）。这里的 `_looks_like_ip`
    只为区分"IP 写错一位"与"刻意写的字面量（unix socket 名）"。

    做这层检查是因为 uvicorn 自己的解析器**格式错也静默**（非法 IP 会被当成普通字面量，
    见 `uvicorn/middleware/proxy_headers.py` 里 `_TrustedHosts` 的注释），于是"网段写错一位"
    的表现是"XFF 忽然不生效了"，很难查。而写宽（`*`、`0.0.0.0/0`）的表现是"谁都能伪造来源 IP"，
    更危险——所以这两类在这里就判错（fail-closed，与鉴权同一取向）。
    """
    for item in value.split(","):
        host = item.strip()
        if not host:
            return "列表里有空项（多余逗号？）"
        if host in {"*", "0.0.0.0/0", "::/0"}:
            return f"`{host}` 等于信任所有对端"
        if "/" in host:
            try:
                net = ipaddress.ip_network(host, strict=False)
            except ValueError:
                return f"`{host}` 不是合法的网段"
            if net.prefixlen == 0:
                return f"`{host}` 等于信任所有对端"
        elif _looks_like_ip(host):
            try:
                ipaddress.ip_address(host)
            except ValueError:
                return f"`{host}` 不是合法的 IP"
        elif " " in host:
            return f"`{host}` 含空格"
    return None


def _looks_like_ip(host: str) -> bool:
    """像 IP（纯数字/点/冒号/十六进制），用于区分"IP 写错"与"刻意写的字面量（unix socket 名）"。"""
    return bool(host) and all(c in "0123456789abcdefABCDEF:.%" for c in host)


def _is_int(value: str) -> bool:
    """非负整数（配置里没有负数语义；`-1` 之类应当被判错）。"""
    return value.isdigit()


# k8s 会为同命名空间内的 Service 自动注入一组环境变量（`<SVC>_SERVICE_HOST`、
# `<SVC>_SERVICE_PORT[_<name>]`、`<SVC>_PORT_<port>_TCP[_ADDR|_PORT|_PROTO]`、`<SVC>_PORT`）。
# 它们跟着 `WARDEN_` 前缀混进来，但**不代表用户拼错**——不忽略的话，每个 pod 启动都会
# 刷一串假告警，真正的拼写错误反而被淹没。
_K8S_SERVICE_ENV_RE = re.compile(
    r"_(SERVICE_HOST|SERVICE_PORT|SERVICE_PORT_[A-Z0-9_]+"
    r"|PORT|PORT_[0-9]+_TCP(?:_ADDR|_PORT|_PROTO)?)$"
)

# 已声明、但**应用自己不读**的环境变量（第五十三批）。
#
# 它们必须与「拼写错误」区分开：`deploy/k8s/secret.example.yaml` 的 `WARDEN_METRICS_BEARER`
# 经 `envFrom: secretRef` 会进 pod 环境，而读它的是 **ServiceMonitor**
# （`deploy/observability/servicemonitor.yaml` 的 `bearerTokenSecret.key`——由 Prometheus
# 的抓取端从同一个 Secret 里取）。不声明的话，每个 pod 启动都会告警
# 「它不会生效——是不是拼错了？」，运维会去找一个并不存在的拼写错误；而值本身的问题
# （留了占位符）反而没人看。声明在这里的键：
#   ① 不再被当成拼写错误（`unknown_warden_variables`）；
#   ② **仍然**受启动校验的占位符检查约束（留占位符 = 抓取 401 = `up==0`）；
#   ③ 不登记进 `ENV_SPECS`——「登记了却没人读」是本仓库正在消灭的另一个形状。
_EXTERNAL_ENV_KEYS: dict[str, str] = {
    "WARDEN_METRICS_BEARER": "由 ServiceMonitor 从同一个 Secret 读取（应用本身不读）",
}


def external_env_names() -> frozenset[str]:
    """已声明、但由**部署里的其它组件**消费的变量名（见 `_EXTERNAL_ENV_KEYS`）。"""
    return frozenset(_EXTERNAL_ENV_KEYS)


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
        # k8s 注入的服务发现变量不是拼写错误（见 _K8S_SERVICE_ENV_RE）
        if _K8S_SERVICE_ENV_RE.search(name):
            continue
        # 已声明、但由部署里别的组件读的变量也不是拼写错误（见 _EXTERNAL_ENV_KEYS）
        if name in _EXTERNAL_ENV_KEYS:
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
