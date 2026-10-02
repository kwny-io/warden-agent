# 变异测试（mutation testing）：证明"测试全绿"不等于"测试有效"

本文件记录本仓库接入变异测试的**口径、分片表、实测数字、逐条裁决和边界**。
所有数字都可以用文末的命令当场复现——包括"哪几条存活是被判定为等价变异、不必再追"。

---

## 一、它回答的是覆盖率答不了的那个问题

行覆盖率只回答"这行**被执行**过吗"，不回答"执行了之后**有没有人断言**"。
几十行没断言的代码可以轻松把覆盖率顶上去——这就是"假绿"。

变异测试反过来做：把源码**改坏**（`<` 改成 `<=`、`True` 改成 `False`、返回值换成 `None`、
边界值 ±1、删掉一次递归……），再跑一遍测试。

| 结果 | 叫法 | 含义 |
| --- | --- | --- |
| 测试**变红** | 突变体被**杀死** | 真有测试在盯着这段逻辑——它坏掉就有人喊 |
| 测试**仍然全绿** | 突变体**存活** | 这段逻辑**没有任何测试约束它**：坏了没人知道 |

所以它不是覆盖率的替代品，是覆盖率的**反作弊检查**。

---

## 二、怎么跑

### 分片（shard）：四个"错了会出安全事故"的模块组

变异测试的成本 = 突变体数量 × 每组测试的耗时，两者是乘性关系。全仓库跑一遍是
几十小时——那是"跑不完的门禁"，等于没有门禁。所以按模块**分片**：每片只变异它自己的
文件、只跑盖得住这些文件的测试，四片在 CI 里真并行。

**分片表的唯一来源是 `scripts/mutation_shards.py`**（片名、变谁、跑哪些测试、门槛多少、实测基线）。
`pyproject.toml` 的 `[tool.mutmut]` 是**渲染产物**，不是手写的第二份真相：

```bash
./.venv/Scripts/python.exe scripts/mutation_shards.py list          # 看表
./.venv/Scripts/python.exe scripts/mutation_shards.py render ratelimit   # 把 pyproject 重写成该片
./.venv/Scripts/python.exe scripts/mutation_shards.py verify-config # 禁止"表和配置各说各话"
./.venv/Scripts/python.exe scripts/mutation_shards.py matrix        # 输出 CI matrix JSON
```

| 分片 | 模块 | 错了会怎样 |
| --- | --- | --- |
| `core-security` | `core/redaction.py`、`web/security_headers.py`、`policy/policy.py` | 密钥明文进日志、安全响应头缺一条、该拦的路径没拦（审批被绕过） |
| `concurrency` | `runtime/locking.py` | 同一个 run 被两个进程同时执行（重复扣费、重复改仓库） |
| `ratelimit` | `web/ratelimit.py` | 窗口/计数写错：要么被绕过打爆后端，要么把正常用户挡在外面 |
| `patch-guard` | `git/patch.py` | 补丁落盘前的路径校验漏一处 = 文件被写到仓库外面 |

> 每片的 `tests` 必须盖住该片模块**被任何测试覆盖过的行**。因为
> `mutate_only_covered_lines = true`：没被这些测试跑到的行**根本不会被变异**，
> 于是"分数看着还行"其实是变异面被静默缩小了——这比分数低危险得多。
> 最小集合用 `scripts/mutation_shards.py suggest-tests` 机械算出（贪心集合覆盖），
> `check-tests` 会核对表里这些文件的实际覆盖率（低于 99% 就红）。

### Windows（开发机）

mutmut 3 在原生 Windows 上拒跑（需要 `fcntl`/WSL）。脚本内置了容器自包裹：

```bash
bash scripts/mutation.sh --docker --shard=ratelimit
```

容器里跑的是 `uv sync --frozen --extra dev`（依赖 = 仓库锁定的那一套，不是手写包清单——
手写清单**已经漂过一次**，见第六节）。结果写在 `mutants/mutmut-cicd-stats.json`。

### Linux / macOS / CI

```bash
bash scripts/mutation.sh --shard=ratelimit   # 单片
bash scripts/mutation.sh --all-shards        # 全部（慢）
```

CI 里是 `.github/workflows/ci.yml` 的 `mutation` job：**夜间定时 + 手动触发**，
matrix 跑四个分片（`if: schedule || workflow_dispatch`）。
不放在每次 push 的必跑链路里——它慢，而且它保护的是"测试质量"，
不是"这次改动是否正确"，让它挡住每个 PR 只会逼人绕过门禁。

---

## 三、分数口径（这一节比分数重要）

```
score = killed / (total - skipped - no_tests - timeout)
```

四条口径都是刻意的：

1. **`timeout` 不算杀死。** 突变体把代码改成死循环，测试超时——这是"没跑出来"，
   不是"被抓住"。把它算成杀死，分数会虚高。（`timeout` **留在分母里**：超时会把分数压低，刻意的保守口径。）
2. **`skipped` / `no_tests` 从分母里扣掉**（`no_tests` 是没有任何测试文件覆盖到的突变体）。
   它们既没被验证也没被证伪，留在分母里会让分数变成环境噪音。
3. **门禁阈值 = 该片实测基线 − 2 个百分点**，不设成"必须 ≥ 某漂亮数字"。
   变异分数会随实现重构上下浮动（同一条逻辑换个写法，突变体数量和等价突变体都会变），
   卡死会让所有人为了绿而绿。留 2 个点：**倒退必须被看见，正常浮动不该阻塞**。
   阈值与基线必须成对出现，且阈值必须低于基线、又不能低太多——
   这两条由 `tests/test_mutation_shards.py` 按住。
4. **"没跑"和"跑了但分数低"必须分开报。** 见下面的退出码 5。

### 门禁脚本与退出码

```bash
./.venv/Scripts/python.exe scripts/mutation_gate.py 94.4 mutants/mutmut-cicd-stats.json
# 0=达标 / 1=分数不够 / 2=文件不存在或用法错 / 3=文件读不出来 / 4=可评估突变体为 0 / 5=一个突变体都没跑完
```

四个"不通过"码必须彼此可区分，这点是**实测撞出来的**：原先 `load_stats` 直接抛
`FileNotFoundError` 出去，Python 进程的退出码也是 1——跟"分数不够"完全一样，
维护者看到红会去补根本不缺的测试。现在文件找不到返回 2、格式错返回 3、
配置空转返回 4，日志里一眼分得清。

**退出码 5 是一次真实假绿换来的。** concurrency 分片第一次跑：mutmut 先生成 206 个突变体，
然后跑**基线测试**收统计；基线里一条用例因竞态失败 → 整轮中止，206 个突变体全部停在
"not checked"，`export-cicd-stats` 导出 `total=206` 而 `killed/survived/timeout` 全是 0。
该片当时还没有实测基线、门槛是 0.0，于是 `0.0 >= 0.0` **直接通过**。
现在：`total > 0` 而三者全 0 = 一个都没执行完 → 退出码 5，文案明确说"不是分数问题，是没跑起来"。

它单独成文件、而不在 shell 里写段 `python -c`，是因为"分数够不够"是个**会改变 CI 结论的决策**——
和审批决策、回滚判断是同一类东西，必须能被单测固定住。`tests/test_mutation_gate.py`
用 17 条用例固定了全部分支，包括"恰好等于门槛算过、差 0.1 个点不算"和上面这个退出码 5。

---

## 四、实测结果

### core-security（2026-09-29，容器内全量）

| 阶段 | killed | survived | 分数 | 说明 |
| --- | --- | --- | --- | --- |
| 首次接入（三个模块全量，未补测） | 228 | 22 | 91.2% | 这就是"假绿"的真实水位 |
| 补完第一批测试 | 239 | 11 | 95.6% | 11 条存活全部逐条看过 diff |
| 处置完 11 条存活（本轮收尾） | **242** | **9** | **96.4%** | 剩 9 条**全部**是等价变异 |

三行的 `total` 不完全相同（250 → 251），因为 mutmut 每轮按**当前测试能映射到的**代码行
重新生成突变体：补了测试之后有一行进入映射，多出一个突变体（并且它被杀死）。
口径没变，结论也不受影响：**存活从 22 降到 9，而这 9 条全是杀不掉的等价变异。**

### 四个分片现状（2026-10-01，2026-10-02 官方复跑确认）

> **官方复跑（2026-10-02 02:15–02:35，容器内 `bash scripts/mutation.sh --docker --all-shards`，跑的是批 40
> 改动后的工作区）**：四片全部「通过」，数字与下表**逐字相同**，存活 16 条（9+2+0+5）与裁决表
> **逐条同名**（`scripts/mutant_check.py` 0 失败）。也就是说这张表不是历史快照，而是当前工作区的实测值。
> `concurrency` 存活 0、`patch-guard` 存活 5 条全是等价变异 —— 两片都**没有可杀的突变体**了。

| 分片 | 突变体（可评估） | killed | survived | timeout | 分数 | 门槛 | 缺口状态 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `core-security` | 251 | 242 | 9 | 0 | 96.4% | 94.4% | 9 条**全是等价变异**，已结清 |
| `ratelimit` | 95 | 93 | 2 | 0 | 97.9% | 95.9% | 2 条**全是等价变异**，已结清 |
| `concurrency` | 206 | **206** | **0** | 0 | **100.0%** | 98.0% | 13 条真缺口全被杀，**等价变异 0 条**，已结清（见下） |
| `patch-guard` | 346 | 341 | 5 | 0 | **98.6%** | 96.6% | 5 条**全是等价变异**（**可杀的突变体 0 条**），已结清——四轮重跑的账见下 |

四片现在都**没有未裁决的存活突变体**：能杀掉的都被杀掉了，剩下的每一条都逐条实测过、
写在第五节。`patch-guard` 从最差的一片（存活 80/291）到 98.6%，中间的账是：

| 轮次（2026-） | total | killed | survived | timeout | 分数 | 这一轮变了什么 |
| --- | --- | --- | --- | --- | --- | --- |
| 09-30 接入 | 291 | 209 | —※ | —※ | 71.8% | 先把 `tests/test_patch_parser.py` 建起来 |
| 09-30 补测 | 340 | 287 | —※ | —※ | 84.4% | 按覆盖到的分支补参数化用例 |
| 09-30 补边界 | 346 | 322 | 19 | 5 | 93.1% | 补上 CRLF / 两文件 / 预算 / 锚点取值 |
| 10-01 裁决 | 346 | 340 | 5 | 1 | 98.3% | 24 条存活逐条裁决 + 按裁决补测（含两条**测试写法**的教训） |
| 10-01 超时口径 | 346 | 341 | 5 | **0** | **98.6%** | 给 pytest 加 `--timeout=30` + mutmut 外层上限抬到 60 倍 |

最后一轮把最后的 1 条超时也变成了"pytest 内部变红"：`timeout` 从 5 条降到 0 条，
这条口径的细节与依据见第五节末尾。

> ※ 头两轮只留了比例（`killed/total`），没留存活与超时的拆分——那时还没有"裁决清单"
> 这个概念。从第三轮起每条未杀都逐条看过 diff，因此才有后面的裁决表。

`mutants/mutmut-cicd-stats.json` 是机器可读的原始台账（每片覆盖一次）；
分片结果清单在 `mutants/mutmut-results-<片名>.txt`，逐条 diff 在 `mutants/survivor-diffs-<片名>.txt`。

> 口径说明：**"存活 N 条"不是"N 个必补的测试"。** 逐条看 diff 之后分成两类——
> 真缺口（本该被抓住却没抓住）与等价变异（改了与没改行为完全一样）。
> 只对前者补测试；后者写进下节，避免后人再追一次不存在的漏洞。

### `concurrency`：47 → 0（存活不是"等价"，而是被测试自己掩盖了）

第一轮 47 条存活里，34 条在第一轮就补测杀掉了；剩下的 13 条**二轮全杀（13/13）**，
没有一条是等价变异。其中两条的存活原因值得记下来，因为它是**测试写法**的坑而不是实现的坑：

- **`owner_of` 把状态抹掉了**：`InProcessRunLock.owner_of()` 会在"已过期"时顺手删掉条目。
  边界用例如果先调 `owner_of`、再调 `acquire`，那 `acquire` 看到的是"键空闲"，
  于是 `expires_at > now` 写成 `>=`（差一个等号）照样通过。
  修法：断言前**不碰 `owner_of`**，让"已过期但条目还在"这个状态活着（`tests/test_run_lock.py::test_边界_恰好到期这一刻就能被接管且不能续租`）。
- **只看上界的超时断言**：`stop()` 的 join 上限（2 秒）原先只断言"不超过 2.5 秒"。
  于是把 `thread = self._thread` 改成 `thread = None`（**根本不 join**）也能过——
  它同时也说明：这类"性能/上限"断言必须**上下界都写**，否则"什么都没做"永远最快。
  修法：加下界（`elapsed >= 1.5`）。

其余 11 条是"格式/文案/参数没断言"（`token_hex(4)` 的长度、丢锁告警的逐字首尾、
告警打在哪个 logger、`type(store).__name__`、`SqlRunLock(store)` 有没有真把 store 交下去）——
**都不影响功能，所以只有显式断言才守得住**。13 条的逐条验证脚本：`scripts/mutant_check.py`。

---

## 五、等价变异（杀不掉是正常的，不要再补测试）

"等价变异"= 突变体和原代码**行为完全一样**，任何测试都不可能区分它们。
写在这里是为了留一份"已经想过、不是漏了"的记录。每一条都能用
`scripts/mutant_check.py` 当场验证（把变异真写进源码、只跑对应测试文件、再还原）。

| 模块 | 突变体 ID | 变异 | 为什么行为完全一样 |
| --- | --- | --- | --- |
| `core/redaction.py` | `x__fingerprint__mutmut_7` | sha256 的 `utf-8` → `UTF-8` | Python 的 codec 名大小写不敏感，编码结果逐字节相同 |
| `core/redaction.py` | `x_redact_arguments__mutmut_14` | `bytes` 分支解码的 `utf-8` → `UTF-8` | 同上 |
| `core/redaction.py` | `x_redact_text__mutmut_3` | `group == 0` → `== 1` | 该处 `_sub` 未传 `_g`，默认同为 `0`，两个分支产出相同 |
| `core/redaction.py` | `x_redact_arguments__mutmut_33` | `k if isinstance(k, str) else str(k)` → 恒取 `str(k)` | 键是 `str` 时 `str(k) == k`，两分支结果一样 |
| `policy/policy.py` | `x_deny_when_path_in_protected__mutmut_4/6/9` | 取参默认值 `""` → `None` / 省略 / `"XXXX"` | 三者都不落在任何受保护前缀下，判定结果不变（同一条源码行的 3 个突变） |
| `policy/policy.py` | `x_ask_when_tool_not_pure__mutmut_9` | 非纯工具判定异常后 `pure = False` → `None` | `None` 同样是假值，仍走 `ASK`——fail-closed 行为不变 |
| `policy/policy.py` | `xǁPolicyEngineǁevaluate__mutmut_10` | 决策排序权重 `DENY: 2` → `DENY: 3` | 它仍是最严的一档，`max()` 的结果不变 |
| `web/ratelimit.py` | `xǁRateLimiterǁ__init____mutmut_7` | 报错文案加 `XX` 前后缀 | 测试断言的是"文案含关键词"，加前后缀不改变契约（要杀死它就得断言逐字节相等——那是过度断言） |
| `web/ratelimit.py` | `x_parse_rate_limit__mutmut_20` | `text.partition("/")` → `rpartition("/")` | 只有一个 `/` 时两者相同；≥2 个 `/` 时，`partition` 的右半段与 `rpartition` 的左半段都必然含 `/`，`int()` 都会失败 → 可观测行为完全相同 |
| （`concurrency` 片现在**没有**等价变异） | — | — | 该片 13 条存活逐条打回源码后**全被杀死了**，没有一条是等价变异。对比记录留在第五节末尾的"存活≠必补测试"说明里 |

### `patch-guard`：24 条（19 存活 + 5 超时）已逐条裁决完（2026-10-01）

**结论：5 条等价变异（杀不掉，已逐条实测证实）+ 19 条真缺口（每一条都补了会变红的用例）。**
清单在 `mutants/mutmut-results-patch-guard.txt`；复现走 `scripts/mutant_check.py`（把变异真写进
源码、只跑该片选中的测试文件、跑完核对 sha256 还原）。

#### 5 条等价变异

| 突变体 | 变异 | 为什么行为完全一样 |
| --- | --- | --- |
| `x__reject_unsafe_path__mutmut_17` | 路径段过滤 `seg not in ("", ".")` → `("XXXX", ".")` | 丢掉的只是空串过滤。判定只问 `".." in segments`，而空串永远不等于 `".."` |
| `x__reject_unsafe_path__mutmut_18` | 同上 → `("", "XX.XX")` | 丢掉的只是 `"."` 过滤，判据同上 |
| `parse__mutmut_58` | 文件头分支 `cur_new = ""` → `None` | 下游两处都是真值判断：`oldp.strip() or newp.strip()` 短路、`newp if newp else anchor` —— `None` 与 `""` 同假 |
| `parse__mutmut_63` | 文件数预算 `(1 if cur_old else 0)` → `(1 if (cur_old) or True else 0)` | 进这一支时 `cur_old` 刚被赋成**非空**路径（空路径更早就抛 `PatchConflict`），条件恒真 |
| `parse__mutmut_65` | 同上 → `(1 if cur_old else 1)` | 同理：`else` 那一支不可达 |

**这 5 条就是第五轮里唯一活下来的东西**（341/346 = 98.6%、`timeout` 0）——也就是说：
这一片里凡是"改错了会**被观察到**"的突变体，现在都有测试按住。

还有两条**曾经**写在这张表里、现在已经被杀死：`apply__mutmut_19` / `50`
（`read_text` / `write_text` 的 `encoding="utf-8"` → `"UTF-8"`；codec 名大小写不敏感，
行为确实一样）。它们是被下面那条 kwargs 契約用例顺带收掉的（那条断言把编码名当字串钉住）。
这正好又是"等价是会过期的结论"的一条证据。

#### 19 条真缺口（全部是"改了以后测试全绿、行为已经错"）

按"错在哪"排：

| 突变体 | 变异 | 会出什么事 | 现在由谁按住 |
| --- | --- | --- | --- |
| `__init____mutmut_2` | 默认行预算 `200_000` → `200001` | 预算这类"安全阀"数字错一格不会有任何调用方发现（README 写的是 20 万行） | `test_默认预算就是文档里那三个数_200文件_20万行_5MiB` |
| `parse__mutmut_20` | CRLF 归一化 `replace("\r\n", "\n")` → `replace("XX\r\nXX", "\n")`（匹配不上，后面那半句把 CRLF 拆成**两行**） | **删除补丁静默失效**：多出来的空行插在 `--- a/x` 与 `+++ /dev/null` 之间，`deleted` 从 `True` 变成 `False`——补丁"应用成功"但文件没被删 | `test_CRLF的删除补丁必须真的算删除` |
| `parse__mutmut_96` / `98` / `176` | 解析循环里三处 `idx += 1` → `idx = 1` / `idx += 2` | 多跳一行，正好跨掉 `@@` 或 `---`：**整个 hunk 被丢掉**，补丁报成功但一行没改 | `test_块内非hunk行只跳过它自己一行`、`test_块前面有一行前导行时两个块各自只收一次且hunk不丢` |
| `parse__mutmut_99` | 文件头分支的 `continue` → `break` | 收下第一个文件块就不再往下走：**多文件补丁只应用第一个** | `test_正文里孤立的加号头会收尾并更新目标路径` + 第二轮的参数化用例 |
| `parse__mutmut_137` | hunk 正文扫描 `not lines[a].startswith("+++")` → `"XX+++XX"` | 正文里孤立的 `+++` 不再收尾：把下一块的目标路径吃进当前块 | `test_正文里孤立的加号头会收尾并更新目标路径` |
| `parse__mutmut_97` / `124` / `174`、`_apply_file__mutmut_25` / `46` | `idx += 1` → `idx -= 1` / `idx = 1`、`a = idx + 1` → `idx - 1`、`pos += 1` → `pos = 1`（**都会死循环**） | 测试被挂住。改成死循环的代码**不会让任何测试变红**——它只是"不再结束" | `pytest --timeout=30`（分片表生成的配置）：在 pytest 内部变红 = 杀死。第五轮 `timeout` 归零就是这条生效的证据，理由见下 |
| `apply__mutmut_17` / `42` / `45` / `46` | `read_text` / `write_text` 的 `encoding="utf-8"` → `None` / 省略 | 跟随本机 locale：在 cp1252 / GBK 的机器上读中文补丁直接 `UnicodeDecodeError`，写出去的是本机编码——**只有本机能跑通，一上 CI 就炸** | `test_读写必须显式utf8而不是跟随本机locale` |
| `apply__mutmut_19` / `50` | 同上 → `"UTF-8"`（只有大小写） | 行为一样（等价变异），但"显式声明的编码名"不该被改掉 | 同一条 kwargs 契约用例（顺带收） |
| `apply__mutmut_43` / `46` | `write_text(..., newline="\n")` → `newline=None` / 省略 | 落盘换行符跟随平台（Windows 上变 CRLF）：**同一份补丁在不同机器改出不同字节**，hash 校验与后续 git diff 全乱 | `test_CRLF换行的diff归一化后hunk完整且落盘不带回车`（断言落盘字节） |

（早期版本这里写的是"7 条等价 + 17 条真缺口"，差异就是 `apply__mutmut_19` / `50` 从等价挪到了真缺口。）

#### 两条"测试写法"的教训（比数字值钱）

1. **反射式断言永远杀不死变异。** 曾经写了一条"用 `inspect.signature` 读出 `read_text` 的默认参数、
   断言它等于 `"utf-8"`"的用例——看上去正好按住 `encoding=None` 那类改法，实测**一条都没杀掉**。
   原因在 mutmut 的运行方式：它只跑"覆盖到该函数体"的测试（`tests_by_mangled_function_name` 逐条筛），
   而反射不执行那个函数体，于是这条用例根本没被映射到任何突变体上。
   改法：直接断言**调用契约**——monkeypatch 掉 `Path.read_text` / `Path.write_text`，记下收到的
   kwargs，再断言编码与换行是显式的（`tests/test_patch_parser.py::test_读写必须显式utf8而不是跟随本机locale`）。
2. **`encoding=None` / `newline=None` 在 Linux + UTF-8 环境里和显式值逐字节相同**，
   于是"行为断言"在那里天然没有判别力（本机编码就是 UTF-8，`os.linesep` 也是 `\n`）——
   必须把 kwargs 本身当契约钉住。这也是那两条等价变异会"过期"的原因。

#### "超时"算未杀，以及为什么把它搬进 pytest（已实测生效）

门禁口径（第三节）把 `timeout` 算**未杀**：从"测试有没有变红"这个角度，超时不能证明有测试
守住了这段逻辑，它只证明**这次运行没跑完**。而这一片的超时恰好全是同一类改法
（把自增改成死循环）——也就是说：**它们既是真缺口，又能靠"挂住"把证据藏起来。**

mutmut 对每条突变体本来就有两道**外部**上限（都随 `timeout_multiplier` 缩放）：

- 墙钟：`(基线耗时 + timeout_constant) × timeout_multiplier`（`mutmut/workers/isolation.py:875-876`）；
- CPU：`ceil((基线耗时 + timeout_constant) × timeout_multiplier × 2)`，由子进程的
  `resource.setrlimit(RLIMIT_CPU, ...)` 兜住，SIGXCPU 之后 1 秒 SIGKILL
  （`mutmut/__main__.py:1046`、`workers/isolation.py:864-865`）。

默认倍数 15 意味着"谁先掐断"由 mutmut 说了算，而外面那一刀只被记成 `timeout`。所以分片表里
给这一片加了两个数（都在 `scripts/mutation_shards.py` 的 SHARDS 表里）：

- `pytest_add_cli_args` 追加 `--timeout=30`：让**超时发生在 pytest 内部**（一个用例变红 = 杀死）；
- `timeout_multiplier = 60`：把外面那一刀推到远后面（≈ 3 分钟墙钟），把"谁先掐断"的顺序钉死。

30 秒是量出来的、不是拍的：本片选中的 3 个测试文件最慢单条实测 2.8 秒（`--durations=6`），
留了约 10 倍余量。**太紧会把"慢"误判成"红"，而误杀是虚高分数，比漏杀更难发现**——
这条上限只在"本片选中的测试都很快"这个前提下成立，换一片要重新量。

**效果（第五轮实测）**：`timeout` 由 5 条降到 **0 条**，分数 340/346 = 98.3% → **341/346 = 98.6%**。
那 5 条死循环型缺口现在会以"用例变红"的形式被杀掉，而不是被外面砍一刀记成"没跑完"。
（改 `timeout_multiplier` 会让 mutmut 把之前记成 `timeout` 的那些**重新评估**一遍——
`mutmut/configuration.py:274-275` 专门为这个键做了 cache 失效，所以这一轮不是拿缓存糊过去的。）

> 顺带一个容易看混的口径差异：**mutmut 自己的分数把 `timeout` 算作 killed**
> （`mutmut/__main__.py` 里 `score = (killed + timeout) / tested`），而本仓库的
> `scripts/mutation_gate.py` 把它算作**未杀**。我们用的是后者——保守的那一个。

---

## 六、变异测试照出来的真问题（本轮的价值都在这）

分数从 91.2% 涨到 96.4% 只是副产品。真正有用的是下面这些**被照出来的东西**：

### 1. `if os.name == "nt":` —— 平台分支断言的反模式（真缺陷）

`policy/policy.py` 的路径拆分按平台决定是否统一小写：

```python
if os.name == "nt":
    return tuple(p.casefold() for p in parts)
return parts
```

原来的测试写成"看当前平台再决定断言哪一边"：

```python
if os.name == "nt":
    assert _path_parts("C:/Users") == ("c:", "users")
else:
    assert _path_parts("C:/Users") == ("C:", "Users")
```

问题：**Linux CI 上永远只执行 `else` 分支**——"Windows 上大小写不敏感"这条安全语义
从来没有被任何一次 CI 验证过。变异测试直接把 `if os.name == "nt":` 改成
`if os.name == "XXntXX":` 和 `if os.name == "NT":`，两条都**存活**，
把这个盲区照了出来。（在 Windows 开发机上它反而会红，于是"本地绿、CI 绿"的假象更稳。）

改法：用 `monkeypatch` 注入平台名，两个平台的语义**都要断言**：

```python
import warden_agent.policy.policy as policy_mod

monkeypatch.setattr(policy_mod.os, "name", "nt")
assert _path_parts("C:/Users/ME") == _path_parts("c:/users/me")   # Windows：折叠大小写
monkeypatch.setattr(policy_mod.os, "name", "posix")
assert _path_parts("/Data") != _path_parts("/data")               # POSIX：敏感，不许折叠
```

改完之后这两条变异体立刻被杀死（`scripts/mutant_check.py` 里标着 `KILL`，
跑一遍就能看到它们变红）。**这是本轮唯一一处"实现没改、但行为终于被验证"的地方。**

### 2. "跑了，但没人检查返回值" —— ratelimit 从 0.0% 到 97.9%

这一片第一次跑的结果是 **21 个突变体全部存活**，而 mutmut 报的是 `no_tests=0`：
那些行**都被执行到了**，不是没覆盖。当时被放过去的例子：

| 变异 | 为什么之前没人发现 |
| --- | --- |
| 放行时返回的等待秒数 `0` → `1` | 用例只断言 `allow()`（取第一位），从不看 `check()` 的第二位 |
| 窗口剩余秒数从**减**改成**加** | 用例从不断言等待秒数 |
| 窗口起点参与运算（算出负数，被 `max(1, …)` 兜成 1） | 看起来"完全合理"，最难手工发现 |
| 参数下限 `<= 0` → `<= 1` | 现有用例都用 10/60，没有"刚好用 1"的调用 |
| `store=store` → `store=None` | 单副本下行为**一模一样**，只有"多副本时计数落到共享存储"会静默失效 |
| 报错文案被改成 `None` | 只断言"抛了 ValueError"，不看文案 |

补完断言后：**93/95 = 97.9%**，只剩 2 条等价变异（上节表格）。
教训：**"代码被执行过"和"结果被断言过"是两件事**，而覆盖率只看前者。

### 3. 基线测试自己的竞态（一条用例的毛病会让整片分数归零）

concurrency 分片第一次跑出 0/206 全是 "not checked"，根因是基线里这条断言有竞态：

```python
for _ in range(20):          # 等心跳跑一次
    if lease.lost:
        break
    time.sleep(0.05)
assert lease.lost is True
assert notified == ["lost"]  # ← 容器里撞到 assert [] == ['lost']
```

实现是**先置 `_lost`、再打点/记日志、最后才调回调**，中间那个窗口里 `lost` 已经是 `True`
而 `notified` 还是空。改成"等回调出现"（等到 `notified` 非空或超时）才是等对了东西。
顺带把 `locking.py` 里 `self._on_lost()` 用 `try/except` 包住并 `logger.exception`：
回调抛错原本只会打到 stderr，**丢锁通知会无声无息地不送达**。

### 4. 依赖清单漂移 / 换行符 —— 两条"环境踩坑"的工程教训

- **手写包清单会漂。** 容器里原本手写了一张依赖清单；第 10 批给本机加了 `pytest-timeout`，
  容器清单没跟上，而 `filterwarnings = ["error"]` 会把「Unknown config option: timeout」
  升级成 pytest 的 `INTERNALERROR` → 变异测试死在"收集统计"阶段（`runner returned 3`，
  看着像 mutmut 坏了）。现在容器统一 `uv sync --frozen`，容器里的依赖 = 仓库锁定的那一套。
- **CRLF 会让 Linux 侧脚本直接失败。** 仓库没有 `.gitattributes`，而 Windows 开发机
  `core.autocrlf=true`：一次 `git stash` / `git stash pop` 就把 215 个源码文件重检出成 CRLF，
  容器里 `bash scripts/mutation.sh` 当场报 `$'\r': command not found`。
  已加 `.gitattributes`（`* text=auto eol=lf`）并在本机关掉 `auto 转换`。
- **`--rm` 会把日志一起吃掉。** 容器跑挂了、`--rm` 已删掉容器，日志无处可查，
  只能重跑一遍才知道为什么失败。现在一律 `| tee mutants/*.log`。
- **Docker 引擎抖一下会从中间掐断长跑，而这一跑没有断点续跑。** 2026-10-01 下午实测：
  跑到 169/346 时 Docker Desktop 的后台引擎短暂掉了一次，日志末尾只有一行
  `level=error msg="error waiting for container: unexpected EOF"`，进程静默停止。
  更麻烦的是 `scripts/mutation.sh` 为了反假绿会在跑之前 `reset_mutation_state`，
  所以**重跑是从 0/346 重新开始**，前 169 条的战绩不会保留。这是刻意的权衡
  （宁可重跑，也不要拿上一片的数字交差），但要看住这一点：长跑前后都要看日志尾巴，
  不能只看“进程还在不在”。（容器被掐时会顺手把 `[tool.mutmut]` 恢复成默认分片，
  这一点由 `trap restore_config EXIT` 兜住。）
- **门禁脚本自己也是被门禁管的代码。** 本轮补测试时把 `tests/test_policy.py` 和
  `scripts/mutation_gate.py` 各写超了一行 100 列，直接把 CI 的 `quality` job 改红
  （`ruff check src tests scripts` 的 E501）。已修，并把这行加进日常自检：

```bash
./.venv/Scripts/python.exe -m ruff check src tests scripts && \
./.venv/Scripts/python.exe -m mypy src/warden_agent
```

---

## 七、诚实边界（这些必须写出来，否则上面的数字会被读得过于乐观）

1. **只有四个模块的分数，不是"整个仓库的测试有效性"。**
   其余模块仍只有行覆盖率支撑——那只是"跑过"，不是"被断言过"。
2. **`patch-guard` 已经清干净了，但"清干净"只指突变体层面。**
   这一片从 71.8% 一路到 **98.6%（341/346）**：19 条真缺口全部补了会变红的用例，
   剩下 5 条是实测证实的等价变异（清单见第五节），`timeout` 归零。四轮账见第四节。
   （`concurrency` 同样从 77.2% 清到 100.0%——过程见第四节的 47→0。）
   但有一条方法论教训不要忘：`concurrency` 那 13 条里有 3 条**曾经被裁决成"等价变异"**，
   `patch-guard` 里 `apply__mutmut_19` / `50` 也一样——断言一收紧就全被杀了。
   所以"等价"是个**会过期的结论**，不是永久免检章。
3. **等价变异的判断跟着实现走。** 实现了（比如把 `casefold` 换成 `lower`），
   裁决表就要重跑重写：`./.venv/Scripts/python.exe scripts/mutant_check.py`。
   脚本会打印"原文没匹配上（源码变了？）"并**以退出码 1 结束**，避免表格悄悄过期。
4. **Windows 语义是靠 `monkeypatch` 断言的，不是真机跑出来的。**
   它证明的是"这段逻辑在两个平台分支下都符合预期"，不是"在真实 NTFS 上端到端跑通了"。
5. **没接增量/差分变异。** 因此它跑夜间全量，而不是每个 PR——
   代价是"某个 PR 引入的存活突变体"最晚夜里才被发现。这是权衡，不是疏忽。
6. **变异分数不能反推代码质量。** 它只说明"已有的测试对已有实现有多敏感"。

---

## 八、复现命令（照抄可跑）

```bash
# 1. 跑某一分片（Windows 走容器；不指定 --shard 则用默认分片）
bash scripts/mutation.sh --docker --shard=ratelimit

# 2. 分值门禁：第一个数是该片门槛（取自 scripts/mutation_shards.py 的 SHARDS 表），
#    文件是刚跑完那一片写出的统计——两个分片不能混用
./.venv/Scripts/python.exe scripts/mutation_gate.py 95.9 mutants/mutmut-cicd-stats.json   # ratelimit
./.venv/Scripts/python.exe scripts/mutation_gate.py 96.6 mutants/mutmut-cicd-stats.json   # patch-guard

# 3. 逐条验证存活突变体的裁决（32 条：21 条该被杀 / 11 条等价）
#    退出码 0 才算"裁决表没过期"；源码改过而没同步表格时它会以 1 结束
./.venv/Scripts/python.exe scripts/mutant_check.py

# 3b. 万一上一步被打断（强杀 / OOM / 拔电源），源码会停在"改坏"的状态：
#     重跑会自动收拾残局；也可以手动还原（同一件事，退出码 0 = 收拾干净）
./.venv/Scripts/python.exe scripts/mutant_check.py --restore

# 4. 分片表与 pyproject 是否一致 + 门禁/分片脚本自身的单测
./.venv/Scripts/python.exe scripts/mutation_shards.py verify-config
./.venv/Scripts/python.exe -m pytest tests/test_mutation_gate.py tests/test_mutation_shards.py -q
```

容器里跑一片大约 10 分钟（宿主 `docker logs -f warden-mutmut-<片名>` 能看到进度，结束时
统计落在 `mutants/mutmut-cicd-stats.json`）。`mutants/` 下的清单与 diff 都是**产物**，
不进版本库。

---

## 九、这个脚本会**真的改源码**，所以它自带安全网

`--mutmut` 是在副本上跑，`scripts/mutant_check.py` 不一样：它把变异体**真写进工作树里的源码**，
只跑那一条对应的测试文件，然后靠 sha256 核对还原。三条规矩：

1. **改写前先备份、先落清单。** 原文（逐字节）进 `.mutant_check_backup/`，`manifest.json` 里记
   「备份名 + 改写后内容的 sha256」，而且清单是**先于**源码改写落盘的——被强杀也留得下收拾残局的凭据。
2. **每条都在 `finally` 里还原。** 测试红/绿/异常都会还回去；正常跑完连备份目录一起删掉。
   `Ctrl-C`、`SIGTERM` 有信号处理器兜（先还原再退出）。
3. **不可捕获的强杀（`TerminateProcess`、OOM、断电）救不了当场**，但下次运行会先"收拾残局"：
   只有当前内容与当初写坏的那份**逐字节相同**（sha256 对得上）才自动还原；对不上——
   说明这期间你手改过这个文件——它**拒绝动手**、打印备份在哪、以退出码 2 结束，
   **绝不拿旧备份覆盖你的新改动**。

退出码就是这套契约的对外接口：

| 退出码 | 含义 |
| --- | --- |
| `0` | 裁决表与源码同步：该杀的都杀了，等价变异仍是等价 |
| `1` | 表格过期（原文没匹配上）或出现缺口（该杀却没杀死） |
| `2` | 上次被强杀的残局需要人工处理（备份在 `.mutant_check_backup/`） |

两条踩过的坑，写在这里省下一次调试：

- **匹配前必须把 `\r\n` 归一成 `\n`。** 本仓库工作树里有 CRLF 文件，而 `CASES` 里的跨行原文是按
  `\n` 写的：不归一的话，一条跨行用例会静默变成 `SKIP`（看起来只是"表格过期"，其实是读法不对）。
  归一之后，落盘时按原文件的换行风格还原，逐字节一致。
- **"上次被强杀"和"表格过期"的症状会互相伪装。** 强杀留下的变异体会让那一条原文匹配不上，
  于是报出来的是 `SKIP`。所以脚本加了一条旁证：某条 `old` 匹配不上、而**它的 `new` 恰好匹配上**时，
  说明那处当前内容**正是变异体本身**——直接提示你跑 `--restore`（或 `git` 恢复那个文件）。
  真正被强杀杀过的那次就是这么认出来的。

这套安全网不是想出来的，是**跑出来发现的**：一次被外层超时强杀后，工作树里静静躺着一个变异体，
而当时的脚本没有任何痕迹可查。三个验证场景（强杀后重跑自动还原 / `--restore` / 手改过则拒动手）
都真跑过一遍。

