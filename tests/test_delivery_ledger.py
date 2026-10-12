"""交付台账（`tests/DELIVERY_LEDGER.yaml`）的守卫。

现场与理由写在台账头部注释里，一句话版本：**「回执说它验的是哪棵树」以前只能靠人记得去核**。
第七十八批**第九处**就是这么交付出去的——交付面写着「本地冻结 `wa78/freeze78h.log`、
tree `e1ddf6a3…`」，而实际交付的 Z2 树是 `83891221…`（= `freeze78i.log`）：
语法正确、文件都在、引用的提交也都在，**任何文本守卫都抓不到**，
当时唯一的防线是「提交前逐句读 staged diff」。

这个文件把「已交付提交 ↔ 树」钉成可校验的东西。判据只有第 1 条是硬的，但它正好是出事的那条：

  1. **每一行的 `tree` 必须等于 `git rev-parse <sha>^{tree}`** —— 台账/回执指着另一棵树，当场红；
  2. `sha` 必须在本仓历史里、且必须是 `HEAD` 的祖先（不给别的分支或不存在的提交背书）；
  3. `sha` 不许重复；
  4. `attestation=git+frozen` ⇒ `shape`/`keys`/`receipt` 三件必须同时给；
     `attestation=git-only` ⇒ 三件必须都空（**不许自报数字**：没有仓库外回执的，就只能有 tree）；
  5. **回填规则**：必须有一行 `sha == HEAD^`、且**不得**有 `HEAD` 自己的行
     —— 提交没法在自己的正文里写下自己的树（写进去，树就变了），
     所以每个已交付提交由**下一个**提交背书。
     这条义务只对「**检出的提交就是被推送的那个提交**」适用，口径由**事件 + ref** 分派
     （`_refill_applies`，本文件唯一的口径判据）：`push` 到**交付面分支**（台账头部的
     `delivery_branch`，本仓 = `dev`）/ 本机 ⇒ 严格；`push` 到**非交付分支** ⇒ 回填义务不适用
     （被检出的是那条分支上的在飞提交，它不欠「由下一个交付提交背书」这个账；
     但**豁免只解掉回填这一条**，tree/sha/祖先/重复/自报数字照旧全部适用；
     **台账没写 `delivery_branch` 时不豁免任何分支** —— 把「没写」读成「都放行」就是静默放行）；
     `refs/tags/*` 检出与 `pull_request` 的**合并提交**检出 ⇒ 不适用
     （那两个父级都还没有「已交付提交」的身份），
     而且**不适用必须两边都对上：事件 + ref 形状**——PR 检出必须真的是合并提交（≥2 个父级）；
     发布 tag 检出的事件必须真的是 `workflow_dispatch`（本仓 `ci.yml` 的 `push` 只过筛 `branches`、
     不含 tag ⇒ 能产出「检出的是 tag」这条路的只有手动派发），否则报「口径被借用」；
  6. `run` **只核形状**（纯数字）：run 号在 GitHub 上，CI 里核不到——不假装核过，如实写在台账注释里。

口径分派不是凭空假设：`actions/checkout` 在 `pull_request` 事件下检出的**不是** PR 的头提交，
而是「PR 头合并进 base」的**合成合并提交**（父级 = `[base tip, PR head]`）。
本仓**一次 PR 运行都没有过**（`gh run list --workflow=ci.yml` 全是 `push`/`workflow_dispatch`；
这条命令谁都能跑，结论可复核）⇒ 这一路只有**本地模拟**证据，而那份模拟**住在仓库外**
（`wa79/prsim/`，不随仓库交付；`git ls-tree -r <HEAD> | grep -i prsim` 是空的）：
真建一个合并提交当检出、`GITHUB_EVENT_NAME=pull_request` 跑绿、同一个检出不放它则红。
**没有真 run，也没有仓内可核的证据**；写成「PR 上验过」就是过度声称。
`pull_request_target` **不在**豁免里（它的检出是 base ref、不是合并提交），本仓触发器也没有它——
将来若加上，按当时的检出形状补一条口径；现在的行为是**宁可红**，不静默放过。

「查不动」与「没问题」分开：git 不可用、或仓是**浅克隆**（历史对象不在本地）⇒ **报红**，不 skip。
浅克隆要 `fetch-depth: 0`（`.github/workflows/ci.yml` 的 `quality` job 已配，注释指向本文件）。
`git` 本身挂住（锁、凭据提示）⇒ `_GIT_TIMEOUT` 秒后当作「查不动」**报红**：挂到 job 的
`timeout-minutes` 再以别的名义结束，比报红更难查。

豁免**只看事件名是不够的**（外部复核给出的反例：本机 `GITHUB_EVENT_NAME=pull_request` +
任意一条自己合的合并提交就能免掉回填）⇒ 还要 **ref 形状对得上**：PR 必须
`refs/pull/<n>/merge|head`、发布 tag 必须 `refs/tags/v*`；对不上就退回严格口径并报「口径未登记」
（fail-closed，而不是静默放行）。

**采集缺口（本批实测，不是猜）**：`ci.yml` 有 `schedule`（每周一 03:00 UTC），而定时跑默认检出
**默认分支** `master`（`origin/HEAD -> origin/master`，tip `ad986393…`，在第七十九批 X 的时点
落后 `dev` **149** 个提交（这个数会随批次数长大 —— **别把它当判据**；要现算就
`git rev-list --count origin/master..dev`），
`git ls-tree -r origin/master` 里**没有**这份台账）⇒ 那条路上守卫根本没被采集（不是绿，是不存在）。
本批在 `quality` job 的 checkout 上钉住了 `ref`（`schedule` ⇒ `dev`），并按 YAML 接线加了用例。

**已知缺口（本批不修，留给下一批；反例由外部复核逐条给出）**：

  * `git+frozen` 行的 `receipt` 只核**文件名形状**，不核回执**存在**、更不核「回执里的跑后 tree ==
    这一行的 tree」⇒ 反例：写一个不存在的 `freeze999.log` 仍是绿；有回执的行改标 `git-only`
    就能把错树洗掉（**第九处的实质仍在**）。根因是回执住在仓库外、CI 看不见，
    修法（下一批）：把回执副本搬进交付树 + 记 sha256，让「回执指的树」与「交付树」同处一棵树。
  * 交付面**散文**里的 tree 字面量不在判据内（第九处真正坏的就是那句散文）。实测量过：
    `CHANGELOG.md:392/970/974` 与 `docs/复核索引.md` 共 **16 处**，形态三种（`tree <40hex>`、
    `tree <8hex>`、`树哈希 <40hex>`），其中 2 处是**冻结中途树**（对象在，但任何提交都没有那棵树：
    实测 `git rev-list --all` 命中为空）⇒ 判据不能简单要求「每个字面量都能在某个提交里找到」；
    要正式覆盖它，得先把冻结回执变成仓内可核的东西（见上面那条缺口）。
    （这句原来写的是「仓库里现在没有 `tree <40hex>` 字面量」——**是假的**，第二轮只读复核抓到：
    我只用了一种写法 grep 就下了「没有」的结论，换形态立刻是 16 处。教训记在桌面 77 号文档。）
  * `sha` 必须写 **40 位全 sha**：短 sha 即使唯一解析，也可能指到与本意不同的提交
    （`rev-parse --disambiguate` 只挡「现在就有多个对象共用这个前缀」这一种情况）。
    歧义检查与 40 位形状检查**两条都留**。
  * `yaml.safe_load` 对重复的 `rows:` 键静默取后者（没做重复键检测）。

判据有牙由本文件自己的用例证明（不是「读代码看着对」）：`test_判据有牙_*` 拿**真台账**在内存里改坏
一处，断言违反项里出现对应的那一条。改的是副本，不动磁盘；所以牙齿留在仓里、随 CI 每次跑一遍。
"""

from __future__ import annotations

import copy
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
LEDGER = REPO / "tests" / "DELIVERY_LEDGER.yaml"
CI_YML = REPO / ".github" / "workflows" / "ci.yml"

_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX16 = re.compile(r"^[0-9a-f]{16}$")
_ATTS = ("git-only", "git+frozen")
_PR_REF = re.compile(r"^refs/pull/\d+/(merge|head)$")
_TAG_REF = re.compile(r"^refs/tags/v[0-9]")  # 只有发布 tag（`v*`）会进这个工作流
_HEAD_REF = re.compile(r"^refs/heads/(.+)$")  # 分支检出：只有**交付面分支**承担回填义务
_GIT_TIMEOUT = 120  # 秒：git 挂住不是「没问题」，是「查不动」


class GitUnusable(RuntimeError):
    """查不动（git 不可用 / 浅克隆）——必须与「没问题」分开。"""


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(REPO), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=_GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        # 挂住（锁 / 凭据提示 / 网络）不许拖到 job 的 timeout-minutes 再以别的名义结束：
        # 那时红的原因与台账无关，查起来更贵。
        raise GitUnusable(
            f"`git {' '.join(args)}` 超过 {_GIT_TIMEOUT}s 没返回 ⇒ 查不动（不是「没问题」）"
        ) from None


def _usability_error(*, shallow: bool, head_ok: bool) -> str | None:
    """纯函数：把「这个仓能不能核台账」这件事写成可测的判据
    （放在这里是为了让用例能证明它不静默）。"""
    if shallow:
        return (
            "本仓是**浅克隆**：历史对象不在本地 ⇒ 交付台账（tests/DELIVERY_LEDGER.yaml）核不动。"
            "该 job 的 actions/checkout 需要 `fetch-depth: 0`。"
        )
    if not head_ok:
        return "拿不到 HEAD ⇒ 回填规则核不动。"
    return None


def _require_usable_git() -> None:
    probe = _git("rev-parse", "--is-shallow-repository")
    if probe.returncode != 0:
        detail = (probe.stderr or probe.stdout).strip()
        raise GitUnusable(f"git 不可用（rc={probe.returncode}）：{detail}")
    head = _git("rev-parse", "HEAD")
    message = _usability_error(
        shallow=probe.stdout.strip() == "true",
        head_ok=head.returncode == 0 and bool(head.stdout.strip()),
    )
    if message is not None:
        raise GitUnusable(message)


def _resolve(sha: str) -> str | None:
    out = _git("rev-parse", "--verify", f"{sha}^{{commit}}")
    return out.stdout.strip() if out.returncode == 0 else None


def _tree_of(sha: str) -> str | None:
    out = _git("rev-parse", f"{sha}^{{tree}}")
    return out.stdout.strip() if out.returncode == 0 else None


def _load_ledger() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """台账的（头部字段, 行）。头部字段里参与判据的只有 `delivery_branch`。"""
    raw = yaml.safe_load(LEDGER.read_text(encoding="utf-8"))
    # 输入校验用 `raise` 而不是 `assert`：`python -O` 会把 `assert` 整条剥掉，
    # 那时「空台账会是一个绿灯」这句承诺就悄悄失效了（外部复核指出）。
    if not isinstance(raw, dict):
        raise ValueError(f"台账顶层必须是 mapping（`rows:` 起头），拿到 {type(raw).__name__}")
    rows = raw.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("台账 `rows:` 必须是非空列表（空台账会是一个绿灯）")
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"每行必须是 mapping：{row!r}")
    return raw, rows


def _load_rows() -> list[dict[str, Any]]:
    return _load_ledger()[1]


def _delivery_branches() -> tuple[str, ...]:
    """台账声明的**交付面分支**（`delivery_branch: dev`，也接受列表）。

    **空 = 不豁免任何分支**：把「没写」读成「都放行」就是静默放行（fail-closed），
    所以调用方拿到 () 时必须继续严格，而不是反过来。
    """
    meta, _rows = _load_ledger()
    declared = meta.get("delivery_branch")
    if declared is None:
        return ()
    if isinstance(declared, str):
        return (declared.strip(),) if declared.strip() else ()
    if isinstance(declared, list):
        return tuple(str(item).strip() for item in declared if str(item).strip())
    raise ValueError(f"`delivery_branch` 只能是字符串或列表（拿到 {type(declared).__name__}）")


def _is_ancestor(sha: str, head: str) -> bool:
    """`merge-base --is-ancestor`：**rc==0 是祖先、rc==1 不是、rc>1 是「查不动」**。

    混在一起（`returncode != 0` 一律当成「不是祖先」）会把「未知 rev / 对象坏」渲染成
    「不是 HEAD 的祖先」这种**假红**——假红多了就没人信这个门禁（外部复核指出）。
    """
    rc = _git("merge-base", "--is-ancestor", sha, head).returncode
    if rc == 0:
        return True
    if rc == 1:
        return False
    raise GitUnusable(
        f"`git merge-base --is-ancestor {sha} {head}` rc={rc} ⇒ 查不动（不是「不是祖先」）"
    )


def _ambiguous_prefixes(prefix: str) -> int:
    """仓里有几个对象以这个 sha 前缀开头（>1 ⇒ 短 sha 有歧义，迟早静默指到别的提交）。"""
    return len(_git("rev-parse", f"--disambiguate={prefix}").stdout.split())


def _refill_applies(
    *,
    event: str | None,
    ref: str | None,
    parent_count: int,
    delivery: tuple[str, ...] = (),
) -> tuple[bool, str | None]:
    """回填义务**只对「检出的提交就是被推送的那个提交」适用**。

    返回 `(义务是否适用, 不适用时的自证违反项)`。这不是「读起来舒服」的抽象，是两处真实假阳的修法：

      * `refs/tags/v*` 检出：HEAD 是发布提交、不是 dev 的 tip ⇒ 严格口径会误报
        （父级那一行还在 dev 的下一次
        push 上）；
      * `pull_request` 检出：HEAD 是 GitHub 合成的合并提交（`[base tip, PR head]`）⇒ 两个父级都没有
        「已交付提交」的身份，`HEAD^` 那一行**结构上不可能存在**。
        若不放行，**将来的每一个 PR 都会红**，
        而红的原因与 PR 的改动无关（假阳比假阴更贵）。
        放行的代价是必须自证：PR 检出必须是**合并提交**
        （≥2 父级），否则视为「口径被借用」——本机/分支检出不许借 PR 口径跳过回填。

    其余一律严格（含 `event is None` 的本机运行与 `push`）：本机跑的就是 dev 的线性历史，没得商量。
    **豁免必须两边都对上（事件 + ref 形状）**：只看事件名的话，本机 `GITHUB_EVENT_NAME=pull_request`
    + 一条自己合的合并提交就能免掉回填——那是减速带，不是证明（外部复核的反例）。
    """
    if event is None:
        # 本机：**不认任何豁免**。豁免描述的是「CI 检出了一个什么形状」，本机随手设个环境
        # 变量就能骗过去⇒那不能当依据。（真在发布 tag 上本机跑，会因「HEAD 自己有行」报红：
        # 这是那道判据的代价，写得出来就已经比「记得去读 diff」强。）
        return True, None
    if _TAG_REF.match(ref or ""):
        # 发布 tag 豁免得先**自证检出形状**：事件必须是手动派发。
        # 本仓 `ci.yml` 的 `push` 只过筛 `branches`、不含 tag ⇒ 能产出
        # 「检出的是 tag」这条路的只有 `workflow_dispatch`
        # （`test_口径分派_本仓的检出形状都认得` 直接读 `ci.yml` 钉住这条前提）。
        # 不核事件的话：本机设个 `GITHUB_REF=refs/tags/v1.0.0`（配 `GITHUB_EVENT_NAME=push`）
        # 就能借走 tag 口径跳过回填——与本文件「豁免必须两边都对上」的话直接冲突。
        if event != _DISPATCH:
            return True, (
                f"`refs/tags/*` 口径被借用：ref 是发布 tag 形状（{ref!r}），但事件是 {event!r}"
                f"—— 只有 `{_DISPATCH}`（手动派发选 tag）才会检出 tag（本仓 `push` 不含 tag）"
            )
        return False, None
    if (ref or "").startswith("refs/tags/"):
        # tag 口径只对**发布 tag**（`refs/tags/v*`）开放：其它 tag 形状不是这个工作流会产生的检出
        return True, (
            f"`refs/tags/*` 口径未登记：{ref!r} 不是发布 tag（`refs/tags/v*`）⇒ 不许借它跳过回填"
        )
    branch = _HEAD_REF.match(ref or "") if event == "push" else None
    if branch:
        if branch.group(1) in delivery:
            return True, None          # 交付面分支：严格 —— 回填义务本来就是为它写的
        if not delivery:
            # 台账没声明交付面分支 ⇒ **不豁免任何分支**（fail-closed）：
            # 把「没写」读成「都放行」，等于给一条静默放行的后门。
            return True, None
        # 非交付分支：被检出的是那条分支上的在飞提交，它不欠「由下一个交付提交背书」这个账。
        # 今天本仓只推 `dev`，所以这条路只在「真开功能分支/PR 流程」时才走到；
        # 那时候硬撑严格口径，红的原因与被检出的改动无关 ⇒ 假阳 ⇒ 门禁变成噪音（比假阴更贵）。
        # 真实几何的实测复现：`wa79/fb-repro.py`（仓外，只见测一次）。
        # ⚠️ 豁免**只**解掉回填这一条：内容判据（tree/sha/祖先/重复/自报数字）照旧全部适用。
        # ⚠️ 这条口径**只对 `push` 开放**（假阳也只发生在 push 上）：`pull_request` / `schedule` /
        # 手动派发 / 本机带着分支形状的 ref，一律往下走原有分派（fail-closed）——
        # 否则「伪造 PR 事件 + 分支 ref」那条被借用口径的路径会静默变成绿。
        return False, None
    if event == "pull_request":
        if not _PR_REF.match(ref or ""):
            # fail-closed：只看事件名就能豁免 = 本机一条 `git merge --no-ff` + 一个环境变量
            return True, (
                f"`pull_request` 口径未登记：{ref!r} 不是 GitHub 的 PR 检出 ref"
                "（`refs/pull/<n>/merge|head`）⇒ 退回严格口径"
            )
        if parent_count < 2:
            return False, (
                "`pull_request` 口径被借用：GitHub 的 PR 检出是**合成的合并提交**（≥2 个父级），"
                f"现在 HEAD 只有 {parent_count} 个父级 ⇒"
                "这是本机/分支检出的形状，回填义务不许被跳过"
            )
        return False, None
    return True, None


def _refill_violations(
    *,
    event: str | None,
    ref: str | None,
    head: str,
    parents: tuple[str, ...],
    seen: dict[str, int],
    delivery: tuple[str, ...],
) -> list[str]:
    """回填规则（口径由 `_refill_applies` 分派）+「不许有 HEAD 自己的行」。"""
    out: list[str] = []
    applies, borrowed = _refill_applies(
        event=event, ref=ref, parent_count=len(parents), delivery=delivery
    )
    if borrowed is not None:
        out.append(borrowed)
    if head in seen:
        out.append(
            f"台账里有 HEAD（{head[:7]}）自己的行：提交没法在自己的正文里写下自己的树"
            "（写进去，树就变了）⇒ 这一行必是假的"
        )
    if applies:
        if not parents:
            out.append("拿不到 HEAD^ ⇒ 回填规则核不动（仓库只有一个提交？）")
        elif parents[0] not in seen:
            out.append(
                f"台账里没有 HEAD^（{parents[0][:7]}）那一行："
                "**每个已交付提交必须由下一个提交背书**，"
                "删掉它这里就红——这是逼人回填的机制"
            )
    return out


def _violations(
    rows: list[dict[str, Any]],
    *,
    head: str,
    parents: tuple[str, ...],
    event: str | None = None,
    ref: str | None = None,
    delivery: tuple[str, ...] | None = None,
) -> list[str]:
    """台账的全部判据。返回违反项（空列表 = 干净）。

    `event`/`ref` 是**入参**而不是从环境里现读：用例必须能钉住口径
    （CI 里这套用例也会在 PR 事件下跑）。
    `delivery` 是交付面分支（台账头部 `delivery_branch`）；**默认 `()` = 不豁免任何分支**
    （fail-closed：忘了传参也不会静默放行）。`_violations` 会显式传台账里声明的值。
    """
    if delivery is None:
        delivery = _delivery_branches()
    out: list[str] = []
    seen: dict[str, int] = {}

    for index, row in enumerate(rows, start=1):
        sha = str(row.get("sha") or "").strip()
        tag = f"第{index}行（{sha or '缺 sha'}）"
        if not sha:
            out.append(f"{tag}：缺 `sha`")
            continue
        full = _resolve(sha)
        if full is None:
            out.append(
                f"{tag}：这个 sha 不在本仓历史里"
                f"（`git rev-parse --verify {sha}^{{commit}}` 失败）"
            )
            continue
        if not _HEX40.match(sha):
            out.append(
                f"{tag}：`sha` 不是 40 位小写十六进制（{sha!r}）⇒ 短 sha 即使唯一解析，"
                "也可能指到与你本意不同的提交（`--disambiguate` 只挡「现在就有歧义」）"
            )

        if full in seen:
            out.append(f"{tag}：`sha` 与第{seen[full]}行重复（同一个提交被写成两条交付）")
        else:
            seen[full] = index

        tree = str(row.get("tree") or "").strip()
        if not _HEX40.match(tree):
            out.append(f"{tag}：`tree` 不是 40 位小写十六进制（{tree!r}）")
        else:
            real = _tree_of(sha)
            if real is None:
                out.append(f"{tag}：拿不到该提交的树")
            elif real != tree:
                # ↓ 第七十八批第九处的现场就长这样：台账说一棵树，git 里是另一棵
                out.append(
                    f"{tag}：`tree` 与 `git rev-parse {sha}^{{tree}}` 不一致："
                    f"台账 {tree} / git {real}"
                )

        if not _is_ancestor(full, head):
            out.append(f"{tag}：不是 HEAD 的祖先（不许给别的分支/游离提交背书）")

        ambiguous = _ambiguous_prefixes(sha)
        if ambiguous > 1:
            out.append(
                f"{tag}：这个 sha 前缀有歧义（仓里有 {ambiguous} 个对象以它开头）⇒"
                "现在指对了不代表以后指得对，写 40 位全 sha"
            )

        att = str(row.get("attestation") or "").strip()
        shape = str(row.get("shape") or "").strip()
        keys = str(row.get("keys") or "").strip()
        receipt = str(row.get("receipt") or "").strip()
        if att == "git+frozen":
            if not _HEX16.match(shape):
                out.append(f"{tag}：`git+frozen` 但 `shape` 不是 16 位小写十六进制（{shape!r}）")
            if not _HEX16.match(keys):
                out.append(f"{tag}：`git+frozen` 但 `keys` 不是 16 位小写十六进制（{keys!r}）")
            if not receipt:
                out.append(f"{tag}：`git+frozen` 必须给出仓库外回执的文件名（`receipt`）")
            elif (
                "/" in receipt
                or "\\" in receipt
                or not receipt.startswith("freeze")
                or not receipt.endswith(".log")
            ):
                out.append(
                    f"{tag}：`receipt` 必须是仓库外回执的**裸文件名** `freezeNN*.log`"
                    f"（{receipt!r}）"
                )
        elif att == "git-only":
            for name, value in (("shape", shape), ("keys", keys), ("receipt", receipt)):
                if value:
                    out.append(f"{tag}：`git-only` 不许写 `{name}`（没有仓库外回执就不许自报数字）")
        else:
            out.append(f"{tag}：`attestation` 只能是 {'/'.join(_ATTS)}（现在是 {att!r}）")

        run = str(row.get("run") or "").strip()
        if run and not run.isdigit():
            out.append(f"{tag}：`run` 只能是纯数字（这里只核形状，真伪在 GitHub 上，CI 核不到）")

    out.extend(
        _refill_violations(
            event=event, ref=ref, head=head, parents=parents, seen=seen, delivery=delivery
        )
    )
    return out


def _parents_of(rev: str) -> tuple[str, ...]:
    """**全部**父级（`git rev-list --parents -n 1`）：合并提交有 2 个，PR 口径的自证就靠这个数。"""
    out = _git("rev-list", "--parents", "-n", "1", rev)
    return tuple(out.stdout.split()[1:]) if out.returncode == 0 else ()


def _context() -> tuple[str, tuple[str, ...], str | None, str | None]:
    _require_usable_git()
    head = _git("rev-parse", "HEAD").stdout.strip()
    return (
        head,
        _parents_of("HEAD"),
        (os.environ.get("GITHUB_EVENT_NAME") or None),
        (os.environ.get("GITHUB_REF") or None),
    )


def _current() -> list[str]:
    head, parents, event, ref = _context()
    return _violations(_load_rows(), head=head, parents=parents, event=event, ref=ref)


# ---------------------------------------------------------------- 判据本身

def test_交付台账干净() -> None:
    """一条硬判据 + 五条一致性判据全过。红了先看 `HEAD^` 那一行回填了没有。"""
    assert _current() == []


# ----------------------------------------------- 交付面分支（口径的第三条路：非交付分支）

def _wip_geometry() -> tuple[str, tuple[str, ...]]:
    """「父级还没被回填」的检出形状 —— 功能分支上就是它。

    拿本仓 tip 顶替「那条分支上还没回填的在飞提交」，并把 `parents` 也指回它：
    于是「台账里没有 HEAD^ 那一行」恰好成立，而且**不用造提交对象**（CI 里也跑得动）。
    真实几何（真造一个 WIP 提交）的实测：`wa79/fb-repro.py`（仓外）。
    """
    head = _git("rev-parse", "HEAD").stdout.strip()
    assert head not in {r.get("sha") for r in _load_rows()}, "本仓 tip 不该有台账行（回填规则）"
    return head, (head,)


def test_非交付分支不承担回填义务() -> None:
    """推功能分支不该红：被检出的是那条分支上的在飞提交，它不欠这个账。"""
    head, parents = _wip_geometry()
    for ref in ("refs/heads/feat-x", "refs/heads/wip-试验"):
        got = _violations(_load_rows(), head=head, parents=parents, event="push", ref=ref)
        assert not any("HEAD^" in item for item in got), (ref, got)


def test_交付分支必须承担回填义务() -> None:
    """同一个检出形状，只要 ref 是交付面分支（`dev`）就必须红 —— 豁免不许漏到交付面上。"""
    head, parents = _wip_geometry()
    got = _violations(_load_rows(), head=head, parents=parents, event="push", ref="refs/heads/dev")
    assert any("HEAD^" in item for item in got), got


def test_非交付分支仍然要过内容判据() -> None:
    """豁免**只**解掉回填这一条：功能分支上写错 tree 照样红（否则就是一条静默放行的后门）。"""
    head, parents = _wip_geometry()
    rows = copy.deepcopy(_load_rows())
    rows[0]["tree"] = "0" * 40
    got = _violations(rows, head=head, parents=parents, event="push", ref="refs/heads/feat-x")
    assert any("不一致" in item for item in got), got


def test_分支豁免只对_push_开放() -> None:
    """删掉 `event == "push"` 这个门槛，就是给了一条**静默放行**的后门。

    例：`event="pull_request"` + `ref="refs/heads/feat-x"` + `delivery=("dev",)`——事件名说这是 PR、
    ref 却是分支形状。分支豁免**必须不生效**：回落原有分派后 `_PR_REF` 不认它 ⇒ 退回严格口径，
    并给出「口径被借用」的理由。否则「伪造一个事件名 + 一条分支 ref」就能把回填义务整条关掉。
    """
    applies, borrowed = _refill_applies(
        event="pull_request", ref="refs/heads/feat-x", parent_count=1, delivery=("dev",)
    )
    assert applies, "伪造 PR 事件不该借走分支豁免"
    assert borrowed is not None and "pull_request" in borrowed, borrowed
    head, parents = _wip_geometry()
    got = _violations(
        _load_rows(),
        head=head,
        parents=parents,
        event="pull_request",
        ref="refs/heads/feat-x",
        delivery=("dev",),
    )
    assert any("HEAD^" in item for item in got), got


def test_台账没声明交付分支时一律严格() -> None:
    """`delivery=()`（= 台账没写 `delivery_branch`）⇒ 不豁免任何分支：缺声明不是放行证。

    连**不传参**（走默认值）也必须严格 —— 将来有人加了新调用点却忘了传 `delivery`，
    那也必须是 fail-closed，而不是静默把回填义务关掉。
    """
    head, parents = _wip_geometry()
    got = _violations(
        _load_rows(), head=head, parents=parents, event="push", ref="refs/heads/feat-x", delivery=()
    )
    assert any("HEAD^" in item for item in got), got
    assert _refill_applies(event="push", ref="refs/heads/feat-x", parent_count=1) == (True, None)


def test_本仓台账声明了交付分支且就是_dev() -> None:
    """交付面是**声明**出来的，不是猜的：改路线时改这一行（它同时是参赛提交清单那件事的答案）。"""
    assert _delivery_branches() == ("dev",)


def test_怪_ref_一律严格() -> None:
    """形状认不出来（不是 `refs/heads/<名字>`）⇒ 退回严格口径，fail-closed。"""
    head, parents = _wip_geometry()
    for ref in ("refs/heads/", "refs/heads", "weird", ""):
        got = _violations(_load_rows(), head=head, parents=parents, event="push", ref=ref)
        assert any("HEAD^" in item for item in got), (ref, got)


def _quality_steps() -> list[dict]:
    """按 YAML 解析 `ci.yml` 的 `quality` job 步骤。

    **不查子串**：变异矩阵 M10 抓到过这条用例的假绿——注释里也写着 `fetch-depth: 0`，
    把真正的设置删掉、注释留着，查子串的版本照样绿。
    """
    doc = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    steps = (doc.get("jobs") or {}).get("quality", {}).get("steps")
    if not isinstance(steps, list) or not steps:  # 同上：不用 `assert`（`-O` 会剥掉）
        raise ValueError(f"ci.yml 里没有 quality job 的步骤（拿到 {steps!r}）")
    return steps


def test_ci_给_quality_job_配了_fetch_depth_0() -> None:
    """守卫要读历史对象：浅克隆会让它「查不动」。配置 disappearing 也要有东西拦一下。"""
    steps = _quality_steps()
    checkouts = [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@")]
    assert checkouts, "quality job 里没有 checkout 步骤 ⇒ 后面什么都核不了"
    with_ = checkouts[0].get("with") or {}
    depth = with_.get("fetch-depth")
    assert depth in (0, "0"), f"checkout 没配 `fetch-depth: 0`（拿到 {depth!r}）⇒ 浅克隆下核不动"
    assert isinstance(depth, int), "`fetch-depth` 写数字（`0`）而不是字符串：两种都能跑，但别让人猜"


# ------------------------------------------------- 判据有牙（改内存里的副本，不动磁盘）

def _mutated() -> tuple[list[dict[str, Any]], str, tuple[str, ...]]:
    head, parents, _event, _ref = _context()
    return copy.deepcopy(_load_rows()), head, parents


def _violations_strict(
    rows: list[dict[str, Any]], head: str, parents: tuple[str, ...]
) -> list[str]:
    """牙齿用例一律钉住**严格口径**（`push` + `refs/heads/dev`）：**不许读环境**，
    CI 里这套用例也会在 `pull_request` 事件下跑，那时环境变量是 PR 口径。"""
    return _violations(rows, head=head, parents=parents, event="push", ref="refs/heads/dev")


def test_判据有牙_错树会被抓() -> None:
    """**第九处的现场**：把 Z2 的 tree 换成被作废那次冻结（78h）的树。"""
    rows, head, parents = _mutated()
    rows[3]["tree"] = "e1ddf6a3a411106c9c07f2008f851b6adfb8469b"
    violations = _violations_strict(rows, head, parents)
    assert any("不一致" in item and "e1ddf6a3" in item for item in violations), violations


def test_判据有牙_不存在的_sha_会被抓() -> None:
    rows, head, parents = _mutated()
    rows[0]["sha"] = "deadbee"
    violations = _violations_strict(rows, head, parents)
    assert any("不在本仓历史里" in item for item in violations), violations


def test_判据有牙_删掉_HEAD_上一行会被抓() -> None:
    rows, head, parents = _mutated()
    assert parents, "本仓 HEAD 必须有父级（否则回填规则核不动）"
    rows = [row for row in rows if _resolve(str(row["sha"])) != parents[0]]
    violations = _violations_strict(rows, head, parents)
    assert any("HEAD^" in item and "背书" in item for item in violations), violations


def test_判据有牙_写上_HEAD_自己的行会被抓() -> None:
    rows, head, parents = _mutated()
    rows.append(
        {
            "sha": head,
            "tree": _tree_of(head),
            "attestation": "git-only",
            "shape": None,
            "keys": None,
            "receipt": None,
            "run": None,
            "note": "(伪造)",
        }
    )
    violations = _violations_strict(rows, head, parents)
    assert any("HEAD" in item and "必是假的" in item for item in violations), violations


def test_判据有牙_git_only_自报数字会被抓() -> None:
    """没有仓库外回执的行，不许顺手写一个看起来很像的 shape。"""
    rows, head, parents = _mutated()
    rows[0]["shape"] = "2a0407185b5fc4a0"
    violations = _violations_strict(rows, head, parents)
    assert any("不许写 `shape`" in item for item in violations), violations


def test_判据有牙_git_frozen_缺件会被抓() -> None:
    rows, head, parents = _mutated()
    rows[2]["keys"] = None
    rows[2]["receipt"] = "wa78/freeze78g.log"  # 带路径也不行：仓库外的东西不许写成路径
    violations = _violations_strict(rows, head, parents)
    assert any("`keys` 不是" in item for item in violations), violations
    assert any("裸文件名" in item for item in violations), violations


def test_判据有牙_attestation_写错会被抓() -> None:
    rows, head, parents = _mutated()
    rows[2]["attestation"] = "maybe"
    violations = _violations_strict(rows, head, parents)
    assert any("`attestation` 只能是" in item for item in violations), violations


def test_判据有牙_重复_sha_会被抓() -> None:
    rows, head, parents = _mutated()
    rows.append(copy.deepcopy(rows[-1]))
    violations = _violations_strict(rows, head, parents)
    assert any("重复" in item for item in violations), violations


def test_判据有牙_非数字_run_会被抓() -> None:
    rows, head, parents = _mutated()
    rows[3]["run"] = "run-38112192015"
    violations = _violations_strict(rows, head, parents)
    assert any("纯数字" in item for item in violations), violations


def test_判据有牙_shape与keys_非十六进制会被抓() -> None:
    rows, head, parents = _mutated()
    rows[2]["shape"] = "2a0407185b5fc4a"  # 15 位
    rows[2]["keys"] = "2876849183F3A2C6"  # 大写
    violations = _violations_strict(rows, head, parents)
    assert any("`shape` 不是" in item for item in violations), violations
    assert any("`keys` 不是" in item for item in violations), violations


def test_判据有牙_不是_HEAD_祖先的行会被抓() -> None:
    """拿台账里**最早**那一行当 HEAD：它后面的行都不是它的祖先 ⇒ 必须被抓。

    这条判据现实里挡的是「给别的分支/游离提交背书」。本仓没有非祖先提交可用，
    所以就换个方向造形状：拿一个更早的**真**提交当 HEAD（不是编一个 sha）。
    """
    rows, _head, _parents = _mutated()
    early = _resolve(str(rows[0]["sha"]))
    assert early is not None, "台账第一行必须是本仓真提交"
    violations = _violations_strict(rows, early, (early,))
    assert any("不是 HEAD 的祖先" in item for item in violations), violations


def test_查不动必须报红_不许静默放过() -> None:
    """浅克隆 / 拿不到 HEAD：与「没问题」分开（否则守卫失效时也是一个绿灯）。"""
    shallow = _usability_error(shallow=True, head_ok=True)
    assert shallow is not None and "fetch-depth" in shallow
    assert _usability_error(shallow=False, head_ok=False) is not None
    assert _usability_error(shallow=False, head_ok=True) is None


def test_真仓可用性探测在本机是通的(monkeypatch: pytest.MonkeyPatch) -> None:
    """本机/CI 正常情况下不该走到 GitUnusable；走到了说明环境有问题（例如浅克隆）。

    光断言「HEAD 是 40hex」的话，把 `_require_usable_git` 掏空成 `pass` 也照样绿（外部复核指出）
    ⇒ 这里补一条真会红的对照：把 `git rev-parse --is-shallow-repository` 伪造成 `true`，
    探测必须抛 `GitUnusable`（浅克隆＝「查不动」，不是「没问题」）。
    """
    _require_usable_git()
    head, parents, _event, _ref = _context()
    assert _HEX40.match(head), head
    assert all(_HEX40.match(item) for item in parents), parents

    real = _git

    def fake(*args: str) -> subprocess.CompletedProcess[str]:
        if args[:2] == ("rev-parse", "--is-shallow-repository"):
            return subprocess.CompletedProcess(args, 0, "true\n", "")
        return real(*args)

    monkeypatch.setattr(sys.modules[__name__], "_git", fake)
    with pytest.raises(GitUnusable):
        _require_usable_git()


def test_ci_定时跑要钉住交付分支() -> None:
    """`schedule` 默认检出**默认分支**（本仓 `master` 落后 `dev` 149 个提交、且没有这份台账）
    ⇒ 不钉住的话，周一那一跑既测的是旧树、又根本没采集到这台台账守卫（本批实测）。"""
    steps = _quality_steps()
    checkouts = [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@")]
    assert checkouts, "quality job 里没有 checkout 步骤"
    ref = (checkouts[0].get("with") or {}).get("ref")
    assert isinstance(ref, str), f"checkout 没钉 `ref`（拿到 {ref!r}）⇒ 定时跑会检到 master 上"
    # **不查子串**（本文件自己的口径）：`assert "dev" in ref` 连 `'not-dev-broken'` 都放得过，
    # 把 `|| github.sha` 换到前面也放得过（外部复核指出）⇒ 直接把那个条件表达式解出来。
    m = re.fullmatch(
        r"\$\{\{\s*github\.event_name\s*==\s*'(?P<event>[^']+)'\s*"
        r"&&\s*'(?P<branch>[^']+)'\s*\|\|\s*github\.sha\s*\}\}",
        ref.strip(),
    )
    assert m, (
        f"`ref` 得写成「事件等于某值时钉某分支、否则用 github.sha」这一种形状（拿到 {ref!r}）"
        "—— 换别的写法（先把 github.sha 摆前面、或钉到另一个名字里含 `dev` 的分支）就保不住定时跑"
    )
    assert m["event"] == "schedule", f"`ref` 里判的不是 `schedule`（拿到 {m['event']!r}）"
    assert m["branch"] == "dev", f"`ref` 没钉在交付分支 `dev` 上（拿到 {m['branch']!r}）"


def test_判据有牙_PR_口径未登记会被抓() -> None:
    """只看事件名就能豁免：本机设个 `GITHUB_EVENT_NAME=pull_request` + 自己合一条就跳过回填。"""
    rows, head, _parents = _mutated()
    rows = [row for row in rows if _resolve(str(row["sha"])) != head]
    bad = _violations(rows, head=head, parents=("0" * 40, "1" * 40), event=_PR, ref=_DEV)
    assert any("口径未登记" in item for item in bad), bad


def test_判据有牙_tag口径被借用会被抓() -> None:
    """`refs/tags/*` 也要**两边都对上**：`push` 事件 + 一个 tag 形状的 ref ≠ 「检出的是 tag」。

    不核事件的话，本机设个 `GITHUB_REF=refs/tags/v1.0.0` 就能借走 tag 口径跳过回填
    （外部复核在最终树上抓到的假绿）。
    """
    rows, head, _parents = _mutated()
    rows = [row for row in rows if _resolve(str(row["sha"])) != head]
    bad = _violations(rows, head=head, parents=("0" * 40,), event="push", ref=_TAGS)
    assert any("口径被借用" in item for item in bad), bad


def test_判据有牙_非发布tag_借用tag口径会被抓() -> None:
    rows, head, _parents = _mutated()
    rows = [row for row in rows if _resolve(str(row["sha"])) != head]
    bad = _violations(
        rows, head=head, parents=("0" * 40,), event=_DISPATCH, ref="refs/tags/nightly"
    )
    assert any("口径未登记" in item for item in bad), bad


def test_浅克隆必须真的报红_不是只证明纯函数(monkeypatch: pytest.MonkeyPatch) -> None:
    """外部复核：只测 `_usability_error` 的话，把 `_require_usable_git` 里那句 raise 删了仍是绿。"""

    def fake(*args: str) -> subprocess.CompletedProcess[str]:
        if args == ("rev-parse", "--is-shallow-repository"):
            return subprocess.CompletedProcess(["git", *args], 0, "true\n", "")
        return subprocess.CompletedProcess(["git", *args], 0, f"{'a' * 40}\n", "")

    monkeypatch.setattr(sys.modules[__name__], "_git", fake)
    with pytest.raises(GitUnusable) as reason:
        _require_usable_git()
    assert "fetch-depth" in str(reason.value)


def test_查不动_不许渲染成_不是祖先(monkeypatch: pytest.MonkeyPatch) -> None:
    """rc>1（未知 rev / 对象坏）不是「不是祖先」：那是假红，久了没人信这个门禁。"""

    def fake(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["git", *args], 128, "", "fatal: bad object")

    monkeypatch.setattr(sys.modules[__name__], "_git", fake)
    with pytest.raises(GitUnusable) as reason:
        _is_ancestor("0" * 40, "1" * 40)
    assert "查不动" in str(reason.value)


def test_判据有牙_短sha有歧义会被抓(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--disambiguate` 只要返回 >1 个对象就报：现在指对了不代表以后指得对。"""
    rows, head, _parents = _mutated()
    real = _git

    def fake(*args: str) -> subprocess.CompletedProcess[str]:
        if args[:1] == ("rev-parse",) and args[1].startswith("--disambiguate="):
            return subprocess.CompletedProcess(["git", *args], 0, f"{'a' * 40}\n{'b' * 40}\n", "")
        return real(*args)

    monkeypatch.setattr(sys.modules[__name__], "_git", fake)
    bad = _violations(rows, head=head, parents=("0" * 40,), event="push", ref=_DEV)
    assert any("歧义" in item for item in bad), bad


# ------------------------------------------------- 口径分派（回填义务在哪些检出上适用）

# 用例里的口径字面量只写一处，免得每一行都长到 E501
_PR = "pull_request"
_PR_MERGE = "refs/pull/7/merge"
_TAGS = "refs/tags/v0.1.0"
_DEV = "refs/heads/dev"
_DISPATCH = "workflow_dispatch"


def test_口径分派_pull_request_必须自证是合并提交() -> None:
    """PR 检出豁免回填义务，代价是必须证明检出真的是 GitHub 合成的合并提交（≥2 父级）。"""
    applies, borrowed = _refill_applies(event=_PR, ref=_PR_MERGE, parent_count=1)
    assert applies is False and borrowed is not None
    assert "口径被借用" in borrowed
    applies, borrowed = _refill_applies(event=_PR, ref=_PR_MERGE, parent_count=2)
    assert applies is False and borrowed is None


def test_口径分派_标签与分支与目标分支() -> None:
    assert _refill_applies(event=_DISPATCH, ref=_TAGS, parent_count=1) == (False, None)
    assert _refill_applies(event=_DISPATCH, ref=_DEV, parent_count=1) == (True, None)
    assert _refill_applies(event=None, ref=None, parent_count=1) == (True, None)  # 本机：严格
    # push 一个合并提交：照样严格（它的第一个父级就是已交付提交）
    assert _refill_applies(event="push", ref=_DEV, parent_count=2) == (True, None)
    assert _refill_applies(event="pull_request_target", ref=_DEV, parent_count=1) == (True, None)


def test_判据有牙_PR_口径被借用会被抓() -> None:
    """只有 1 个父级的检出声称自己是 PR 检出 ⇒ 红（本机/分支检出不许借 PR 口径）。"""
    rows, head, _parents = _mutated()
    violations = _violations(rows, head=head, parents=(head,), event=_PR, ref=_PR_MERGE)
    assert any("口径被借用" in item for item in violations), violations


def test_PR_检出豁免回填义务_严格口径同一个检出就要红() -> None:
    """**对照**：同一份台账 + 同一组父级（都不在台账里）——PR 口径绿、严格口径红，分派才有意义。

    台账先滤掉 HEAD 自己的行（假如处于「已写好回填行、还没提交」的窗口）——那条在本文件另有判据。
    """
    rows, head, _parents = _mutated()
    rows = [row for row in rows if _resolve(str(row["sha"])) != head]
    fabricated = ("0" * 40, "1" * 40)
    assert _violations(rows, head=head, parents=fabricated, event=_PR, ref=_PR_MERGE) == []
    strict = _violations(rows, head=head, parents=fabricated, event="push", ref=_DEV)
    assert any("HEAD^" in item and "背书" in item for item in strict), strict


def test_标签检出豁免回填义务_分支检出同一个形状就要红() -> None:
    rows, head, _parents = _mutated()
    rows = [row for row in rows if _resolve(str(row["sha"])) != head]
    fabricated = ("0" * 40,)
    assert _violations(rows, head=head, parents=fabricated, event=_DISPATCH, ref=_TAGS) == []
    strict = _violations(rows, head=head, parents=fabricated, event=_DISPATCH, ref=_DEV)
    assert any("HEAD^" in item for item in strict), strict


def test_口径分派_本仓的检出形状都认得() -> None:
    """这条是把上面那些口径字面量**钉在本仓真实触发器上**：本仓有 `push`/`pull_request`/
    `workflow_dispatch`/`schedule`，而这些检出形状里只有 PR 的合并提交与发布 tag 会被豁免。
    用例本身是在问「我给的那些 ref 字面量，真的都是 GitHub 会产生的形状吗」。

    只断言正则与自己文件里的常量匹配是**自指**：改 `ci.yml` 触发器它不会变（外部复核指出）
    ⇒ 这里真的去读 `ci.yml` 的 `on:`，把两条前提钉住：
      * `push` 只过筛 `branches`、**不含 tag** ⇒ 「检出是发布 tag」只可能来自手动派发
        （这正是 `_refill_applies` 里 tag 豁免的自证）；
      * 触发器里**没有** `pull_request_target`（本仓不豁免它，宁可红）。
    """
    assert _PR_REF.match(_PR_MERGE), _PR_MERGE
    assert _PR_REF.match("refs/pull/123/head"), "GitHub 也产生 `refs/pull/<n>/head`"
    assert not _PR_REF.match(_DEV)
    assert _TAG_REF.match(_TAGS), _TAGS
    assert not _TAG_REF.match("refs/tags/nightly")
    # 本机（`event is None`）无论 ref 长什么样都必须严格，且**一条违反项都不许多报**。
    # 断言整元组而不是 `[0] is True`：后者只钉住「义务是否适用」一个分量，把
    # `_refill_applies` 里 `if event is None:` 那行改坏**观测不到差异**（矩阵 G10 实测 0 红）
    # ⇒ 本机带着 tag/PR 形状的 `GITHUB_REF` 会凭空多一条「借用口径」的
    # 违反项（假阳）而没人拦。
    assert _refill_applies(event=None, ref=_TAGS, parent_count=9) == (True, None)
    assert _refill_applies(event=None, ref=_PR_MERGE, parent_count=9) == (True, None)
    assert _refill_applies(event=None, ref=None, parent_count=1) == (True, None)

    doc = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    # YAML 1.1 里裸 `on:` 会被解析成**布尔 True**（PyYAML 的经典坑）⇒ 两种键都取。
    triggers = doc.get("on") or doc.get(True)
    assert isinstance(triggers, dict), f"ci.yml 的 `on:` 没解析出来（拿到 {triggers!r}）"
    for event in ("push", "pull_request", "workflow_dispatch", "schedule"):
        assert event in triggers, (
            f"本仓触发器里没有 `{event}` 了？口径分派（`_refill_applies`）与台账头部的话要跟着改"
        )
    push = triggers.get("push") or {}
    assert "branches" in push, "本仓的 `push` 是按分支过筛的"
    assert "tags" not in push, (
        "`push` 一旦加上 `tags:`，「检出的是 tag」就可能来自 push ⇒ tag 豁免的自证"
        "（事件必须是手动派发）不再成立，守卫与台账的话要一起改"
    )
    assert "pull_request_target" not in triggers, (
        "触发器加了 `pull_request_target`：它的检出是 base ref、不是合并提交 ⇒"
        "「宁可红、不静默放过」这条口径要重新评审（现在故意不豁免它）"
    )
