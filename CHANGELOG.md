# Changelog

> 本文件按**时间戳**记录每次提交改动的内容,以及**尚未实现**的待办(未实现/路线图)。
> 每次提交都同步更新本文件,保持「git 历史 ↔ 文档」一致。

---

## 2026-09-04

### 已提交

- **初始化发布**:项目从 haifa-agent-py 独立重构并改名为 **warden-agent**。
  - 包名/目录:haifa_agent → warden_agent
  - 环境变量/库名/URN/HTTP 头:HAIFA_* → WARDEN_*(warden-agent-local.db、WARDEN_API_KEY、X-Warden-Api-Version 等)
  - MIT License、Docker、pyproject、.env.example 就绪
- **删除公开仓库里的复刻向学习笔记**(docs/ 下 4 份:演进路线、全文件详解、面试准备指南、项目完整流程)——这些是个人自用学习材料,保留在本地桌面,不进公开仓库。
- **全项目清除旧名/出处引用**(haifa / 原版 / 对标 Java),注释与品牌统一为 Warden。
- **测试**:常见 162 passed, 1 skipped(Postgres 集成测试无库自动跳过)。
- **运行验证**:HTTP 服务启动、/health、/chat(真实 DeepSeek)、审批门禁(WAITING_APPROVAL→reject)、离线假模型均实测通过。
- **T3 CLI 命令行入口(warden)完成**:
  - 子命令:`warden chat` / `stream` / `approvals` / `approve` / `reject` / `health` / `caps`
  - 复用已有 HTTP 端点;默认连 127.0.0.1:8000,可用 `WARDEN_BASE_URL` 覆盖
  - 实测:chat 触发审批 → approvals 看队列 → approve/reject 决策,全过程跑通
  - 修复两个真实 bug:① httpx 默认走系统代理导致 127.0.0.1 被代理转发返回 502(改 trust_env=False);② chat 子命令残留死代码引用了不存在的属性导致 AttributeError
  - 新增 `tests/test_cli.py`(4 项)。全量测试 **166 passed, 1 skipped**
  - 新增 `[project.scripts] warden = "warden_agent.cli:main"`(pyproject)
- **T5 SDK 化(README 三行接入)完成**:README 开头新增"三行接入"小节,展示 `pip install` + `build_agent()` 一行装配 + `chat()` 的库用法;`pip install -e .` 已验证可安装、可 import。
- **T2+T7 HTTP contract 完成**:
  - **统一 API 版本头**:所有 HTTP 响应统一注入 `X-Warden-Api-Version: 1.0`(中间件层统一加,不只 problem 响应)
  - **幂等 Idempotency-Key**:POST 请求带 `Idempotency-Key`,同 key 重复请求返回同一份缓存结果(不重跑);5xx / SSE 流式响应不缓存
  - **统一错误码契约**:`API_ERROR_CODES` 收敛 400/401/403/404/409/500/503,problem+json 响应含 `errorCode` 字段(如 `AUTHENTICATION_REQUIRED`)
  - **多 run 幂等(T7)**:`SessionRegistry` 按 run_id 复用会话,已 COMPLETED 的 run 不重复初始化/重跑,配合 Idempotency-Key 幂等
  - 新增 `tests/test_web_contract.py`(6 项)。全量测试 **172 passed, 1 skipped**
  - 过程中修复幂等缓存的实现(中间件拿到的 body 是异步流,需 `async for` 消费并重建响应)
- **T4 Coding Agent 雏形完成**:
  - 新增 `coding_agent/` 模块:`run_coding_task()` 给需求 → 读代码 → 出 unified diff → 走 `git.apply_patch` 门禁落地(不自动 commit/push)
  - 新增两个**受限代码浏览工具**:`code.list`(列目录) / `code.read`(读文件,64KB 上限),均做 workdir 边界校验(防 `..` 逃逸/绝对路径读取 workdir 之外)
  - 复用 `build_agent` + `git/`(make_git_tools、GitWorktreeCoordinator)已有能力,不重复造轮子
  - 接入 CLI:`warden coding "<需求>" --workdir <仓库>` 本地跑(不需 HTTP 服务)
  - 新增 `tests/test_coding_agent.py`(5 项,含路径穿越安全测试)。全量测试 **177 passed, 1 skipped**
- **T1 执行沙箱/隔离完成**:
  - 新增 `execution/sandbox.py`:`SandboxSpec` / `NetworkPolicy` / `SandboxedExecutionBroker`
  - **只读工作区**:把输入目录拷进临时工作区并设只读,命令跑在副本上,改不到宿主,跑完即弃
  - **网络策略 NetworkPolicy(默认禁网)**:默认拒绝 curl/wget/ping/nc/http/urllib 等疑似网络命令;`allow_network=True` 放行
  - **资源限制(内存/CPU/文件)**:在 `ExecutionBroker.execute` 造子进程时按平台应用真实限制——POSIX 用 `resource.setrlimit`(RLIMIT_AS 内存 / RLIMIT_CPU / RLIMIT_NOFILE),Windows 用 Job Object(pywin32,限进程内存/JOB_TIME/活动进程数),避免 fork 子进程逃逸
  - 诚实标注边界:这是"应用层资源限制"(跨平台可用),非内核级 cgroup / OS 沙箱(bubblewrap / Seatbelt)
  - 新增 `tests/test_sandbox.py`(9 项,含资源限制、路径穿越安全、make_limiter 触发)。全量测试 **186 passed, 1 skipped**
- **T6 跨 run 协调恢复完成**:
  - `SqliteStore.list_checkpoints()`:枚举所有 run 的存档点(抽出统一的 `_decode_checkpoint()` 解码器,供单查/枚举复用)
  - `SqliteCheckpointStore.list()`:接上底层枚举,为协调恢复提供"看全部存档"的能力
  - 新增 `runtime/recovery.py` 的 `RecoveryController`:读全部 checkpoint 按状态机分组——终态跳过 / RUNNING 等续跑 / FAILED 重试/判终态 / 等审批·交互·暂停的等人工
  - `Checkpoint` 新增 `attempts` 字段(默认 1,向后兼容),记录该 run 累计尝试圈数,`RecoveryController` 靠它决定"FAILED 是否还重试"(`max_attempts_per_run` 上限防无限循环)
  - 测试驱动修正:最初用"按 run_id 计 checkpoint 条数"估 Attempts 是错的(store 按 run 覆盖写,每 run 恒为 1 条),测试当场抓住,改为把 attempts 落在 Checkpoint 上
  - 新增 `tests/test_recovery.py`(7 项)。全量测试 **190 passed, 1 skipped**
- **T10 Web 前端产品化完成(React + TS + Tailwind)**:
  - 新增 `web/` 前端项目(Vite):组件化交互式控制台——`ChatView`(SSE 流式打字机 + 内联审批)、`ApprovalPanel`(审批队列)、`InfoPanel`(run 状态/能力/记忆/健康)
  - 对接后端全部接口:`POST /chat/stream/{run_id}`(SSE)、`/approvals`、`/approve|reject`、`/capabilities`、`/memory`、`/health`
  - FastAPI **单端口托管 SPA**:`server.py` 新增 `_find_spa_index()` 优先喂 `web/dist` 的 React 构建产物,并 `app.mount("/assets")` 托管静态 JS/CSS;未构建时自动回退旧版演示控制台
  - 开发模式:Vite(5173) 经 `vite.config.ts` 代理 `/chat`,`/events` 等到后端(8000),前后端分离热更新
  - Docker 多阶段构建:阶段1 node 构建前端 → 阶段2 Python 后端 + 放进 `/app/web/dist` 托管,单镜像部署
  - 修正两个路径 bug:`_repo_dir()` 向上 3 层定位仓库根(原多跳一层导致找不到 dist)、`..` 冗余;新增 `web/README.md`、更新 `.dockerignore`
  - 更新 `tests/test_web.py`(首页改返回构建产物 + 新增 `test_t10_react_spa_服务和资源`)。全量测试 **192 passed, 1 skipped**

## 2026-09-05

### 已提交

- **T8 可观测性(指标 + /metrics)完成**:
  - 新增 `core/metrics.py`:零第三方依赖、线程安全的进程内指标注册表,输出 Prometheus text 格式(Counter 计数器 / Gauge 瞬时值 / Histogram 耗时分布),`/metrics` 可被 Prometheus / Grafana 直接抓
  - `web/server.py` 网关中间件埋点:请求总数、耗时直方图、5xx 错误数;新增 `GET /metrics` 文本出口(不占用业务路由)
  - **收尾修复 3 个真 bug**(见私人文档「排查日志」):
    ① `histogram()` 原返回 None,调用方 `observe()` 直接 AttributeError → 改为返回 `Histogram` 调查柄,桶在注册时绑定
    ② `Gauge.inc/dec` 原"读-改-写"在无标签键上误写,`set→dec` 后键分裂、render 时 IndexError → 统一用锁内原子 `_add_gauge`
    ③ `observe` 分桶 `+Inf` 计数重复、`_sum` 恒为 0 → 修正桶边界 + 真实累加总和
  - **收尾过一遍质量门禁**:`metrics.py` + `server.py` 过 `mypy --strict` 与 `ruff`(全绿);`/approve|reject` 接入 `warden_approvals_total` 指标;清理掉"只声明未接线"的死指标(工具/策略/终态/活跃 run 度量的埋点留待后续在 loop 层做);顺手修 `_find_spa_index` 里一行既有 `open(..., "r")` 冗余模式参数
  - 新增 `tests/test_metrics.py`(3 项)。全量测试 **193 passed, 1 skipped**

- **planner/intent 接模型 + 工具自解释(loop 深度③⑤再上一档)**:
  - **loop 深度③ 阶段规划接模型**(`loop/planner.py`):任务复杂度由离线启发式判定为"复杂"后,
    阶段内容不再用固定模板,而是由**模型生成**(`plan_with_model` / `ModelPlanner`,走结构化输出要
    `{"steps":[{"title","goal"},...]}`);模型不可用 / 返回垃圾时自动降级回通用模板(离线可测)。
    复杂度判定仍用启发式(省一次调用、可三等),只有"复杂"才值得让模型细化阶段。
  - **loop 深度⑤ 意图判断接模型**(`loop/intent.py`):给 `ToolIntentRouter` 增加 `reasoner`——
    无触发信号时让**模型自己说明理由**,说合理就放行,否则维持提醒;不给 reasoner 则退回启发式,
    默认行为与离线测试行为不变。
  - **工具自解释增强(能力层"长"进系统)**:给 `ToolSpec` 增加 `triggers` 触发词元数据,
    新增 `tool/trigger.py` **从工具描述自动提取触发词**(英文词 + 中文相邻双字 + 过滤泛词),
    intent 路由与 skill 触发路由都从工具自身"长出"触发信号、不再手配映射表。
    `skill.trigger.pick` 暴露的工具也自带 `triggers` 元数据,让意图路由能识别"何时该触发技能路由"。
  - **skill 触发复用共享提取**(`skill/trigger.py`):匹配信号与意图路由同源(`tool.trigger.tokens`),
    英文词 + 中文双字 + 滤泛词,行为与原先等价。
  - 新增 `tests/test_tool_trigger.py`、`planner`/`intent` 增强测试;全量测试 **256 passed, 1 skipped**

- **能力层再上一档：技能版本化 + 多 Agent 共享记忆/容错降级/并行调度**:
  - **技能版本化**(`skill/skill.py`):`SkillCatalog` 由"别名→单绑定"升级为"别名→{版本→绑定}";
    `find(alias, version=None)` 不传版本默认取**最新版**(数字版本按数值比较,如 1.10 > 1.9);
    `load_skill` 同别名可登记多版本、不再静默覆盖;信任快照 `digest` 加入 version 使不同版本可区分;
    `load_skills_from_dir` 支持 `<alias>/<version>/SKILL.md` 版本目录约定。
  - **多 Agent 共享工作记忆**(`multiagent/supervisor.py`):新增 `MemoryScope.WORKSPACE`
    (一次协作工作区内共享);`share_memory(service, *agents, scope=WORKSPACE)` 把同一份
    MemoryService + WORKSPACE 注入各子 Agent,并挂 `memory.remember/recall` 工具,
    研究员写入→写手能读到(共享上下文、避免重复查)。
  - **多 Agent 容错降级**(`multiagent/supervisor.py`):`wrap_agent_as_tool` 增 `retries` /
    `fallback` / `degrade`:子 Agent 失败 → 重试 → 换备用专员 → 降级交接单,不再向主管层崩溃。
  - **多 Agent 并行/串行分派**(新增 `multiagent/dispatch.py`):确定性 `Dispatcher`,线程池真并行
    `run_parallel`(离线可测真并行耗时)或按依赖串行 `run_sequential`,不靠模型脑补。
  - 新增 `tests`:技能版本化(5)、WORKSPACE 作用域(3)、多 Agent 容错/共享记忆/分派(6);
    全量测试 **270 passed, 1 skipped**

- **工具调用全链路稳定性层**(新增 `tool/stability.py` + 接入 loop):
  - **超时护栏**:给工具调用设硬时限,卡死的工具不再同步卡死整个 loop(工作线程 + deadline,
    `future.result(timeout)`,到点返回超时信号并交还控制权)。
  - **指数退避重试**:瞬时故障(超时/ConnectionError/OSError)按 `backoff_base*2^(n-1)` 指数退避
    自动重试(上限 `backoff_max`),扛限流/抖动;pure(无副作用)工具额外可重试。
  - **统一降级兜底**:重试耗尽配了 `fallback` → 返回带 `[降级]` 标记的兜底结果;否则返回错误串。
  - **熔断保护(circuit breaker)**:连续失败达 `circuit_threshold` 次 → 短路期内直接返回 `[熔断]`
    信号、不调不重试(不再空转打上游);冷却 `circuit_cooldown` 后**半开试探**一次,成功即关闭、
    失败再打开;按工具名分桶隔离(一个工具挂了不连累别的)。默认 `threshold=0` 关闭。
  - **对齐项目约定**:结果带标志(非异常抛出,同 execution/broker)、复用 `ToolSpec.pure` 可重试信号、
    `StabilityConfig()` 默认全关(向后兼容,不配不改行为);`AgentLoop(..., stability=...)` 可选接入,
    loop 深度① 下游零改动。
  - 新增 `tests/test_tool_stability.py`(18 项:退避递增/重试成功/非瞬时不重试/pure 语义/超时不卡死/
    降级/默认关闭/loop 集成/熔断触发/熔断跳重试/半开恢复/半开失败再熔断/隔离)。全量测试
    **288 passed, 1 skipped**

- **去重复/收敛堆砌(审查驱动的一轮重构)**:
  - **统一 `PolicyDenied`** 到 `policy/policy.py`(loop/session 共用一份,消除跨模块 catch 不到隐患)。
  - **分词器收敛 4→1**:`loop._tokens`/`intent._words` 委托到 `tool.trigger.tokens()`(英文词+中文双字+滤泛词),
    intent 顺带补上中文识别(原版只匹配英文)。
  - **删冗余 `demo_full.py`**(是 demo_e2e 的严格子集、无测试);README/test_architecture 同步移除。
  - **两套主循环共享"工具执行"**:抽模块级 `exec_tool`(稳定性:超时/退避/降级/熔断 + 错误转字符串的单一来源),
    `AgentLoop._safe_execute` 与 `AgentSession._execute/stream` 都走它——**HTTP/流式/CLI 产品路径首次吃到深层工具稳定性**,
    工具失败改为喂回模型自纠(不再直接崩),保留 tool_call_id 配对/审批/持久化/流式。
  - **诚实标注"独立能力/未接入主链"**:凭证加密、Checkpoint·恢复、受控执行·沙箱是已写好但未接产品主链的能力,
    不再冒充"已实现",也不删除(留给以后接线),README 补说明。
  - 全量测试 **287 passed, 2 skipped**

### 未实现(路线图,见 README)

- T9 分层/契约测试(接口契约一致性、分层边界)
