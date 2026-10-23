"""手工变异核对：把某个变异**真的施加到源码上**，只跑对应测试文件，看测试是否变红。

背景：变异测试（`scripts/mutation.sh`）把源码改坏再跑测试——测试变红叫「杀死」，
仍然全绿叫「存活」。存活的突变体有两类，必须分开：

  · **等价变异**（本文件里标 EQUIV）：改了与没改行为完全一样，**杀不掉是正常的**，
    不是测试缺口。写在这里是为了后人不要再追一次（也不用为此补测试——补不出来）。
  · **真缺口**（标 KILL）：本该被测试抓住。这里把它们逐个施加回源码，
    期望测试变红；**仍然绿就是缺口**，得补测或改实现让它可测。

所以本文件是一张「存活突变体裁决表」：跑一遍，退出码非 0 就说明有缺口没堵。

跑法：`./.venv/Scripts/python.exe scripts/mutant_check.py`
解释器不用你记得选：当前不是仓内 `.venv` 时它会自己换过去重跑一遍。

**成本（2026-10-15 实测；旧说法「约 1~2 分钟」与「全套 40 分钟上下」都已过期）**：
全套 75 条（63 KILL + 12 EQUIV）
**473s（7.9 分钟）**，而且**几乎全花在 `tests/test_delivery_ledger.py` 那一片**（52 条那一版：
台账片 11 条 278s、整机 616s，其余 40 多条合计只要 7 分钟）。

数字怎么来的：墙钟与每条耗时用**它自己的尺子**——`--dry` 与每条的 `[i/N] … (耗时s)` 行；
「每条用例拉起多少次 git」用 `python scripts/count_git_spawn.py tests/test_delivery_ledger.py` 量
（新增成本断言时要同段指名尺子，`tests/test_cost_claims.py` 钉住这条）。所以给了两个闸：

  · `--dry`：只校验锚点（那条 old 在目标文件里是否恰好命中 1 次），不施加变异 → **1 秒**出结果。
    改完 CASES 先跑它，别花 40 分钟才发现某条锚点已经过期。
  · `--only 子串`：只跑「标签或测试文件」里含该子串的用例（快速回归，如 `--only 例外清单`）。

**每条用例跑完打一行 `[i/N] 结论 基线… 标签 (耗时s)`**，并把 stdout 设成行缓冲——
不然非 TTY 下是块缓冲：全程日志 0 字节（2026-10-15 真踩过，等了 26 分钟看不到任何进度，
只能翻 `.mutant_check_backup/` 猜它在哪一条）。

**基线取证**（2026-10-15 加，也是被真事教出来的）：只看 `pytest` 的退出码是不够的——
任何非 0 都会被记成 KILLED，于是「基线根本跑不起来」（比如用系统 python，那里的
cryptography 残缺 ⇒ 59 个 collection ERROR）会被伪装成「牙齿很硬」。
所以每片测试文件在动手前先跑一次**未被变异**的基线，结论里分开写：
`KILLED`（基线绿，强证据）／`KILLED?`（基线本就红，弱证据）／`BROKEN`（基线跑不起来，
这一片的「杀死」不算证据，跟缺口同等待遇：退出码非 0）。

**崩溃安全**（这条是被真事教出来的）：本脚本会**真的改写仓库源码**，所以：

  · 每次改写前先把原文逐字节备份到 `.mutant_check_backup/`，并把「备份名 + 改写后内容的
    sha256」写进 `manifest.json`，**先落盘再改源码**；
  · 正常结束、`Ctrl-C`、`SIGTERM` 都会还原（信号处理器 + `finally` 两道）；
  · 被**强杀**（TerminateProcess/SIGKILL，进程来不及做任何事）会留下残局：
    下次启动时 `_settle_previous_run()` 会检查——只有当前内容**逐字节等于我们写坏的那份**
    （sha 对得上）才自动还原文；对不上（比如你手改过）就**拒绝动手**并让你人工处理，
    绝不拿旧备份覆盖你的新改动。

**跑变异期间别读仓库**（也是被真事教出来的）：本脚本会**周期性把源码留在改坏状态**，
所以那段时间里任何「读代码 / 审代码 / 数行数 / 搜关键字」拿到的都是**假结果**
——我自己在另一轮变异跑着的时候去读一处实现，读到的就是被改坏的那份。
要并行干活就干**不碰仓库的**（读日志、读笔记、写文档）；真要读代码，先等这一轮
`--restore` 跑完。
真要读代码，先等这一轮 `--restore` 跑完。

写这个的原因：一次运行被外层 60 秒超时**强杀**，恰好在「`200_000` → `200001`」这条上，
源码被留在改坏状态；而 `git status` 因为 stat 缓存看着是干净的，很容易就这么提交进去。
所以工具必须自己兜住这件事（并且 `--restore` 可以手动还原）。
数据来源：
  · 2026-09-29 容器内 mutmut 全量运行（killed=242 / survived=9 / total=251 → 96.4%）。
    表中 9 条 EQUIV 就是那 9 条存活；2 条 KILL 是同一轮里**已经被补的测试杀死**的
    （改之前它们也存活，所以这张表同时是"补了什么"的证据）。
  · 2026-09-30 ratelimit 分片（killed=93 / survived=2 / total=95 → 97.9%）：
    该片原先 21 个突变体**全部存活**（代码跑到了，但断言几乎不查返回值），补完断言后
    只剩下面那 2 条，且都是等价变异——这就是"补无可补"该有的样子。
  · 2026-09-30 concurrency 分片二轮（193/206=93.7% → **206/206=100.0%**）：
    首跑 47 条存活，补测杀掉 34 条，剩下 13 条**全部**在下面，期望全是 KILL（等价变异 0 条）。
    **这里有两个修正要先说**：`token_hex(4)→(5)`、stop 不 join、丢锁告警加 XX 前后缀
    这 3 条原先被本文件记为等价变异，理由是"没有可观测差异"——现在看是**理由错了**：
    差异一直存在（后缀长度 / stop 是否等心跳让开 / 告警文案的首尾），只是断言没查。
    所以裁决表里的 EQUIV 也是会过期的：断言一收紧，得回来重跑一遍。
    （顺着这条教训：锁定 `runtime/locking.py` 的用例必须把该分片的**两个**测试文件都跑上，
    否则在另一个文件里刚补的断言会被本脚本漏掉，得出“还是等价”的假结论。）
  · 2026-10-01 patch-guard 分片（四轮账见 docs/mutation.md 第四节）：
    93.1%（存活 19 + 超时 5）→ 逐条裁决 → 96.2% → 98.3% → **第五轮 341/346 = 98.6%
    （存活 5、超时 0）**。这一片现在已经没有“可杀但没被杀”的突变体：
    存活的 5 条全是等价变异（下面列出）；那 5 条“超时”是把 `idx += 1` 改成死循环的突变体，
    分片表给 pytest 加了 `--timeout=30` 之后它们在 pytest 内部就变红（不再被外层砍成 timeout）。
    下面那 6 条 KILL 是第四轮按裁决补的实测缺口。这一片带出两条**测试写法**的教训：
      1) 反射式断言（`inspect.signature` 取默认值）**永远杀不死变异**：变异运行只跑
         “覆盖到该函数体”的测试（`mutmut/__main__.py` 用 `tests_by_mangled_function_name` 逐条筛），
         而反射不执行函数体 → 它不会被映射到任何突变体上；
      2) `encoding=None` / `newline=None` 在 Linux+UTF-8 环境里与显式值**行为逐字节相同**
         （UTF-8 就是本机编码，`os.linesep` 也是 `\n`），行为断言在那里天然没有判别力——
         必须直接钉 kwargs 这个契约（`tests/test_patch_parser.py` 里那条 kwargs 用例）。
"""

import contextlib
import hashlib
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import tempfile
import time

KILL = "kill"
EQUIV = "equiv"
# 输出全 ASCII，避免 Windows 控制台 cp936 把中文打成乱码

REDACTION = "src/warden_agent/core/redaction.py"
POLICY = "src/warden_agent/policy/policy.py"
T_REDACTION = "tests/test_redaction.py"
T_POLICY = "tests/test_policy.py"
RATELIMIT = "src/warden_agent/web/ratelimit.py"
T_RATELIMIT = "tests/test_ratelimit.py"
LOCKING = "src/warden_agent/runtime/locking.py"
T_LEASE = "tests/test_run_lease.py"
# 锁定 locking.py 的分片**有两个**测试文件：裁决时必须两个都跑，
# 否则在 test_run_lock.py 里补的断言会被漏掉（多个字段用空白分隔，下面会 split）。
T_LOCK = "tests/test_run_lock.py tests/test_run_lease.py"
PATCH = "src/warden_agent/git/patch.py"
# 交付台账守卫：这一片的「源码」就是守卫本身（判据 7：覆盖连续性，2026-09 补的洞）
LEDGER_GUARD = "tests/test_delivery_ledger.py"
# 沙箱隔离档：源码一份，但**两个**测试文件都得跑（判据分在参数层与 broker 两侧）
SANDBOX = "src/warden_agent/execution/sandbox.py"
T_SANDBOX = "tests/test_sandbox_isolation.py tests/test_sandbox_isolation_gate.py"
# 同样，patch-guard 分片选了**三个**测试文件（mutmut 逐条突变体跑的就是这三个）。
T_PATCH = "tests/test_git.py tests/test_patch_path_guard.py tests/test_patch_parser.py"
# 「事件会不会真的发生」那一层的守卫（triggers: 段：schedule 只从默认分支求值）
JOB_GATES_GUARD = "tests/test_ci_job_gates.py"
# 第93 批：单机存储 × 多副本开关（启动即拒）
RUN_SERVER = "src/warden_agent/web/run_server.py"
T_SHARED_GUARD = "tests/test_shared_state_guard.py"
# 第91/92 批的两族静默失真（第九十四批补登记）
CHAT_ROUTES = "src/warden_agent/web/routes/chat.py"
T_CHAT_FAIL = "tests/test_chat_failure_status.py"
SQLITE_STORE = "src/warden_agent/store/sqlite.py"
T_SCHEMA = "tests/test_schema_downgrade.py"
APPROVAL_ROUTES = "src/warden_agent/web/routes/approvals.py"
T_APPROVAL = "tests/test_approval_event_shape.py"

# 被反复引用的长字符串拎出来，免得每行都超 100 列（ruff E501）
_PATH_DEFAULT = 'raw = str(arguments.get("path", ""))'
# 交付面那条推前形状守卫（第八十七批新增两条变异指向它：脚本 + 它的用例）
PUSH_SHAPE_SRC = "scripts/check_delivery_push.py"
PUSH_SHAPE = "tests/test_delivery_push.py"
_OS_NAME = 'if os.name == "nt":'
_RANK = "{Decision.ALLOW: 0, Decision.ASK: 1, Decision.DENY: 2}"
_READ_OLD = 'abs_path.read_text(encoding="utf-8") if abs_path.exists() else ""'
_WRITE_NEW = 'abs_path.write_text("\\n".join(new_lines), encoding="utf-8", newline="\\n")'

# (期望, 理由, 文件, 原文, 变异, 测试文件)
CASES: list[tuple[str, str, str, str, str, str]] = [
    # ---- scripts/check_delivery_push.py：「起点不在树里」的第四种结局（第八十七批）----
    (KILL, "起点核不动时整条收摊（第八十六批 CI 上真实发生过的行为：rc=2）",
     PUSH_SHAPE_SRC,
     '            base_sha = fallback',
     '            raise GitFailed(f"{tip[:7]} 起点核不动") from exc', PUSH_SHAPE),
    (KILL, "退回说了但不说没核到什么（静默：读者会以为形状已经核完了）",
     PUSH_SHAPE_SRC,
     '            f"  没核到的是：这次 push 到底推了几个提交——改写历史后树里没有可比的起点。"',
     '            f"  已退回核第一父提交。"', PUSH_SHAPE),
    # ---- core/redaction.py：4 条存活，全部等价 ----
    (EQUIV, "sha256 的 utf-8 换成 UTF-8（codec 别名，编码结果逐字节相同）",
     REDACTION,
     'hashlib.sha256(value.encode("utf-8", "replace"))',
     'hashlib.sha256(value.encode("UTF-8", "replace"))', T_REDACTION),
    (EQUIV, "bytes 解码的 utf-8 换成 UTF-8（同上）",
     REDACTION,
     'value.decode("utf-8", "replace")',
     'value.decode("UTF-8", "replace")', T_REDACTION),
    (EQUIV, "redact_text 的 group == 0 换成 == 1（_sub 未传 _g，两分支产出相同）",
     REDACTION, "if group == 0:", "if group == 1:", T_REDACTION),
    (EQUIV, "str 键判定恒假（str(k) == k，两分支结果一样）",
     REDACTION, "key = k if isinstance(k, str) else str(k)",
     "key = k if (isinstance(k, str)) and False else str(k)", T_REDACTION),
    # ---- policy/policy.py：7 条存活，5 条等价、2 条真缺口（平台语义没被测到） ----
    (EQUIV, "path 默认值换成 None（str(None) 不在任何受保护前缀下，仍然不拦）",
     POLICY, _PATH_DEFAULT, 'raw = str(arguments.get("path", None))', T_POLICY),
    (EQUIV, "path 默认值直接省掉（同上，默认 None）",
     POLICY, _PATH_DEFAULT, 'raw = str(arguments.get("path", ))', T_POLICY),
    (EQUIV, "path 默认值换成 XXXX（同样不落在受保护前缀下）",
     POLICY, _PATH_DEFAULT, 'raw = str(arguments.get("path", "XXXX"))', T_POLICY),
    (EQUIV, "pure 置成 None（None 也是假值，仍走 ASK，fail-closed 不变）",
     POLICY, "pure = False", "pure = None", T_POLICY),
    (EQUIV, "DENY 的排序权重 2 换成 3（仍是最严，取最大值的结果不变）",
     POLICY, _RANK, "{Decision.ALLOW: 0, Decision.ASK: 1, Decision.DENY: 3}", T_POLICY),
    (KILL, "os.name 换成永远不等的字符串（Windows 大小写不敏感失效）",
     POLICY, _OS_NAME, 'if os.name == "XXntXX":', T_POLICY),
    (KILL, "os.name 的 nt 写成大写 NT（同上，Windows 语义失效）",
     POLICY, _OS_NAME, 'if os.name == "NT":', T_POLICY),
    # ---- web/ratelimit.py：2 条存活，均为等价 ----
    (EQUIV, "报错文案加 XX 前后缀（断言的是“文案含关键词”，加前后缀不改变契约）",
     RATELIMIT, 'raise ValueError("限流参数必须为正整数")',
     'raise ValueError("XX限流参数必须为正整数XX")', T_RATELIMIT),
    (EQUIV, "partition 换成 rpartition（≥2 个 / 时两侧必有一侧含 /，int() 都会失败）",
     RATELIMIT, 'text.partition("/")', 'text.rpartition("/")', T_RATELIMIT),
    # ---- runtime/locking.py：13 条存活，**全部为真缺口**（现已全部杀死） ----
    # 该片 2026-09-30 首跑 159/206 = 77.2%（47 条存活），二轮 206/206 = 100.0%。
    (KILL, "随机后缀 4 字节换成 5 字节（后缀长度变了——owner 格式是排查契约）",
     LOCKING, "secrets.token_hex(4)", "secrets.token_hex(5)", T_LOCK),
    (KILL, "随机后缀 4 换成 None（token_hex(None) 直接抛错，取锁路径炸）",
     LOCKING, "secrets.token_hex(4)", "secrets.token_hex(None)", T_LOCK),
    (KILL, "过期判定 > 换成 >=（恰好到期的那一刻别人就抢不到，多占一个 TTL）",
     LOCKING,
     "            if held is not None and held.expires_at > now and held.owner != owner:",
     "            if held is not None and held.expires_at >= now and held.owner != owner:", T_LOCK),
    (KILL, "续租判定 <= 换成 <（恰好到期的那一刻还以为能续）",
     LOCKING,
     "            if held is None or held.owner != owner or held.expires_at <= now:",
     "            if held is None or held.owner != owner or held.expires_at < now:", T_LOCK),
    (KILL, "stop 里不看心跳线程（不 join）——可观测差异=stop 是否等心跳让开；\n"
           "            这条曾经被记为等价，是**理由错了**：join 上限只是上界，下界必须自己断言",
     LOCKING, "thread = self._thread", "thread = None", T_LOCK),
    (KILL, "丢锁告警加 XX 前后缀（日志/告警规则按子串匹配→以前抓不到，现在按首尾断言）\n"
           "            同上：过去把它当等价变异是**断言太松**，不是真没差异",
     LOCKING,
     '            "Run 租约续租失败：run=%s owner=%s —— 租约可能已被接管，"',
     '            "XXRun 租约续租失败：run=%s owner=%s —— 租约可能已被接管，XX"',
     T_LOCK),
    (KILL, "丢锁告警里 Run 变小写（同一条告警的**逐字**首尾也是运维契约）",
     LOCKING,
     '                "Run 租约续租失败：run=%s owner=%s —— 租约可能已被接管，"',
     '                "run 租约续租失败：run=%s owner=%s —— 租约可能已被接管，"',
     T_LOCK),
    (KILL, "回落锁不把 store 交下去（SqlRunLock(None)：多副本下等于锁在不存在的表上）",
     LOCKING,
     "        return SqlRunLock(store)  # type: ignore[arg-type]  # 结构化满足协议",
     "        return SqlRunLock(None)  # type: ignore[arg-type]  # 结构化满足协议", T_LOCK),
    (KILL, "回落告警不带存储类型（看到告警也不知道该换哪个存储）",
     LOCKING, "            type(store).__name__,", "            None,", T_LOCK),
    (KILL, "回落告警直接省掉类型参数（%s 没东西可填，格式化直接报错）",
     LOCKING, "            type(store).__name__,\n        )", "        )", T_LOCK),
    (KILL, "回落告警打在 root logger 上（按模块名过滤告警就失效了）",
     LOCKING, "        logging.getLogger(__name__).warning(",
     "        logging.getLogger(None).warning(", T_LOCK),
    (KILL, "回落告警加 XX 前后缀（同丢锁告警：首尾逐字是契约）",
     LOCKING,
     '            "请求共享 Run 锁，但存储 %s 不支持取锁方法——回落为进程内实现："',
     '            "XX请求共享 Run 锁，但存储 %s 不支持取锁方法——回落为进程内实现：XX"',
     T_LOCK),
    (KILL, "回落告警的类型名取成 NoneType（拿到的是 None 类型，仍然是在说瞎话）",
     LOCKING, "            type(store).__name__,", "            type(None).__name__,", T_LOCK),
    # === 触发器台账的「never 必须有期限」（第七十六批）===
    (KILL, "承认「还没轮到」的日子被改成过去（never 就不再是有期限的断言）",
     "tests/CI_JOB_GATES.yaml",
     "    first_possible_fire: 2026-10-19", "    first_possible_fire: 2026-10-12", JOB_GATES_GUARD),
    (KILL, "永远不响这条规则整个摘掉（台账可以无限期地宣称「它就是不响」）",
     JOB_GATES_GUARD,
     '        if str(entry.get("last_observed_fire")) == "never" and first < today:',
     "        if False:", JOB_GATES_GUARD),
    (KILL, "只认 kind=schedule 的过滤反过来（真台账那条 schedule 反而被跳过）",
     JOB_GATES_GUARD,
     '    for entry in entries:\n        if entry.get("kind") != "schedule":',
     '    for entry in entries:\n        if entry.get("kind") == "schedule":', JOB_GATES_GUARD),
    (KILL, "复核日期在将来不再报（可以写一个没发生过的复核）",
     JOB_GATES_GUARD,
     "        if checked > today:", "        if False:", JOB_GATES_GUARD),
    (KILL, "日期边界从 > 改成 >=（今天复核过的条目会立刻红）",
     JOB_GATES_GUARD,
     "        if checked > today:", "        if checked >= today:", JOB_GATES_GUARD),
]

# ---- 成本断言的尺子与判据（第八十六批补：新判据当时没有变异登记，等于没人证明它有牙）----
# 两条判据只核「有没有指名尺子」与「N 条用例是否等于实测」；而尺子本身会不会量、判据会不会空转，
# 各自也得有变异体撞一次——`变异测试` job 在 push 上本来就 skipped，没人会替你抓。
COST_GUARD = "tests/test_cost_claims.py"
COST_RULER = "scripts/count_git_spawn.py"
CASES += [
    (KILL, "「成本数字必须同段指名尺子」被短路 ⇒ 数字又能不指名尺子（成本断言不可重放）",
     COST_GUARD,
     "and _RULER not in para:", "and False:", COST_GUARD),
    (KILL, "树锚豁免被拆 ⇒ 带日期的历史数字被当成当下事实（判据开始说假红）",
     COST_GUARD,
     '            if "本文件" in para and not _TREE_ANCHOR.search(para):',
     '            if "本文件" in para:', COST_GUARD),
    (KILL, "条数核对被短路 ⇒ 「本文件 N 条用例」写错也没人管（本批第一步抓到的就是这个）",
     COST_GUARD,
     "                    if actual is not None and int(claim) != actual:",
     "                    if False:", COST_GUARD),
    (KILL, "命中统计恒 0 ⇒ 「判据不是空转」那条自己变成空转",
     COST_GUARD,
     "            if _SPAWN_WORD.search(para) and _SPAWN_CLAIM.search(para):\n"
     "                spawn_hits += 1",
     "            if False:\n"
     "                spawn_hits += 1", COST_GUARD),
    (KILL, "尺子失聪：计数点被拆掉 ⇒ 「N 次」永远回到没人数过的状态（尺子自己会量的那两条钉住）",
     COST_RULER,
     '        if "git" in flat:', "        if False:", COST_GUARD),
    (EQUIV, "成本数字本身改错 ⇒ 判据不管量级（它只管引用与条数，量级由尺子重放来核）；"
            "登记这一条是为了把边界写清楚，免得下一个人以为「改了数字会被判据抓到」",
     "tests/test_delivery_ledger.py",
     "整文件 **416 次**（每条 76.5 → 9.5 次）",
     "整文件 **616 次**（每条 76.5 → 9.5 次）", COST_GUARD),
]

# ---- git/patch.py（patch-guard 分片，2026-10-01 第三、四轮）----
# 官方重跑：2026-09-30 是 322/346 = 93.1%（19 存活 + 5 超时），
#         2026-10-01 把超时搬进 pytest + 按裁决补测后 333/346=96.2% → 340/346=98.3%
#         → **第五轮 341/346 = 98.6%（存活 5，超时 0）**。
# 剩下的 5 条全是等价变异（文件末尾列出），可杀的突变体 0 条；
# 下面这 6 条 KILL 是第四轮按裁决补的实测缺口（也就这一片需要长期重跑的几条）。
CASES += [
    # 行预算默认值：质量阀错一格
    (KILL, "行数预算默认值 200_000 → 200001（多一行被静默放行；反射断言杀不掉它）",
     PATCH, "max_lines: int = 200_000,", "max_lines: int = 200001,", T_PATCH),
    # 读写不显式 utf-8：在 Linux+UTF-8 上行为一样，但在 cp936/任意非 UTF-8 机器上直接崩。
    # 所以下面五条都靠 test_apply读写文件必须显式指定utf8和LF 里的 **kwargs 契约** 杀死。
    (KILL, "读旧内容 encoding=None（跟随本机 locale：非 UTF-8 机器上解非 ASCII 直接崩）",
     PATCH, _READ_OLD, 'abs_path.read_text(encoding=None) if abs_path.exists() else ""',
     T_PATCH),
    (KILL, "写回 encoding=None（同上，落盘编码跟着本机跑）",
     PATCH, _WRITE_NEW,
     'abs_path.write_text("\\n".join(new_lines), encoding=None, newline="\\n")',
     T_PATCH),
    (KILL, "写回 newline=None（Windows 上落盘变 CRLF：同一份补丁在不同机器改出不同字节）",
     PATCH, _WRITE_NEW,
     'abs_path.write_text("\\n".join(new_lines), encoding="utf-8", newline=None)',
     T_PATCH),
    (KILL, "写回省掉 encoding（跟随本机 locale）",
     PATCH, _WRITE_NEW, 'abs_path.write_text("\\n".join(new_lines), newline="\\n")',
     T_PATCH),
    (KILL, "写回省掉 newline（跟随平台换行符）",
     PATCH, _WRITE_NEW, 'abs_path.write_text("\\n".join(new_lines), encoding="utf-8", )',
     T_PATCH),
]

# ---- 沙箱隔离档的注入断层（2026-10 修的：注入只作用一半路径）----
# 缺陷形状：`probe`/`platform` 在 `detect_isolation_tier`/`isolation_tier_from_env` 上能注入，
# 但 `resolve_isolation_tier` 与 `SandboxedExecutionBroker.__init__` 把参数吞掉了 ⇒ 「注入说
# 给不了内核档」在真有内核档的机器上不报错、「注入说能给」在没内核档的机器上无效。
# 下面三条各断一层，都要求测试变红（断言写成两态对比，所以两类机器上都带电）。
CASES += [
    (KILL, "参数层直连真实探测（resolve_isolation_tier 吞掉注入）",
     SANDBOX,
     # 锚点必须先带一行文档串：`    detected = detect_isolation_tier(platform=platform,
     # probe=probe)` 在 `resolve_isolation_tier` 与 `isolation_tier_from_env` 里**各有一份**，
     # 只给这一行会撞上「原文不唯一」而 SKIP（变异核对会以 exit=1 报出来——这是故意的）。
     '    """\n    detected = detect_isolation_tier(platform=platform, probe=probe)',
     '    """\n    detected = detect_isolation_tier()', T_SANDBOX),
    (KILL, "合并层只给部署级声明注入（参数层那半边被吞）",
     SANDBOX,
     "    spec_tier = resolve_isolation_tier(spec_isolation, probe=probe, platform=platform)",
     "    spec_tier = resolve_isolation_tier(spec_isolation)", T_SANDBOX),
    (KILL, "broker 吞掉注入（静默退回真实探测：测试看到的是本机能力）",
     SANDBOX,
     "        self.tier = resolve_enforced_isolation(\n"
     "            self.spec.isolation, probe=probe, platform=platform,\n"
     "        )",
     "        self.tier = resolve_enforced_isolation(self.spec.isolation)", T_SANDBOX),
]

# ---- 交付回执 ↔ 台账 的对账（第八十二批补：keys/log/tree 三项此前无人核）----
# 缺陷形状：`tests/test_delivery_receipts.py` 的交叉核只比 sha/tree/shape，而 `shape` 在干净树上
# 恒等于空指纹（CHANGELOG.md:226-228 自己写了「真正的树指纹是 keys」）⇒ 行抄错 keys 也照绿。
RECEIPT_GUARD = "tests/test_delivery_receipts.py"
CASES += [
    (KILL, "台账行与回执的 keys/tree 不对账（真正的树指纹没人核）",
     RECEIPT_GUARD,
     "            if row.get(k) and before != row.get(k):",
     "            if False:", RECEIPT_GUARD),
    (KILL, "仓外日志名不对账（行上写另一个 log 也放行）",
     RECEIPT_GUARD,
     '        if row.get("receipt") and fz.get("log") != row.get("receipt"):',
     "        if False:", RECEIPT_GUARD),
    (KILL, "回执路径不设限（绝对路径与 .. 能把手伸到仓外）",
     RECEIPT_GUARD,
     '        if rel.startswith("/") or ".." in rel.split("/") or not rel.startswith("receipts/"):',
     "        if False:", RECEIPT_GUARD),
    (KILL, "git-only 允许自报数字（回退到「没有回执也能写 shape/keys/receipt」）",
     RECEIPT_GUARD,
     '            if row.get("frozen_proof") != "none" or claimed:',
     "            if False:", RECEIPT_GUARD),
]

# ---- 推前形状守卫（第八十三批：一次推 3 个提交让台账不可取证，发现时已经不可逆）----
# 缺陷形状：`scripts/check_delivery_push.py` 三条判据任意一条被弱化 ⇒ 推送前不再拦。
# 现场：GitHub 只为 push 的 tip 建 run（`gh api …?head_sha=<sha>` 实测 0/0/1），
# 而台账要求链上每个提交都有行 + 每行都有 `evidence/ci/<sha12>.json` ⇒ 中间提交永远拿不到。
# 判据分两层（纯函数构造数据 + 临时 git 仓走真路径），这 3 条都必须被杀。
PUSH_CHECK = "scripts/check_delivery_push.py"
PUSH_GUARD = "tests/test_delivery_push.py"
CASES += [
    (KILL, "多提交判据整个没接进去（一次推 N 个照样放行——本批的现场就是这样漏的）",
     PUSH_CHECK,
     "    if len(commits) > 1:",
     "    if False:", PUSH_GUARD),
    (KILL, "中间提交与 tip 不分（中间那条「永远拿不到 run」不再单独说）",
     PUSH_CHECK,
     "        if index == len(commits) - 1:",
     "        if True:", PUSH_GUARD),
    (KILL, "缺行判定反过来（携带了也接着往下报 ⇒ 正对照立刻变红）",
     PUSH_CHECK,
     "        if facts.first_parent in rows_at.get(facts.sha, set()):",
     "        if False:", PUSH_GUARD),
]

# ---- 交付台账守卫（覆盖连续性，2026-09 补洞那一批）----
# 背景：只核 `HEAD^` 那一行时，`dev` 上连着攒两个本地交付提交就会在台账里留洞，
# 且**从第二个洞开始没有任何判据看得见**（实测：`7cf9b9f`+`904071e` 两条都缺行，守卫只报前者）。
CASES += [
    (KILL, "覆盖连续性判据整个没接进 _violations（链中间的洞重新变得没有任何判据）",
     LEDGER_GUARD,
     "        out.extend(\n"
     "            _coverage_violations(parents=parents, seen=seen, coverage_from=coverage_from)\n"
     "        )",
     "        pass", LEDGER_GUARD),
    (KILL, "缺 coverage_from 声明时 fail-open（「没写」被读成「都放行」）",
     LEDGER_GUARD, "    if not coverage_from:", '    if "":', LEDGER_GUARD),
    (KILL, "只核 HEAD^ 那一跳（判据 7 就是为了修这个洞而补的）",
     LEDGER_GUARD,
     "    holes = [sha for sha in chain[: at + 1] if sha not in seen]",
     "    holes = [sha for sha in chain[:1] if sha not in seen]", LEDGER_GUARD),
    (KILL, "范围外的行不报（抬高 coverage_from 就能把范围偷偷往上缩）",
     LEDGER_GUARD,
     "    beyond = sorted(sha[:7] for sha in seen if sha in set(chain[at + 1 :]))",
     "    beyond: list[str] = []", LEDGER_GUARD),
]

# 第八十四批：给「结论分档」本身登记变异（装置的装置）。基线取证那段逻辑要是被改回去，
# 报告就会重新把「基线跑不起来」说成「牙齿很硬」——假证据，必须能被自己抓住。
# 注意：这两条变异的是**本文件自己**；真正跑的是 tests/test_mutant_check_verdicts.py，
# 它是另起一个进程 import 源码的，所以当前进程不受影响（模块早 import 完了），且改完立即还原。
CASES += [
    # 注意 2：这两条的 old 必须**带上下文**（多行）。因为 CASES 列表里就写着这两个字面量本身，
    # 单行写法会在整个文件里匹配到 2 处 ⇒ 装置只能判 SKIP（本批真踩过：v9 首跑这 2 条全 SKIP）。
    (KILL, "基线跑不起来时不再算 BROKEN（假证据：collection ERROR 被读成强杀死）",
     "scripts/mutant_check.py",
     '    if baseline_broken:\n        return "BROKEN"\n',
     '    if baseline_broken:\n        return "KILLED"\n',
     "tests/test_mutant_check_verdicts.py"),
    (KILL, "基线本来就红时不再降档（弱证据被抬成强证据）",
     "scripts/mutant_check.py",
     '        return "BROKEN"\n    return "KILLED" if baseline_green else "KILLED?"',
     '        return "BROKEN"\n    return "KILLED"',
     "tests/test_mutant_check_verdicts.py"),
]

CI_EVIDENCE_GUARD = "tests/test_ci_evidence.py"
CASES += [
    # 第八十四批续：例外清单（`UNPROVEN_ALLOWED`）的四条子规则逐条撞一遍。
    # 为什么必须登记：本仓现实台账里恰好**没有反例** ⇒ 那条判据在真数据上是恒绿的，
    # 逻辑被改坏也没人知道；这几条由同文件里的合成输入测试（`test_牙_*`）负责杀掉。
    (KILL, "例外清单的豁免被放宽成「任何行都可以没证据」（切点之后的证据要求空转）",
     CI_EVIDENCE_GUARD,
     '        elif sha in after_cut and not e["has_evidence"] and sha not in allowed:',
     "        elif False:", CI_EVIDENCE_GUARD),
    (KILL, "例外行藏回执不再被点名（拿 git-only 盖住一份回执）",
     CI_EVIDENCE_GUARD,
     '        if e["has_receipt"]:',
     "        if False:", CI_EVIDENCE_GUARD),
    (KILL, "例外清单涨过上限不再报（新交付的提交可以永久躲证据）",
     CI_EVIDENCE_GUARD,
     "    if len(allowed) > max_entries:",
     "    if False:", CI_EVIDENCE_GUARD),
    (KILL, "例外行已经有了证据还留在清单里不再报（例外被滥用没人管）",
     CI_EVIDENCE_GUARD,
     # 锚点必须带上下文：这个字面量在同一个文件里有第二处（红账分支也问 has_evidence），
     # 单行锚点会变成「old×2 ⇒ 不知道改哪一处 ⇒ 静默跳过」。
     '        if e["has_evidence"]:\n'
     '            out.append(\n'
     '                f"{sha[:12]} 既在 UNPROVEN_ALLOWED 里又有 ci_evidence'
     ' ⇒ 例外被滥用（撤掉它）"',
     '        if False:\n'
     '            out.append(\n'
     '                f"{sha[:12]} 既在 UNPROVEN_ALLOWED 里又有 ci_evidence'
     ' ⇒ 例外被滥用（撤掉它）"',
     CI_EVIDENCE_GUARD),
    # 第九十批：红账也必须有期限（没期限的洞就是永久的洞）。
    (KILL, "红账可以不写期限（空洞从「在途」变成「永久」）",
     CI_EVIDENCE_GUARD,
     '            if not rb:',
     '            if False:', CI_EVIDENCE_GUARD),
    (KILL, "红账期限已过期不再报（空洞过期了没人重新表态）",
     CI_EVIDENCE_GUARD,
     '            elif rb < _utc_today().isoformat():',
     '            elif False:', CI_EVIDENCE_GUARD),
]

CASES += [
    # 第九十三批：文档里写着的禁止（SQLite 不得配多副本）从这批判起由代码强制。
    # 两条都必须是 KILL——它们是“这条禁令到底管不管用”的全部证据；
    # 第一条杀不掉 = 守卫根本不触发；第二条杀不掉 = 反向对照没被钉住
    # （会把最常用的单副本部署挡在门外）。
    (KILL, "共享状态开关的判定被短路 => 启动即拒永不触发（文档禁令又回到只写在文档里）",
     RUN_SERVER,
     '    if not env_bool("WARDEN_SHARED_STATE", False, env):',
     '    if True:', T_SHARED_GUARD),
    (KILL, "存储类型判定被短路 => 任何存储都拒（PostgreSQL 也被拦，多副本彻底不可用）",
     RUN_SERVER,
     '    if not isinstance(store, SqliteStore):',
     '    if False:', T_SHARED_GUARD),
]

CASES += [
    # 第九十四批：把第91/92 批新增的判据也撞一遍。
    # 为什么必须登记：这三族的共同点是“改了也不报错”——超时被报成用户输错、
    # 库比代码新时继续跑、审批事件退化成 tool_name 字符串。
    # 只有变异能证明测试真的在守它们（否则测试可能只走了 happy path）。
    (KILL, "超时不再算 504（供应商没回结果又被报成用户输错）",
     CHAT_ROUTES,
     '    if isinstance(exc, TimeoutError):',
     '    if False:', T_CHAT_FAIL),
    (KILL, "没料到的异常退回 400（第四十七批之前的老毛病：错误率面板又能恒为 0）",
     CHAT_ROUTES,
     '    return 500  # 没料到：不许假装成“用户输错了”',
     '    return 400  # 没料到：不许假装成“用户输错了”', T_CHAT_FAIL),
    (KILL, "降级守卫恒假 => 库比代码新时继续跑（版本号被改写、payload 静默丢弃）",
     SQLITE_STORE,
     '        if existing > self._SCHEMA_VERSION:',
     '        if False:', T_SCHEMA),
    (KILL, "审批事件退回 tool_name 字符串（断线续读的卡片全空，而页面不报错）",
     APPROVAL_ROUTES,
     '                approval=outcome.approval.to_dict(),',
     '                approval={"tool_name": outcome.approval.tool_name},', T_APPROVAL),
    # 第九十六批：第三个出口（`GET /approvals` 审批队列）也曾手写审批字段。
    # 只撞行为判据还不够——手写字典与 to_dict
    # “恰好长得一样”时行为判据是绿的，能抓住它的只有源码级那条。
    (KILL, "审批队列退回手写五个字段（与 to_dict 只是碰巧一样；源码级判据必须报）",
     APPROVAL_ROUTES,
     '                result.append({"run_id": run_id, **pending.to_dict()})',
     '                result.append({"run_id": run_id, "approval_id": pending.approval_id})',
     T_APPROVAL),
    (KILL, "审批队列不供 to_dict（队列形状与 `/chat` 不同源，前端静默少一层信息）",
     APPROVAL_ROUTES,
     '{"run_id": run_id, **pending.to_dict()}',
     '{"run_id": run_id}', T_APPROVAL),
]

# patch-guard 剩下的等价变异（**不补测试**，只留记录）：
#   · x__reject_unsafe_path__mutmut_17/18：路径段过滤集丢了 ""/"."（判据只问 `".." in segments`）；
#   · parse__mutmut_58：`cur_new = ""` → `None`（下游两处都是真值判断，None 与 "" 同假）；
#   · parse__mutmut_63/65：`(1 if cur_old else 0)` 恒真 / else 支不可达。
# 原先还有 apply__mutmut_19/50（codec 名 `utf-8` → `UTF-8`），它们**已经不属于等价**了：
# 上面那条 kwargs 契約用例把编码名当字串行断言，于是这两条也会变红（严格一点，无害）。


_BACKUP_DIR = pathlib.Path(".mutant_check_backup")
_MANIFEST = _BACKUP_DIR / "manifest.json"
# 手上正在被改写的文件：rel → (路径, 原文)。信号中断时靠它还原。
_PENDING: dict[str, tuple[pathlib.Path, str]] = {}


def _sha_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_raw(path: pathlib.Path) -> bytes:
    return path.read_bytes()


def _write_raw(path: pathlib.Path, data: bytes) -> None:
    path.write_bytes(data)


def _source_text(raw: bytes) -> str:
    """把文件内容翻译成「用于匹配的 LF 文本」。

    必须归一化：本仓库工作树里有 CRLF 文件，而 CASES 里跨行的原文写作 "...\n..."，
    拿 CRLF 原文去 count() 会一条都对不上（踩过：一条跨行用例莫名变 SKIP）。
    原有的换行风格另外记下来（_is_crlf），写回时再还原，保持逐字节一致。
    """
    return raw.decode("utf-8").replace("\r\n", "\n")


def _is_crlf(raw: bytes) -> bool:
    return b"\r\n" in raw


def _encode_like(data: str, crlf: bool) -> bytes:
    """按原文件的换行风格落盘（新写的行也跟着走）。"""
    return (data.replace("\n", "\r\n") if crlf else data).encode("utf-8")


def _put_back(rel: str) -> None:
    pending = _PENDING.pop(rel, None)
    if pending is not None:
        _write_raw(*pending)


def _load_manifest() -> list[dict[str, str]]:
    if not _MANIFEST.exists():
        return []
    try:
        data = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    except Exception:
        return [{"rel": "", "bak": "", "sha": "", "label": "manifest 读不出来"}]
    return data if isinstance(data, list) else []


def _write_manifest(entries: list[dict[str, str]]) -> None:
    _BACKUP_DIR.mkdir(exist_ok=True)
    _MANIFEST.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")


def _clear_backups() -> None:
    if _BACKUP_DIR.exists():
        for item in _BACKUP_DIR.iterdir():
            item.unlink()
        _BACKUP_DIR.rmdir()


def _on_signal(signum: int, _frame: object) -> None:
    for rel in list(_PENDING):
        _put_back(rel)
    print(f"\n!! 收到信号 {signum}：已把源码还原后再退出", file=sys.stderr)
    raise SystemExit(130)


def _settle_previous_run(entries: list[dict[str, str]]) -> int:
    """收拾上次被强杀留下的残局。只有「内容确实等于我们写坏的那份」才敢自动还原。"""
    if not entries:
        return 0
    manual: list[str] = []
    for entry in entries:
        rel, bak = entry.get("rel", ""), entry.get("bak", "")
        path, bpath = pathlib.Path(rel), _BACKUP_DIR / bak
        if not (path.exists() and bpath.exists()):
            manual.append(f"{rel}（源文件或备份不见了）")
            continue
        if entry.get("sha") and _sha_of(_read_raw(path)) == entry["sha"]:
            _write_raw(path, _read_raw(bpath))
            bpath.unlink()
            print(f"还原上次中断残留：{rel}（内容与当初写坏的那份逐字节相同，可以安全还原）")
        else:
            manual.append(f"{rel}（当前内容既不是原文也不是我们的变异体 —— 你手改过？）")
    if manual:
        print("!! 上次运行被强杀，残局需要人工处理（我不拿旧备份覆盖你的改动）：")
        for item in manual:
            print("    -", item)
        print(f"    备份在 {_BACKUP_DIR}/；看过之后删掉该目录再重跑")
        return 2
    _clear_backups()
    return 0


_ROOT = pathlib.Path(__file__).resolve().parents[1]
_VENV_PYTHON = _ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _force_utf8_stdio() -> None:
    """把自己和子进程的 stdout/stderr 全钉成 UTF-8。

    为什么必须有这一条（2026-10-19 真踩）：本文件的正常用法就是
    `python scripts/mutant_check.py > evidence/matrix/<sha12>.log`——**重定向时**
    Windows 下 sys.stdout 默认是 **gbk**，跑到第一个 `⇒` 就
    `UnicodeEncodeError: 'gbk' codec can't encode character '\u21d2'`，
    于是矩阵在 #[34/75] 直接死掉、日志变成台账最需要的那份产物**恰恰生成不了**。
    子进程那边虽然注了 `PYTHONIOENCODING=utf-8`，父进程自己却没人管。
    设 `errors="replace"` 而不只是换编码：宁可打出一个 `?`，也不能让测量装置死于打印。
    """
    os.environ["PYTHONIOENCODING"] = "utf-8"
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        # 被替换过的流（如 capsys）不接受 reconfigure——这是预期内的事，不是错。
        with contextlib.suppress(OSError, ValueError):
            reconfigure(encoding="utf-8", errors="replace")

def _reexec_in_venv(argv: list[str]) -> int | None:
    """仓里有 .venv 而当前不是它在跑，就换成它重跑一遍（返回它的退出码）。

    为什么要这一条：本装置只看 `pytest` 的退出码，**任何**非 0 都记 KILLED。
    于是「用系统 python 跑，基线根本收集不起来」会被伪装成「牙齿很硬」——那是假证据。
    2026-10-15 真踩过：系统 python 里 cryptography 是残缺的 namespace 包 ⇒ 59 个
    collection ERROR；本文件顶部的 docstring 早写了要用 `./.venv/Scripts/python.exe`，
    但「记得手工选解释器」这种事靠不住，写进代码里才靠得住。
    """
    if not _VENV_PYTHON.exists() or pathlib.Path(sys.executable) == _VENV_PYTHON:
        return None
    print(f"（当前解释器 {sys.executable} 不是仓内 .venv，改成 {_VENV_PYTHON} 重跑）")
    child_env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [str(_VENV_PYTHON), str(pathlib.Path(__file__).resolve()), *argv], env=child_env
    )
    return proc.returncode


_BASELINES: dict[str, tuple[bool, bool, str]] = {}


def _baseline(test_file: str, env: dict[str, str]) -> tuple[bool, bool, str]:
    """跑一次**未被变异**的基线，返回 (基线绿?, 是收集失败而不是红测试?, 一行摘要)。

    没有这一步，KILLED 就只是「pytest 退出码非 0」，分不清「测试真的抓住了变异」
    和「这个文件本来就跑不起来」。基线不绿的片，它的 KILLED 只能算弱证据，
    所以要把「弱」写在纸上，而不是靠跑的人记得。
    """
    if test_file not in _BASELINES:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", *test_file.split(), "-x", "-q",
             "-p", "no:cacheprovider"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
        )
        out = proc.stdout or ""
        broken = "errors during collection" in out or proc.returncode in (2, 3, 4)
        last = next((ln.strip() for ln in reversed(out.splitlines()) if ln.strip()), "")
        state = "绿" if proc.returncode == 0 else ("收集失败" if broken else "红")
        _BASELINES[test_file] = (proc.returncode == 0, broken, last[:90] or f"rc={proc.returncode}")
        print(f"基线 {test_file}: {state} —— {_BASELINES[test_file][2]}")
    return _BASELINES[test_file]


def _verdict(mutation_failed: bool, baseline_green: bool, baseline_broken: bool) -> str:
    """一片变异的结论分档：基线状态决定「杀死」这个词有多硬。

    · 变异后测试仍全绿 ⇒ `SURVIVED`（真缺口还是等价变异，看 expect）；
    · 变异后变红、且基线绿 ⇒ `KILLED`（强证据）；
    · 变异后变红、但基线本来就红 ⇒ `KILLED?`（弱证据；本仓已知一例：台账守卫的基线本来红）；
    · 基线压根跑不起来（收集失败）⇒ `BROKEN`：这片的所有「杀死」**都不算证据**。

    没有最后一档，坏环境会被伪装成高杀死率（判据见 tests/test_mutant_check_verdicts.py）。
    """
    if not mutation_failed:
        return "SURVIVED"
    if baseline_broken:
        return "BROKEN"
    return "KILLED" if baseline_green else "KILLED?"


def _problem(rows: list[tuple[str, str, str]]) -> bool:
    """有这些就不给绿：真缺口、原文没匹配上（源码变了？）、基线不可用。"""
    return any(
        verdict in ("SKIP", "BROKEN") or (verdict == "SURVIVED" and expect == KILL)
        for verdict, expect, _ in rows
    )


def _selected(argv: list[str]) -> list[tuple[str, str, str, str, str, str]]:
    """`--only 子串` 只挑标签/测试文件里含该子串的用例；不给就全跑。"""
    if "--only" not in argv:
        return list(CASES)
    at = argv.index("--only")
    needle = argv[at + 1] if len(argv) > at + 1 else ""
    picked = [c for c in CASES if needle in c[1] or needle in c[5]]
    print(f"--only {needle!r}：选中 {len(picked)}/{len(CASES)} 条", flush=True)
    if not picked:
        print(
            f"--only {needle!r} 一条都没选中 ⇒ **这不是「全绿」，是没跑**。\n"
            "        匹配范围只有标签（第 2 字段）与测试文件（第 6 字段）；"
            "拿源码文件名当子串是最常见的踩法"
            "（源码路径不在匹配范围内，如 `--only check_delivery_push`）。",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(2)
    return picked


def _dry_run(cases: list[tuple[str, str, str, str, str, str]]) -> int:
    """只校验锚点：每条的 old 必须在目标文件里**恰好命中 1 次**：

    0 次 = 锚点过期（源码改过，这条变异已经施加不上去了）⇒ 装置会判 SKIP；
    >1 次 = 不加区分（会把别处的同样代码也改坏）⇒ 装置也会判 SKIP。
    判据必须**与装置真实判据一致**，否则这个预检就预测不准它承诺预测的东西。
    列里那个「变异体×N」只是参考：N>0 不代表这条有问题——我们是在 `old` 处替换 1 次，
    别处早就有同样的代码片段（如 `return "KILLED"`）很正常。
    """
    bad = 0
    print("锚点检查（old 必须恰好命中 1 次 = 装置不会判 SKIP）：")
    for index, (_expect, label, rel, old, new, _test) in enumerate(cases):
        text = _source_text(_read_raw(pathlib.Path(rel)))
        hit, hit_new = text.count(old), text.count(new)
        ok = hit == 1
        flag = "OK  " if ok else "!!  "
        print(f"{flag}[{index + 1}/{len(cases)}] {rel}  old×{hit} 变异体×{hit_new}  {label[:40]}")
        bad += 0 if ok else 1
    print(f"\n锚点检查：{len(cases) - bad}/{len(cases)} 条可用", flush=True)
    return 1 if bad else 0


def _flag_value(argv: list[str], flag: str, default: str = "") -> str:
    if flag not in argv:
        return default
    at = argv.index(flag)
    return argv[at + 1] if len(argv) > at + 1 else default


_ROOT = pathlib.Path(__file__).resolve().parent.parent
# 浅检出探针要在哪些判据上试：它们都得靠 git 历史（fetch-depth: 0 是承重的）
_SHALLOW_GUARDS = (
    "tests/test_matrix_evidence.py",
    "tests/test_ci_evidence.py",
    "tests/test_delivery_receipts.py",
    "tests/test_delivery_ledger.py",
)


def _shallow_probe(sha: str) -> tuple[int, int]:
    """在**只抓一个提交**的克隆里跑回执/证据守卫，返回 (rc, 红条数)。

    为什么非得真抓浅克隆：`fetch-depth: 0` 写在 workflow 里只是一句话，
    只有「浅了就必须红」才能证明它承重。rc=0 一律不许写进证据（那是自我否定）。
    """
    with tempfile.TemporaryDirectory(prefix="shallow-probe-") as td:
        run = lambda *a: subprocess.run(a, capture_output=True, text=True,  # noqa: E731
                                       encoding="utf-8", errors="replace")
        for step in (
            ("git", "init", "-q", td),
            ("git", "-C", td, "remote", "add", "origin", str(_ROOT)),
            ("git", "-C", td, "fetch", "-q", "--depth", "1", "origin", sha),
            ("git", "-C", td, "checkout", "-q", "FETCH_HEAD"),
        ):
            r = run(*step)
            if r.returncode != 0:
                print(f"浅检出探针准备失败：{' '.join(step)} ⇒ {r.stderr.strip()[:300]}",
                      file=sys.stderr, flush=True)
                return 9, 0
        env = dict(os.environ, PYTHONIOENCODING="utf-8",
                   PYTHONPATH=str(pathlib.Path(td) / "src"))
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", *_SHALLOW_GUARDS, "-q",
             "-p", "no:cacheprovider", "--tb=no"],
            cwd=td, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
        )
        red = 0
        out = proc.stdout or ""
        # -q 的「N failed, M passed」行**不在最后**：它后面还跟着 FAILED 明细。
        # 所以先数明细行（确定性最强），再拿汇总数交叉核对，两者不一致就报出来。
        detail = [line for line in out.splitlines() if line.startswith("FAILED ")]
        counted = re.search(r"(\d+) failed", out)
        red = len(detail) if detail else (int(counted.group(1)) if counted else 0)
        if counted and detail and int(counted.group(1)) != len(detail):
            print(f"浅检出探针：汇总说 {counted.group(1)} 条红，"
                  f"明细只有 {len(detail)} 行（以明细为准）",
                  file=sys.stderr, flush=True)
        if proc.returncode != 0 and red == 0:
            tail = "\n".join((out or proc.stderr or "").splitlines()[-6:])
            print(f"浅检出探针：rc={proc.returncode} 但没数出红条数"
                  f"（多半是收集/环境问题，不是判据变强）：\n{tail}",
                  file=sys.stderr, flush=True)
        return proc.returncode, red


def _write_evidence(out: pathlib.Path, sha: str, rows: list[tuple[str, str, str]],
                    shallow_rc: int, shallow_red: int) -> tuple[int, int, int]:
    """把**真实判定**写成守卫认得的那份证据；返回 (total, positive, negative)。

    反自证设计（关键）：这里的「OK」列**从 verdict 算出来**，不是抄的；
    只要有一条期望没满足，调用方就不写文件（MATRIX FAIL 不许当证据入库）。
    """
    idx_m = idx_r = 0
    lines = [f"=== matrix mutant_check HEAD={sha} ===",
             "# 列：编号 判定 期望 标签 判定结论（OK=与期望一致；SKIP（原因）=本相位不适用）"]
    for verdict, expect, label in rows:
        tag_type = "M" if expect == KILL else "R"
        # 编号跨所有行（含 SKIP），所以编号会跳；但**计数只算非 SKIP**——
        # 否则 positive+negative ≠ total，正好是「没跑的臂被当成通过」那个病。
        if tag_type == "M":
            idx_m += 1
            no = idx_m
        else:
            idx_r += 1
            no = idx_r
        want = verdict in ("KILLED", "KILLED?") if expect == KILL else verdict == "SURVIVED"
        head = f"{tag_type}{no:02d} {verdict:<9}{expect:<6}"
        if verdict == "SKIP":
            lines.append(f"{head}SKIP（锚点没命中：源码改过或这段被删了） {label}")
            continue
        lines.append(f"{head}{label} {'OK' if want else '!!'}")
    live_m = sum(1 for v, e, _l in rows if v != "SKIP" and e == KILL)
    live_r = sum(1 for v, e, _l in rows if v != "SKIP" and e != KILL)
    pos, neg = live_m, live_r
    live = sum(1 for v, _e, _l in rows if v != "SKIP")
    lines.append(f"MATRIX PASS: {live}/{live} 符合预期（{pos} 条正对照 + {neg} 条反向对照）")
    lines.append(f"SHALLOW depth=1: rc={shallow_rc} 红 {shallow_red} 条"
                 f"（在只抓一个提交的克隆里跑 "
                 f"{'、'.join(p.split('/')[-1] for p in _SHALLOW_GUARDS)}"
                 f" —— 抓不到历史就必须红，fetch-depth: 0 才算承重）")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return live, pos, neg


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)  # 非 TTY 下默认块缓冲 ⇒ 长跑日志全程 0 字节
    argv = sys.argv[1:]
    _force_utf8_stdio()
    reexec = _reexec_in_venv(argv)
    if reexec is not None:
        return reexec
    entries = _load_manifest()
    if "--restore" in argv:
        return _settle_previous_run(entries)
    cases = _selected(argv)
    if "--dry" in argv:
        return _dry_run(cases)
    leftover = _settle_previous_run(entries)
    if leftover:
        return leftover
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    rows: list[tuple[str, str, str]] = []
    suspect: list[str] = []
    backups: list[dict[str, str]] = []
    for index, (expect, label, rel, old, new, test_file) in enumerate(cases):
        t_case = time.monotonic()
        path = pathlib.Path(rel)
        raw = _read_raw(path)
        crlf = _is_crlf(raw)
        text = _source_text(raw)
        if text.count(old) != 1:
            rows.append(("SKIP", expect, label))
            print(
                f"[{index + 1}/{len(cases)}] SKIP  锚点命中 {text.count(old)} 次  {label[:30]}",
                flush=True,
            )
            if text.count(new) == 1 and not text.count(old):
                suspect.append(label)
            continue
        data = _encode_like(text.replace(old, new, 1), crlf)
        bak = f"{index:03d}__{path.name}"
        _BACKUP_DIR.mkdir(exist_ok=True)
        _write_raw(_BACKUP_DIR / bak, raw)
        backups = [b for b in backups if b["rel"] != rel]
        backups.append({"rel": rel, "bak": bak, "sha": _sha_of(data), "label": label})
        _write_manifest(backups)  # 先落盘，再改源码：被强杀也留得下收拾残局的凭据
        _PENDING[rel] = (path, raw)
        try:
            _write_raw(path, data)
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", *test_file.split(), "-x", "-q",
                 "-p", "no:cacheprovider"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
            )
        finally:
            _put_back(rel)
            backups = [b for b in backups if b["bak"] != bak]
            _write_manifest(backups)
        green, broken, _ = _baseline(test_file, env)
        verdict = _verdict(proc.returncode != 0, green, broken)
        rows.append((verdict, expect, label))
        print(
            f"[{index + 1}/{len(cases)}] {verdict:<9}"
            f"基线{'绿' if green else ('跑不起来' if broken else '红')}  "
            f"{label[:40]}  ({time.monotonic() - t_case:.1f}s)",
            flush=True,
        )
    _clear_backups()

    print("结果  期望   变异（KILLED? = 基线本身红，弱证据；BROKEN = 基线跑不起来，证据无效）")
    for verdict, expect, label in rows:
        want = verdict in ("KILLED", "KILLED?") if expect == KILL else verdict == "SURVIVED"
        print(f"{'OK  ' if want else '!!  '}{verdict:<9}{expect:<6}{label}")

    skipped = [r for r in rows if r[0] == "SKIP"]
    gaps = [r for r in rows if r[0] == "SURVIVED" and r[1] == KILL]
    broken_rows = [r for r in rows if r[0] == "BROKEN"]
    killed = sum(1 for r in rows if r[0] == "KILLED" and r[1] == KILL)
    weak = sum(1 for r in rows if r[0] == "KILLED?" and r[1] == KILL)
    equiv = sum(1 for r in rows if r[1] == EQUIV)
    print()
    print(
        f"共 {len(cases)} 条：期望杀死且已杀死 {killed + weak}"
        f"（强证据 {killed} / 弱证据（基线红）{weak}），"
        f"等价变异 {equiv}，跳过 {len(skipped)}，基线不可用 {len(broken_rows)}"
    )
    for r in broken_rows:
        print("  基线跑不起来（不是缺口，是环境/收集问题；它的『杀死』不算证据）：", r[2])
    for r in gaps:
        print("  缺口（该补测试或让它可测）：", r[2])
    for r in skipped:
        print("  原文没匹配上（源码变了？）：", r[2])
    for label in suspect:
        print("  !! 该处当前内容**正是变异体本身**，源码可能残留着上次被强杀留下的变异：", label)
        print("     还原：`python scripts/mutant_check.py --restore`，或用 git 恢复那个文件")

    evidence_out = _flag_value(argv, "--evidence-out")
    if evidence_out:
        if _problem(rows):
            print(
                "\n--evidence-out：**不写证据**——矩阵本身没全绿（有缺口/跳过/基线不可用）。"
                "\n  MATRIX FAIL 不许当证据入库；先把上面那几条修到全绿，再重跑。",
                file=sys.stderr, flush=True,
            )
            return 2
        sha = _flag_value(argv, "--evidence-head")
        if not sha:
            sha = subprocess.run(["git", "-C", str(_ROOT), "rev-parse", "HEAD"],
                                 capture_output=True, text=True).stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            print(f"--evidence-head 不是 40 位 sha：{sha!r}", file=sys.stderr, flush=True)
            return 2
        print("\n浅检出探针：在只抓一个提交的克隆里跑回执/证据守卫…", flush=True)
        sh_rc, sh_red = _shallow_probe(sha)
        total, pos, neg = _write_evidence(pathlib.Path(evidence_out), sha, rows, sh_rc, sh_red)
        print(f"证据已写：{evidence_out}")
        print(f"  被证明的提交 {sha[:12]} / 对照 {total} 条（正 {pos} + 反 {neg}）"
              f"/ 浅检出 rc={sh_rc} 红 {sh_red} 条")
        if sh_rc == 0:
            print("  !! 浅检出里居然全绿 ⇒ 它没证明「fetch-depth: 0 承重」，"
                  "守卫会拒（rc=0 判假洞察）",
                  file=sys.stderr, flush=True)
            return 3
    return 1 if _problem(rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
