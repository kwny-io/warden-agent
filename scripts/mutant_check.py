"""手工变异核对：把某个变异**真的施加到源码上**，只跑对应测试文件，看测试是否变红。

背景：变异测试（`scripts/mutation.sh`）把源码改坏再跑测试——测试变红叫「杀死」，
仍然全绿叫「存活」。存活的突变体有两类，必须分开：

  · **等价变异**（本文件里标 EQUIV）：改了与没改行为完全一样，**杀不掉是正常的**，
    不是测试缺口。写在这里是为了后人不要再追一次（也不用为此补测试——补不出来）。
  · **真缺口**（标 KILL）：本该被测试抓住。这里把它们逐个施加回源码，
    期望测试变红；**仍然绿就是缺口**，得补测或改实现让它可测。

所以本文件是一张「存活突变体裁决表」：跑一遍，退出码非 0 就说明有缺口没堵。

跑法：`./.venv/Scripts/python.exe scripts/mutant_check.py`（约 1~2 分钟）
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

import os
import pathlib
import subprocess
import sys

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
# 同样，patch-guard 分片选了**三个**测试文件（mutmut 逐条突变体跑的就是这三个）。
T_PATCH = "tests/test_git.py tests/test_patch_path_guard.py tests/test_patch_parser.py"

# 被反复引用的长字符串拎出来，免得每行都超 100 列（ruff E501）
_PATH_DEFAULT = 'raw = str(arguments.get("path", ""))'
_OS_NAME = 'if os.name == "nt":'
_RANK = "{Decision.ALLOW: 0, Decision.ASK: 1, Decision.DENY: 2}"
_READ_OLD = 'abs_path.read_text(encoding="utf-8") if abs_path.exists() else ""'
_WRITE_NEW = 'abs_path.write_text("\\n".join(new_lines), encoding="utf-8", newline="\\n")'

# (期望, 理由, 文件, 原文, 变异, 测试文件)
CASES: list[tuple[str, str, str, str, str, str]] = [
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

# patch-guard 剩下的等价变异（**不补测试**，只留记录）：
#   · x__reject_unsafe_path__mutmut_17/18：路径段过滤集丢了 ""/"."（判据只问 `".." in segments`）；
#   · parse__mutmut_58：`cur_new = ""` → `None`（下游两处都是真值判断，None 与 "" 同假）；
#   · parse__mutmut_63/65：`(1 if cur_old else 0)` 恒真 / else 支不可达。
# 原先还有 apply__mutmut_19/50（codec 名 `utf-8` → `UTF-8`），它们**已经不属于等价**了：
# 上面那条 kwargs 契約用例把编码名当字串行断言，于是这两条也会变红（严格一点，无害）。


def main() -> int:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    rows: list[tuple[str, str, str]] = []
    for expect, label, rel, old, new, test_file in CASES:
        path = pathlib.Path(rel)
        orig = path.read_text(encoding="utf-8")
        if orig.count(old) != 1:
            rows.append(("SKIP", expect, label))
            continue
        path.write_text(orig.replace(old, new, 1), encoding="utf-8")
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", *test_file.split(), "-x", "-q",
                 "-p", "no:cacheprovider"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
            )
        finally:
            path.write_text(orig, encoding="utf-8")
        rows.append(("KILLED" if proc.returncode != 0 else "SURVIVED", expect, label))

    print("结果  期望   变异")
    for verdict, expect, label in rows:
        want = "KILLED" if expect == KILL else "SURVIVED"
        print(f"{'OK  ' if verdict == want else '!!  '}{verdict:<9}{expect:<6}{label}")

    skipped = [r for r in rows if r[0] == "SKIP"]
    gaps = [r for r in rows if r[0] == "SURVIVED" and r[1] == KILL]
    killed = sum(1 for r in rows if r[0] == "KILLED" and r[1] == KILL)
    equiv = sum(1 for r in rows if r[1] == EQUIV)
    print()
    print(f"共 {len(CASES)} 条：期望杀死且已杀死 {killed}，等价变异 {equiv}，跳过 {len(skipped)}")
    for r in gaps:
        print("  缺口（该补测试或让它可测）：", r[2])
    for r in skipped:
        print("  原文没匹配上（源码变了？）：", r[2])
    return 1 if (gaps or skipped) else 0


if __name__ == "__main__":
    raise SystemExit(main())
