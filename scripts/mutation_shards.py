"""变异测试的**分片表**（唯一来源）与 `pyproject.toml` 里 `[tool.mutmut]` 的渲染器。

为什么要有这个东西
------------------
变异测试（mutation testing）要回答的问题是："把这段代码改错，会不会有测试变红？"
它必须**逐个突变体**跑一遍测试，所以成本 = 突变体数量 × 每组测试的耗时。

原来只圈定一个组合（三个安全关键模块 + 三个测试文件），于是：
  * 想扩到新模块就得改 pyproject，且一次只能跑一片；
  * CI 里只能是"串行的一大坨"，慢到只能夜间跑；
  * 没有任何机制防止"文件改名了、路径写错了、门禁悄悄空转"。

这个脚本把"哪一片、变谁、跑哪些测试、门槛多少"变成**一张表**，并且：
  * `render <片名>` 把 `[tool.mutmut]` 重写成该片（CI 的 matrix 每个 job 跑自己的片 → 真并行）；
  * `verify-config` 检查 pyproject 里的现状与表一致（**禁止表格与配置各说各话**）；
  * `changed` 按 git 改动只挑出受影响的分片（本地与增量场景不用跑全部分片）；
  * `matrix` 输出 GitHub Actions 的 matrix JSON（省得在 yml 里再抄一份名单）。

退出码：0 = 一致/正常；1 = 与表不一致（配置漂移）；2 = 用法错或文件不存在。
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import json
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"


@dataclass(frozen=True)
class Shard:
    """一个变异测试分片。"""

    name: str
    title: str
    # 要变异的实现文件（相对仓库根）
    modules: tuple[str, ...]
    # 每行：模块路径 → 为什么它值得被变异（"写错的后果是什么"）
    why: tuple[str, ...]
    # 只跑这些测试文件。口径：**它们必须盖住该片模块「被任何测试覆盖过的行」**——
    # 因为 `mutate_only_covered_lines = true`，没被这些文件跑到的行根本不会被变异，
    # 于是"分数看着还行"其实是变异面被静默缩小了（比分数低危险得多）。
    # 用 `scripts/mutation_shards.py suggest-tests` 机械算出最小集合（贪心集合覆盖），
    # 别靠手挑；`check-tests` 会核对当前表里这些文件的实际覆盖率（低于 99% 就红）。
    tests: tuple[str, ...]
    # 门槛（实测基线 − 2 个点）。None = 尚未实测：本片只报告分数、不判红（见 docs/mutation.md）
    min_score: float | None
    # 实测分数（%）。与 min_score 必须同时有/同时无，且门槛必须**低于**基线、又不能低于太多
    # （前者会让门禁天天红、后者会让门禁形同虚设）——这两条由 tests/test_mutation_shards.py 按住。
    baseline: float | None
    # 实测基线的一句话说明（写进 pyproject 的注释里，让人知道门槛从哪来）
    baseline_note: str
    # 额外塞给 pytest 的命令行参数（除固定的 `-x -q -p no:cacheprovider` 之外）。
    # 目前只有 patch-guard 用：加 `--timeout=<秒>` 让**挂住**的突变体在 pytest 内部就变红。
    # 为什么必须这样：mutmut 从外面把进程掐掉时只会记成 `timeout`，而门禁口径里
    # `timeout` 算「未杀」——于是“把 `idx += 1` 改成 `idx -= 1`”这种**死循环**突变体
    # 永远显示不出来，真实缺口被“超时”这个标签盖住了。
    pytest_extra_cli_args: tuple[str, ...] = ()
    # 上面那些额外参数的一行依据（要写清楚上限是怎么定出来的、为什么不会把“慢”误判成“红”）
    pytest_cli_note: str = ""
    # mutmut 自己的**外部**超时倍数（默认 15.0）。它给每条突变体的上限 =
    #   (基线耗时 + timeout_constant) × 倍数（再取一半做墙钟）。
    # 为什么可能要抬：默认值可能先于上面 pytest 的 `--timeout` 触发，那一次只被记成
    #   `timeout`——按门禁口径算「未杀」，于是“会挂住”的突变体在分数里永远显示不出来。
    # 抬高它不改变“谁能杀死突变体”，只是把“谁先掐断”的顺序钉死。
    mutmut_timeout_multiplier: float | None = None


SHARDS: tuple[Shard, ...] = (
    Shard(
        name="core-security",
        title="脱敏 / 安全响应头 / 审批决策",
        modules=(
            "src/warden_agent/core/redaction.py",
            "src/warden_agent/web/security_headers.py",
            "src/warden_agent/policy/policy.py",
        ),
        why=(
            "脱敏：漏一个分支就是明文密钥进日志/事件（CWE-532）",
            "CSP 与安全响应头：写错等于浏览器侧防护静默失效",
            "审批决策：ASK/DENY 判错就是越权执行",
        ),
        tests=(
            "tests/test_redaction.py",
            "tests/test_security_headers.py",
            "tests/test_policy.py",
        ),
        min_score=94.4,
        baseline=96.4,
        baseline_note="killed=242 / total=251，2026-09-29 容器内 Linux",
    ),
    Shard(
        name="concurrency",
        title="运行锁与租约（并发正确性）",
        modules=("src/warden_agent/runtime/locking.py",),
        why=(
            "运行锁/租约：判断写错就是「同一个 run 被两个进程同时执行」（重复扣费、重复改仓库）",
        ),
        tests=(
            # suggest-tests 算出：这两个文件盖住 locking.py 全部 99 行被覆盖行（100.0%）
            "tests/test_run_lock.py",
            "tests/test_run_lease.py",
        ),
        # 2026-09-30 第二轮：把 13 条存活逐条打回源码验证（13/13 全被杀、源码 sha256 还原一致）
        # 后重跑，193/206=93.7% → **206/206=100.0%**。门槛跟着抬到 98：这是最容易
        # "静默并发驱动"的一片（两个副本抢同一个 run，不报错只串数据），宁可让改动先红一次，
        # 也不要再退回"写错了但测试全绿"。
        min_score=98.0,
        baseline=100.0,
        baseline_note=(
            "实测 2026-09-30 容器内 Linux：206/206 = 100.0%（存活 0）。"
            "抬门槛的证据：那 13 条存活逐条打回源码后都被新断言杀死（等价变异 0 条）；"
            "复现命令见 docs/mutation.md「复现与逐条裁决」"
        ),
    ),
    Shard(
        name="ratelimit",
        title="限流",
        modules=("src/warden_agent/web/ratelimit.py",),
        why=("限流：窗口/计数写错要么被绕过（打爆后端），要么把正常用户挡在外面",),
        tests=("tests/test_ratelimit.py",),
        min_score=95.9,
        baseline=97.9,
        baseline_note=(
            "实测 2026-09-30：93/95 = 97.9%。存活 2 个经逐条裁决**均为等价变异**"
            "（partition→rpartition；报错文案加前后缀）——复现脚本 scripts/mutant_check.py"
        ),
    ),
    Shard(
        name="patch-guard",
        title="补丁路径守卫（目录穿越）",
        modules=("src/warden_agent/git/patch.py",),
        why=("补丁落盘前的路径校验：漏一处就是「模型/攻击者把文件写到仓库外面去」",),
        tests=(
            # suggest-tests 算出：test_git.py 一个就盖住 114/119 行，加上专用守卫用例凑满 100%
            "tests/test_git.py",
            "tests/test_patch_path_guard.py",
            "tests/test_patch_parser.py",
        ),
        # 曾经是四个分片里**最差**的一片（存活 80/291）。它同时也是最危险的一片
        # （漏一处就是文件被写到仓库外面），所以缺口清单在 docs/mutation.md 里单独立了一节。
        # 四轮重跑的账（同一口径，只有测试与超时配置在变；逐轮记录见 docs/mutation.md 第四节）：
        #   2026-09-30 补上 tests/test_patch_parser.py：209/291=71.8% → 287/340=84.4%
        #   2026-09-30 再把 parse 的分支/边界写成参数化用例（+++ 另起一行、两文件、CRLF、
        #     预算、锚点取值）→ 322/346=93.1%（分母也变大：分支补全后 mutmut 能进到更多分支）
        #   2026-10-01 把 24 条（19 存活 + 5 超时）逐条裁决 + 按裁决补测 → 340/346=98.3%
        #     （存活 5 + 超时 1；那 1 条是 `_apply_file` 里 `pos += 1` → `pos = 1` 的死循环）
        #   2026-10-01 给 pytest 加 `--timeout=30`、把 mutmut 的外层上限抬到 60 倍
        #     → **341/346 = 98.6%（存活 5、超时 0）**：死循环型缺口改成“pytest 内部变红”，
        #     不再被外面那一刀记成 timeout（口径与理由见 docs/mutation.md 第五节）
        min_score=96.6,
        baseline=98.6,
        baseline_note=(
            "实测 2026-10-01 容器内 Linux：341/346 = 98.6%（存活 5、超时 0）。"
            "这 5 条存活已逐条实测证实为**等价变异**（清单与理由见 docs/mutation.md 第五节），"
            "也就是说这一片的**可杀突变体已经全部杀死**；门槛按基线 - 2.0 记 96.6，"
            "留下的余量是给“换机器 / 换 Docker 后端”这类环境抖动用的"
        ),
        # 2026-10-01：把 24 条（存活 19 + 超时 5）**逐条实测裁决**完了（复现脚本
        # scripts/mutant_check.py + 容器内重跑）。裁决结果：7 条等价变异（写进
        # docs/mutation.md 第五节的裁决表）、17 条真缺口（每条补了一条会变红的用例；
        # 典型：CRLF 删除补丁的 `+++ /dev/null` 被当成无关行跳过 → 文件没被删却报“应用成功”）、
        # 其中 5 条“超时”是 `idx += 1` → `idx -= 1` 这类死循环改法（下面那行把它搬进 pytest）。
        #
        # 超时为什么必须搬进 pytest：mutmut 从外面把进程掐掉时只会记成 `timeout`，而门禁口径里
        # **超时算未杀**——于是死循环类突变体的真实缺口在分数里一直显示不出来。
        pytest_extra_cli_args=("--timeout=30",),
        pytest_cli_note=(
            "上限 30 秒的依据：本片选中的 3 个测试文件最慢单条实测 2.8 秒"
            "（pytest --durations=6，2026-10-01 本机），留了约 10 倍余量"
        ),
        mutmut_timeout_multiplier=60.0,
    ),
)

BY_NAME = {s.name: s for s in SHARDS}
DEFAULT_SHARD = "core-security"

_BLOCK_START = "[tool.mutmut]"
_BLOCK_TEMPLATE = '''[tool.mutmut]
# 注意：本段由 `scripts/mutation_shards.py` 生成（当前写入的是 `{name}` 分片：{title}）。
#    改配置请改那个脚本里的 SHARDS 表，再重新生成（换成别的分片就换最后一个参数）：
#        ./.venv/Scripts/python.exe scripts/mutation_shards.py render {name}
#    `tests/test_mutation_shards.py` 会检查"这段"与"表"一致；不一致直接红，
#    为的是不让"表格说一套、CI 跑另一套"这种最隐蔽的失真发生。
#
# 为什么分片：变异测试是**逐个突变体**跑测试的，一次全量测试约 5 分钟 ⇒ 串行整仓是小时级。
# 分片后每片只跑"覆盖到它"的那几个测试文件，于是可以并行（CI matrix），也可以只跑改动涉及的那片。
# 全部分片、各片门槛与实测基线见 `scripts/mutation_shards.py`；裁决表与口径见 `docs/mutation.md`。
source_paths = ["src"]
# 本片变异的实现（每条都是"写错就是安全事故或静默失效"的地方）：
{module_comments}only_mutate = [
{modules}
]
# 突变体只跑"覆盖到它"的那几个测试文件（mutmut 用 pytest 收集 + 覆盖信息做映射）；
# 不限定的话每个突变体都要跑整套测试，成本直接翻几十倍。
# 注意：往这些模块**加测试**就是"把变异测试的命门收窄"的正路。
pytest_add_cli_args_test_selection = [
{tests}
]
# 实测基线：{baseline}
{cli_note}pytest_add_cli_args = {cli_args}
{timeout_line}max_children = 4
# 只变异“被选中的测试跑过的行”：否则每条突变体都要跑整套测试，成本直接翻几十倍。
# 代价是**选窄了不会报错**，只会静默不再守着那些行——所以上面的 test_selection
# 必须盖住本片模块“被任何测试覆盖过”的所有行（scripts/mutation_shards.py 的 check-tests 按住这点）。
mutate_only_covered_lines = true
'''


def render_block(shard: Shard) -> str:
    """把这个分片渲染成 pyproject 里的 `[tool.mutmut]` 段落文本（以换行结尾）。"""
    why_by_module = dict(zip(shard.modules, shard.why, strict=False))
    comments = ""
    for module in shard.modules:
        reason = why_by_module.get(module, "")
        comments += f"#   {module.split('/')[-1]:<24} {reason}\n"
    if shard.baseline is None or shard.min_score is None:
        baseline = "尚未实测：本片只报告分数、不判红（见 docs/mutation.md 第七节）"
    else:
        baseline = (
            f"{shard.baseline:.1f}% → 门槛 {shard.min_score:.1f}%"
            f"（{shard.baseline_note}）"
        )
    cli_args = ["-x", "-q", "-p", "no:cacheprovider", *shard.pytest_extra_cli_args]
    timeout_line = ""
    if shard.mutmut_timeout_multiplier is not None:
        timeout_line = (
            "# mutmut 对每条突变体的两道**外部**上限（都随这里的倍数缩放）：\n"
            "#   墙钟：(基线耗时 + timeout_constant) × 倍数      [isolation.py:875-876]\n"
            "#   CPU ：ceil((基线耗时 + timeout_constant) × 倍数 × 2)  [__main__.py:1046]\n"
            "# 默认倍数 15 在慢机器上可能先于上面 pytest 的 `--timeout` 触发，\n"
            "# 而那一刀只被记成 `timeout`（门禁口径算「未杀」）。抬高它不改变\n"
            "# “谁能杀死突变体”，只把“谁先掐断”的顺序钉死：该变红的是 pytest，\n"
            "# 不是外面那一刀。\n"
            f"timeout_multiplier = {shard.mutmut_timeout_multiplier}\n"
        )
    cli_note = (
        "# `-x`：一个用例红了就停（\"杀死\"突变体要快）；"
        "`-p no:cacheprovider`：别写 .pytest_cache\n"
    )
    if shard.pytest_extra_cli_args:
        cli_note += (
            "# 为什么要额外加超时、而且要在 pytest 内部就掐断：实测数据（2026-10-01）——\n"
            "#   同一条突变体（`_apply_file` 里 `pos += 1` → `pos = 1` 造成的死循环）\n"
            "#   在外层上限还是默认倍数时只被记成 `timeout`，而门禁口径算「未杀」；\n"
            "#   在 pytest 内部掐断后它直接以红用例的形式被杀（官方重跑 `timeout` → 0）。\n"
            "#   上限不能拍：太紧会把『慢』误判成『红』，而误杀是**虚高**分数，\n"
            "#   比漏杀更难发现。\n"
            f"# {shard.pytest_cli_note}\n"
        )
    return _BLOCK_TEMPLATE.format(
        name=shard.name,
        title=shard.title,
        module_comments=comments,
        modules="\n".join(f'    "{m}",' for m in shard.modules),
        tests="\n".join(f'    "{t}",' for t in shard.tests),
        baseline=baseline,
        cli_note=cli_note,
        cli_args=json.dumps(cli_args),
        timeout_line=timeout_line,
    )


def split_pyproject(text: str) -> tuple[str, str, str]:
    """把 pyproject 切成 (mutmut 段之前, mutmut 段, 之后)。找不到起点抛 KeyError。"""
    lines = text.splitlines(keepends=True)
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == _BLOCK_START)
    except StopIteration as exc:  # pragma: no cover - 配置被删掉才会走到
        raise KeyError(f"pyproject.toml 里找不到 {_BLOCK_START}") from exc
    # 段落到"下一个顶层表头"或文件末尾为止
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if re.match(r"^\[[^\]]+\]", lines[i]):
            end = i
            break
    return "".join(lines[:start]), "".join(lines[start:end]), "".join(lines[end:])


def current_block() -> str:
    _, block, _ = split_pyproject(PYPROJECT.read_text(encoding="utf-8"))
    return block


def write_shard(shard: Shard) -> bool:
    """把 pyproject 的 mutmut 段重写为该分片。返回是否真的改了内容。"""
    text = PYPROJECT.read_text(encoding="utf-8")
    before, block, after = split_pyproject(text)
    wanted = render_block(shard)
    if block == wanted:
        return False
    PYPROJECT.write_text(before + wanted + after, encoding="utf-8")
    return True


# ---------------------------------------------------------------- 改动 → 分片


def shards_for_paths(paths: list[str]) -> set[str]:
    """给定一批改动的仓库相对路径，返回受影响的分片名。

    规则：改动命中分片的实现文件或它的测试文件 → 该片受影响。
    对"不在任何分片里的源码改动"（例如改了 store/ 或 server.py）**不**牵连所有分片——
    那些路径根本不参与变异，牵连只会让人白等；但调用方应把这类改动显式告诉人
    （`changed` 会单独列出，避免"看起来只影响一片"的假安全感）。
    """
    hit: set[str] = set()
    for path in paths:
        normalized = path.replace("\\", "/").strip()
        for shard in SHARDS:
            if normalized in shard.modules or normalized in shard.tests:
                hit.add(shard.name)
    return hit


def untracked_by_shards(paths: list[str]) -> list[str]:
    """改动里"不属于任何分片"的源码文件（提醒用）。"""
    all_tracked = {p for s in SHARDS for p in (*s.modules, *s.tests)}
    out = []
    for path in paths:
        normalized = path.replace("\\", "/").strip()
        if normalized in all_tracked:
            continue
        if normalized.startswith("src/") or normalized.startswith("tests/"):
            out.append(normalized)
    return out


def _unquote_git_path(raw: str) -> str:
    """把 git 的 C 风格引号路径还原成真实路径。

    `core.quotepath`（默认为 true）会把含非 ASCII 的路径写成
    `"docs/\\345\\244\\215.md"` 这样的 C 字符串——它**不等于**真实文件名，
    于是“按路径选分片”会静默地选不中：改了一个中文名源文件 → 分片没跑 →
    CI 绿着放过去。这正是本工具要防的那种假绿（“没跑”与“跑了且干净”长得一样）。

    两层防：① 调 git 时一律带 `-c core.quotepath=false`；② 这里再兜一层，
    因为别人的全局 gitconfig 或 `GIT_CONFIG_*` 环境变量能把它改回来。
    """
    if len(raw) < 2 or not (raw.startswith('"') and raw.endswith('"')):
        return raw
    simple = {
        "a": b"\a", "b": b"\b", "f": b"\f", "n": b"\n",
        "r": b"\r", "t": b"\t", "v": b"\v", "\\": b"\\", '"': b'"',
    }
    body = raw[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in simple:
                out += simple[nxt]
                i += 2
                continue
            octal = body[i + 1 : i + 4]
            if len(octal) == 3 and all(c in "01234567" for c in octal):
                out.append(int(octal, 8))
                i += 4
                continue
        out += ch.encode("utf-8")
        i += 1
    return out.decode("utf-8", errors="replace")


def git_changed_paths(base: str | None) -> list[str]:
    """工作区改动（相对 base；不给 base 就用 HEAD）+ 未跟踪文件。"""
    # `-c core.quotepath=false`：见 `_unquote_git_path`——默认的 C 引号化会
    # 让中文名路径匹配不上分片，而且是**静默**匹配不上。
    no_quote = ["-c", "core.quotepath=false"]
    args = ["git", *no_quote, "diff", "--name-only"]
    if base:
        args.append(base)
    args.append("--")
    diff = subprocess.run(
        args, cwd=ROOT, capture_output=True, text=True,
        # 文件名可能是非 UTF-8（本地化 Windows 上常见）：钉死编码 + replace，
        # 免得“列个改动文件”因为一个解码异常把整个分片跑挂掉。
        encoding="utf-8", errors="replace", check=False,
    )
    untracked = subprocess.run(
        ["git", *no_quote, "ls-files", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    paths = [_unquote_git_path(p) for p in diff.stdout.splitlines() if p.strip()]
    paths += [_unquote_git_path(p) for p in untracked.stdout.splitlines() if p.strip()]
    return sorted(set(paths))


# ---------------------------------------------------------------- CLI


def _cmd_list(_: argparse.Namespace) -> int:
    for shard in SHARDS:
        score = (
            "未实测（只报告不判红）"
            if shard.min_score is None
            else f"{shard.min_score}（基线 {shard.baseline}）"
        )
        print(f"{shard.name:<16} 门槛={score:<22} {len(shard.modules)} 个模块 / "
              f"{len(shard.tests)} 个测试文件  {shard.title}")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    shard = BY_NAME.get(args.shard)
    if shard is None:
        print(f"没有这个分片：{args.shard}（可选：{', '.join(BY_NAME)}）", file=sys.stderr)
        return 2
    print(render_block(shard), end="")
    return 0


def _cmd_render(args: argparse.Namespace) -> int:
    shard = BY_NAME.get(args.shard)
    if shard is None:
        print(f"没有这个分片：{args.shard}（可选：{', '.join(BY_NAME)}）", file=sys.stderr)
        return 2
    if args.check:
        ok = current_block() == render_block(shard)
        print("一致" if ok else "不一致（配置漂移）")
        return 0 if ok else 1
    changed = write_shard(shard)
    tail = "" if changed else "（本来就是它）"
    print(f"pyproject.toml 的 [tool.mutmut] -> {shard.name}{tail}")
    return 0


def _cmd_verify_config(_: argparse.Namespace) -> int:
    """检查现状是否等于"默认分片"的渲染结果（CI 与测试都靠它防漂移）。"""
    shard = BY_NAME[DEFAULT_SHARD]
    expected = render_block(shard)
    actual = current_block()
    if actual == expected:
        print(f"[tool.mutmut] 与分片表一致（{DEFAULT_SHARD}）")
        return 0
    print(f"[tool.mutmut] 与分片表**不一致**（应以 {DEFAULT_SHARD} 为准）", file=sys.stderr)
    import difflib

    for line in difflib.unified_diff(
        actual.splitlines(), expected.splitlines(), "现状", "期望", lineterm=""
    ):
        print(line, file=sys.stderr)
    return 1


def _cmd_changed(args: argparse.Namespace) -> int:
    paths = git_changed_paths(args.base)
    hit = shards_for_paths(paths)
    if args.only_affected:
        for name in [s.name for s in SHARDS if s.name in hit]:
            print(name)
        return 0
    print(f"改动文件 {len(paths)} 个；受影响分片：{', '.join(sorted(hit)) or '（无）'}")
    others = untracked_by_shards(paths)
    if others:
        print(f"另有 {len(others)} 个改动不在任何分片内"
              "（不影响变异结果，但请确认它们不需要新分片）：")
        for path in others[:20]:
            print(f"  {path}")
    if args.explain:
        for shard in SHARDS:
            if shard.name in hit:
                print(f"  {shard.name}: {shard.title}")
    return 0


def _cmd_matrix(_: argparse.Namespace) -> int:
    """GitHub Actions matrix 用的 JSON 数组（含各片门槛，省得在 yml 里再抄一份）。"""
    print(json.dumps([{"shard": s.name} for s in SHARDS], ensure_ascii=False))
    return 0


def _cmd_min_score(args: argparse.Namespace) -> int:
    """给 shell 用：打印门槛；未实测评测分片打印 none。"""
    shard = BY_NAME.get(args.shard)
    if shard is None:
        print(f"没有这个分片：{args.shard}", file=sys.stderr)
        return 2
    print("none" if shard.min_score is None else shard.min_score)
    return 0


# ------------------------------------------------- 测试文件 / 覆盖关系（分片表的证据）
#
# 分片表里最容易写错、错了最不容易被发现的，是「这一片该跑哪些测试文件」。
#
# 先把口径说清楚（`mutate_only_covered_lines = true` 的前提）：
#   * mutmut 只变异**被选中的测试执行过**的行 ⇒ 选得越窄，变异面越小（存活数看着少）；
#   * 反过来，**存活**的突变体一定是"选中的测试跑过那一行、却没人变红"——那是真的不敏感；
#   * 所以选窄了不会报错，只会**静默不再守着那些行**。这比"分数低"危险得多，必须用工具按住。
# 实测撞到过：`patch-guard` 只列了 `tests/test_patch_path_guard.py`，而
#   `src/warden_agent/git/patch.py` 另有 `tests/test_git.py`
#   与 `tests/test_coding_agent.py` 在覆盖它。
#
# 三个探子：
#   * `test_files_importing()`：静态看哪些测试文件**直接 import** 了这个模块（便宜，可当守卫）；
#   * `load_coverage_lines()`：读 coverage.py 的 SQLite，得到「实现文件 → 测试文件 → 执行过的行号」
#     （贵：要先跑一次带 `--cov-context=test` 的全量，所以只在维护分片表时手动跑）；
#   * `suggest_tests()`：贪心集合覆盖，算出「用最少的测试文件覆盖到这个模块所有被覆盖过的行」——
#     这是"把表写对"的机械做法，省得靠人拍脑袋挑文件。

MODULE_ROOT = "src/"
DEFAULT_COVERAGE_DB = ".coverage"


def _pct(part: int, whole: int) -> str:
    """覆盖率的可读形式；分母为 0 时算 100%（没有可覆盖的行，不算问题）。"""
    return "100.0%" if not whole else f"{100.0 * part / whole:.1f}%"


def _pct_float(part: int, whole: int) -> float:
    return 100.0 if not whole else 100.0 * part / whole


def module_name(rel_path: str) -> str:
    """`src/warden_agent/git/patch.py` → `warden_agent.git.patch`（包取到 `__init__` 上一级）。"""
    path = rel_path
    if path.startswith(MODULE_ROOT):
        path = path[len(MODULE_ROOT) :]
    if path.endswith(".py"):
        path = path[: -len(".py")]
    if path.endswith("/__init__"):
        path = path[: -len("/__init__")]
    return path.replace("/", ".")


def _imported_names(source: str) -> set[str]:
    """一个 .py 文件里所有 import 到的「完整名字」（含 `from a.b import c` 拼出的 a.b.c）。"""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if base:
                names.add(base)
            for alias in node.names:
                names.add(f"{base}.{alias.name}" if base else alias.name)
    return names


def test_files_importing(module: str) -> list[str]:
    """哪些测试文件**直接 import** 了这个实现模块（静态代理，是覆盖关系的下界）。"""
    found: list[str] = []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        try:
            names = _imported_names(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        if module in names or any(n.startswith(module + ".") for n in names):
            found.append(path.relative_to(ROOT).as_posix())
    return found


def _rel_src_path(path: str) -> str | None:
    """把 coverage 里记的绝对路径归一到仓库相对路径；不在 src/warden_agent 下返回 None。"""
    text = str(path).replace("\\", "/")
    index = text.find("src/warden_agent/")
    return text[index:] if index >= 0 else None


def _decode_numbits(blob: bytes) -> set[int]:
    """`line_bits.numbits` 是按行号位打包的二进制（第 1 行 = 第 0 位）。"""
    lines: set[int] = set()
    for index, byte in enumerate(blob):
        if not byte:
            continue
        for bit in range(8):
            if byte & (1 << bit):
                lines.add(index * 8 + bit + 1)
    return lines


def load_coverage_lines(db_path: Path) -> dict[str, dict[str, set[int]]]:
    """读 coverage.py 的 SQLite → {实现文件: {测试文件: {执行过的行号}}}。

    开分支覆盖（`has_arcs=1`）时数据在 `arc` 表（fromno/tono 成对，-1 是虚拟起止行），
    否则在 `line_bits` 表。两种都要认——本机与 CI 不一定开同一个开关。
    """
    conn = sqlite3.connect(db_path)
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta"))
        impl: dict[int, str] = {}
        for fid, path in conn.execute("SELECT id, path FROM file"):
            rel = _rel_src_path(path)
            if rel is not None:
                impl[int(fid)] = rel
        tests: dict[int, str] = {}
        for cid, ctx in conn.execute("SELECT id, context FROM context"):
            name = str(ctx).split("::")[0].replace("\\", "/")
            if name.startswith("tests/"):
                tests[int(cid)] = name

        out: dict[str, dict[str, set[int]]] = {}
        if meta.get("has_arcs") == "1":
            for fid, cid, start, end in conn.execute(
                "SELECT file_id, context_id, fromno, tono FROM arc"
            ):
                rel, test = impl.get(int(fid)), tests.get(int(cid))
                if rel is None or test is None:
                    continue
                bucket = out.setdefault(rel, {}).setdefault(test, set())
                for line in (start, end):
                    if line and int(line) > 0:
                        bucket.add(int(line))
        else:
            rows = conn.execute("SELECT file_id, context_id, numbits FROM line_bits")
            for fid, cid, blob in rows:
                rel, test = impl.get(int(fid)), tests.get(int(cid))
                if rel is None or test is None:
                    continue
                out.setdefault(rel, {}).setdefault(test, set()).update(_decode_numbits(blob))
        return out
    finally:
        conn.close()


def module_lines(lines: dict[str, dict[str, set[int]]], module: str) -> dict[str, set[int]]:
    """该模块的 {测试文件: 行号}，去掉空集合（只 import 到、没跑任何行的测试）。"""
    return {test: rows for test, rows in lines.get(module, {}).items() if rows}


def suggest_tests(
    lines: dict[str, dict[str, set[int]]], shard: Shard
) -> tuple[list[str], int, int]:
    """贪心集合覆盖：用最少的测试文件盖住该分片所有模块「被任何测试覆盖过的行」。

    返回 (测试文件列表, 已盖行数, 目标行数)。集合覆盖是 NP-hard，贪心不是最优解，
    但**足够好且可解释**：每次挑"还能多盖最多行"的那个文件。
    """
    goals: dict[str, set[int]] = {}
    for module in shard.modules:
        rows = module_lines(lines, module)
        goals[module] = set().union(*rows.values()) if rows else set()
    total = sum(len(goal) for goal in goals.values())
    candidates = sorted({t for m in shard.modules for t in module_lines(lines, m)})

    chosen: list[str] = []
    done: dict[str, set[int]] = {m: set() for m in shard.modules}
    while True:
        best, gain = "", 0
        for test in candidates:
            if test in chosen:
                continue
            new = sum(
                len(module_lines(lines, m).get(test, set()) - done[m]) for m in shard.modules
            )
            if new > gain:
                best, gain = test, new
        if not best or gain <= 0:
            break
        chosen.append(best)
        for module in shard.modules:
            done[module] |= module_lines(lines, module).get(best, set())
    return sorted(chosen), sum(len(v) for v in done.values()), total


def _load_lines_or_exit(db_path: Path) -> dict[str, dict[str, set[int]]] | None:
    if not db_path.is_file():
        print(
            f"找不到覆盖数据 {db_path}；先生成一次（约 5 分钟）：\n"
            "  ./.venv/Scripts/python.exe -m pytest -q --cov=warden_agent"
            " --cov-context=test --cov-report=",
            file=sys.stderr,
        )
        return None
    try:
        return load_coverage_lines(db_path)
    except (OSError, sqlite3.Error) as exc:
        print(f"读不出 {db_path}：{exc}", file=sys.stderr)
        return None


def _cmd_suggest_tests(args: argparse.Namespace) -> int:
    """打印"用哪些测试文件就能盖住这片模块的全部被覆盖行"（可直接粘回 SHARDS 表）。"""
    lines = _load_lines_or_exit(Path(args.data))
    if lines is None:
        return 3
    targets = [BY_NAME[args.shard]] if args.shard else list(SHARDS)
    exit_code = 0
    for shard in targets:
        chosen, got, total = suggest_tests(lines, shard)
        print(f"\n== 分片 {shard.name}（{shard.title}）")
        print(f"   目标：盖住 {total} 行（这些模块被任何测试覆盖过的行）")
        print(f"   建议：{len(chosen)} 个测试文件 → 盖住 {got} 行（{_pct(got, total)}）")
        if not chosen:
            print("   !! 一个测试都没覆盖到这些模块：分片表或模块路径写错了")
            exit_code = 1
        for test in chosen:
            covered = sum(len(module_lines(lines, m).get(test, set())) for m in shard.modules)
            print(f"     {covered:>5} 行  {test}")
        if sorted(chosen) != sorted(shard.tests):
            print("   当前表里写的是：" + ", ".join(shard.tests))
        print("   粘回 SHARDS 的形态：")
        for test in chosen:
            print(f'        "{test}",')
    return exit_code


def _cmd_check_tests(args: argparse.Namespace) -> int:
    """核对分片表里的测试文件：是否盖住了这些模块「被任何测试覆盖过的行」。

    这不是语法检查，而是防"静默削弱"：表里少写一个测试文件，`mutate_only_covered_lines`
    就会把那些行从变异面里**安静地拿掉**，分数反而可能更好看。
    """
    lines = _load_lines_or_exit(Path(args.data))
    if lines is None:
        return 3
    targets = [BY_NAME[args.shard]] if args.shard else list(SHARDS)
    bad = 0
    for shard in targets:
        print(f"\n== 分片 {shard.name}（{shard.title}）")
        for module in shard.modules:
            per_test = module_lines(lines, module)
            if not per_test:
                print(f"  !! {module}：没有任何测试覆盖到它")
                bad += 1
                continue
            all_rows = set().union(*per_test.values())
            mine: set[int] = set()
            for name in shard.tests:
                mine |= per_test.get(name, set())
            missed = all_rows - mine
            print(f"  {module}")
            ratio = _pct(len(mine), len(all_rows))
            print(f"    表里的测试覆盖 {len(mine)}/{len(all_rows)} 行（{ratio}）")
            if missed:
                owners = sorted(t for t, rows in per_test.items() if rows & missed)
                print(f"    !! 有 {len(missed)} 行没被表里的测试跑到；能跑到它们的是：")
                for test in owners[:10]:
                    print(f"       {test}")
                if len(owners) > 10:
                    print(f"       …另有 {len(owners) - 10} 个")
                if _pct_float(len(mine), len(all_rows)) < args.min_cover:
                    bad += 1
    if bad:
        print(
            f"\n有 {bad} 个模块的覆盖不足（低于 {args.min_cover}%）。"
            "跑 `suggest-tests` 拿到该补的测试文件列表。"
        )
        return 1
    print(f"\n所有分片表里的测试文件都盖住了 ≥{args.min_cover}% 的「被覆盖行」。")
    return 0


def _utf8_stdout() -> None:
    """把 stdout/stderr 兑成 UTF-8 + `errors="replace"`（纯打印动作不允许把命令搞崩）。

    为什么必须：Windows 上 stdout 被**管道重定向**时 Python 默认用 GBK(cp936) 编码，
    而本文件渲染出的文本里有 `⇒` 这类 GBK 里根本没有的符号——`show` 子命令实测直接
    `UnicodeEncodeError` 退出码 1（2026-10-01，`show patch-guard > 文件`）。
    仓库里 `src/warden_agent/rag/eval.py` 碰到同一个问题时用的也是这个写法。
    `errors="replace"` 是最后一道保险：宁可打问号，也不要因为一个字符把整条命令废掉。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # pragma: no cover - 被换成非 TextIOWrapper（pytest 捕获等）
            continue
        with contextlib.suppress(ValueError, OSError):  # pragma: no cover - 流已关闭/不支持重配
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _utf8_stdout()
    parser = argparse.ArgumentParser(description="变异测试分片表与 [tool.mutmut] 渲染器")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出所有分片").set_defaults(func=_cmd_list)

    p_show = sub.add_parser("show", help="打印某个分片渲染后的 pyproject 段落")
    p_show.add_argument("shard", nargs="?", default=DEFAULT_SHARD)
    p_show.set_defaults(func=_cmd_show)

    p_render = sub.add_parser("render", help="把 [tool.mutmut] 重写为指定分片")
    p_render.add_argument("shard", nargs="?", default=DEFAULT_SHARD)
    p_render.add_argument("--check", action="store_true", help="只检查，不改文件")
    p_render.set_defaults(func=_cmd_render)

    sub.add_parser("verify-config", help="检查 pyproject 与分片表是否一致").set_defaults(
        func=_cmd_verify_config
    )

    p_changed = sub.add_parser("changed", help="按 git 改动挑出受影响的分片")
    p_changed.add_argument("--base", default=None, help="与之比较的提交/分支（默认 HEAD）")
    p_changed.add_argument("--only-affected", action="store_true", help="只打印分片名")
    p_changed.add_argument("--explain", action="store_true", help="多打一行分片主题")
    p_changed.set_defaults(func=_cmd_changed)

    sub.add_parser("matrix", help="输出 CI matrix JSON").set_defaults(func=_cmd_matrix)

    p_score = sub.add_parser("min-score", help="打印某分片的门槛")
    p_score.add_argument("shard", nargs="?", default=DEFAULT_SHARD)
    p_score.set_defaults(func=_cmd_min_score)

    p_check = sub.add_parser(
        "check-tests",
        help="核对分片表里的测试文件是否盖住了这些模块的被覆盖行"
        "（需先有一次带 --cov-context 的全量）",
    )
    p_check.add_argument("shard", nargs="?", default=None)
    p_check.add_argument(
        "--data", default=DEFAULT_COVERAGE_DB, help="coverage.py 的 SQLite（默认 .coverage）"
    )
    p_check.add_argument("--min-cover", type=float, default=99.0, help="允许的最低覆盖率（%%）")
    p_check.set_defaults(func=_cmd_check_tests)

    p_suggest = sub.add_parser(
        "suggest-tests", help="算出「用最少的测试文件盖住这片模块的全部被覆盖行」（维护分片表用）"
    )
    p_suggest.add_argument("shard", nargs="?", default=None)
    p_suggest.add_argument(
        "--data", default=DEFAULT_COVERAGE_DB, help="coverage.py 的 SQLite（默认 .coverage）"
    )
    p_suggest.set_defaults(func=_cmd_suggest_tests)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
