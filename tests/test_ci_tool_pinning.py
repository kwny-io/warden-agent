"""门禁**自己执行**的外部镜像必须钉在 digest 上（且全仓只有一份台账）。

为什么要有这条：

- 交付面早就有这条纪律：`scripts/pin_image_digests.sh` 在发布时把 Dockerfile 的 `FROM`、
  `deploy/**/*.yaml` 的 `image:` 钉成 `<仓库>@sha256:…`（第三方镜像用 `--also postgres:16-alpine`
  显式带上），`tests/test_release_scripts.py` 守着它。
- 但**门禁自己执行的外部程序**（kubeconform / promtool / trivy / gitleaks / postgres service /
  变异容器里的解释器）一直写着浮动 tag。后果不是"版本新一点"，而是三件事：
  1) 今天绿 = 「某个版本的程序」绿；上游重推同一个 tag 就换成另一个程序，**没有任何记录**；
  2) 这些容器**挂着工作区运行**（trivy 还挂 `/var/run/docker.sock`）——浮动 tag 意味着
     **CI 里执行的是别人随时可以替换的代码**，门禁那句"绿"就不再证明任何东西；
  3) 最尖的一处是本仓自己的设计：`scripts/gitleaks_reverse_control.sh` 是"证明扫描器没被
     关掉"的反向对照——**它和门禁必须跑同一个程序**，否则这个反证是空的（两个 tag 各写各的，
     总有一天会对不上，而两边谁都不会报错）。
- 所以本守卫盯两件事：**被执行的**必须钉住；**被引用的**（注释、文档字符串里给人复制的手工
  命令）至少 tag 不能与台账漂移。**未登记的外部镜像直接红**（新镜像必须先写进本文件的台账）。

台账 digest 的来源（要可复核，不能用二手值）：本机 `docker images --digests` 记录的
RepoDigest，即 `docker pull <上游名>` 时上游 registry 给出的 manifest digest（**上游名**，
不是镜像站名），并且用 `docker run <仓库>:<tag>@sha256:<digest> …` **实跑**验证过
（kubeconform 返回 `v0.6.7`、gitleaks 返回 `v8.21.2`）。
镜像站的 digest **不能**当权威：2026-10-04 实测同一个 `postgres:16` 在上游名下是 `f1c337…`、
在 `docker.m.daocloud.io` 名下是 `a3b7f4…`——镜像站自己重签名了 manifest。

交付文档也在这条判据里（第七十三批加）：`.md` 的**代码围栏**是读者会照抄执行的命令——既然读者会执行
`docker run … ghcr.io/gitleaks/gitleaks:v8.21.2`，它就是执行面的一部分，**必须与门禁同一个程序**
（`docs/复核索引.md` 里那条 gitleaks 反向对照命令就是这么写出来的）。围栏之外的一律不算：
`CHANGELOG.md` 与 `docs/复核索引.md` 正文里满是“某天实测上游 digest 是 `f1c337…`”这类**观测记录**，
拿今天的台账去比对它们，等于要求把历史记录改假。

不钉什么（有意为之，不是图省事）：

- `Dockerfile` 的 `FROM node:20-slim` / `FROM python:3.12-slim`：本仓对**基镜像**的政策是拉新
  （ci.yml 里 `docker build --pull`；第四十四批为 libpcre2 的 CVE 显式升级过包；版本更新交给
  `.github/dependabot.yml`），钉住会违背它。所以：`FROM …` 行一律跳过；这类镜像单列
  `_BASE_IMAGE_BY_POLICY`，并且**不许出现在执行上下文里**（写 `docker run node:…` 会红）。
- `deploy/` 下的镜像（交付面的 app 镜像、`deploy/observability/docker-compose.yml` 的本地观测栈）：
  由发布流程的 `scripts/pin_image_digests.sh` 统一钉，是另一条轴。本守卫只扫"判定过程中被
  执行的引用"：`.github/**` + `scripts/**` + `tests/**`。

已知限制：钉住的 digest **不会自动更新**（dependabot 只覆盖 Dockerfile 基镜像与 GitHub
Actions，看不见 `docker run` 里的镜像）。这是**故意的**：门禁换一个程序必须是一次显式决定——
本守卫报红 → 人工取新 digest → 改台账 → 在 CHANGELOG 里写清为什么换。

另外三条已知边界（不是漏洞，是划出来的范围）：

- **`.md` 的正文散文不扫**（只扫代码围栏）。散文里的 `postgres:16` 是叙述，不是可复制的命令；
  把散文也扫进来，历史记录会天天误报，守卫就会被人学会忽略。
- **`CHANGELOG.md` 显式排除**，理由写在 `_MD_HISTORY` 里（排除必须带理由，不能是一个裸名字）。
- 围栏里的 YAML 片段**按行取 `image:`**，不引 YAML 解析器：文档里的片段常常不是合法文档、
  也可能是多文档（`deploy/k8s/namespace.yaml` 就是两段），用 `yaml.safe_load` 会直接抛错。
  代价是缩进奇怪、或键名被引号包起来的写法会漏——今天 10 个交付文档围栏里只有 1 处 `image:`，
  等它有实例再收紧。
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_SCAN_DIRS = (".github", "scripts", "tests")
_SKIP_NAMES = {"__pycache__", ".mypy_cache", ".pytest_cache"}
_FILE_SUFFIXES = {".py", ".sh", ".yml", ".yaml"}
# 本文件自己：里面必然有 digest 字面量与仓库名（台账），不能拿它当"引用"来判。
_SKIP_FILES = {"tests/test_ci_tool_pinning.py"}

# 交付文档（`.md`）的**代码围栏**里写的命令，是读者会照抄执行的 ⇒ 走同一台账。
# 围栏之外的正文不算：那里的历史观测记录（“某天上游 digest 是 f1c337…”）改了就是把记录改假。
# 排除**必须带理由**：否则它就是一个静默豁免（与 `_PENDING` 同一条纪律）。
_MD_HISTORY: dict[str, str] = {
    "CHANGELOG.md": "历史日志：里面的旧 tag 是当天的记录，拿今天的台账去比它等于要求改假",
}
_FENCE = "```"
# 围栏里的 YAML 片段：只认 `image: <值>` 这一行，不引 YAML 解析器（片段可能不合法、也可能多文档）。
_IMAGE_LINE_RE = re.compile(r"^\s*(?:- )?image:\s*[\"']?(?P<value>[^\"'\s#]+)")

# 仓库名 -> (tag, digest, 来源与取数日期)。digest 必须是模块文档里那条可复核的权威值。
_PINNED: dict[str, tuple[str, str, str]] = {
    "ghcr.io/yannh/kubeconform": (
        "v0.6.7",
        "sha256:0925177fb05b44ce18574076141b5c3d83235e1904d3f952182ac99ddc45762c",
        "本机 docker images --digests（上游名）2026-10-04 + 实跑 @digest 验证",
    ),
    "ghcr.io/gitleaks/gitleaks": (
        "v8.21.2",
        "sha256:0e99e8821643ea5b235718642b93bb32486af9c8162c8b8731f7cbdc951a7f46",
        "本机 docker images --digests（上游名）2026-10-04 + 实跑 @digest 验证",
    ),
    "postgres": (
        "16-alpine",
        "sha256:cf78e76683b9ca8c5733cbbdce6c9262b45b6767934dd0a95e671f9a0fc20685",
        "本机 docker images --digests（上游库镜像）2026-10-04 + inspect @digest 验证",
    ),
}

# 仓库名 -> (tag, 为什么还没钉)。**必须带理由与日期**：否则它就是一条静默豁免。
_PENDING: dict[str, tuple[str, str]] = {
    "prom/prometheus": (
        "v2.55.0",
        "2026-10-04：本机到 Docker Hub 不通（registry API 与 pull 都失败），取不到权威 digest",
    ),
    "aquasec/trivy": (
        "0.57.1",
        "2026-10-04：同上（Docker Hub 不通）",
    ),
    "python": (
        "3.12-slim",
        "2026-10-04：同上。它同时是 Dockerfile 基镜像，但门禁侧（变异容器）该钉——两边政策不同",
    ),
    "docker.m.daocloud.io/library/alpine": (
        "3.20",
        "2026-10-04：镜像站回退值（演练脚本拉不到 registry 时的兜底）。镜像站 digest 不权威，"
        "要钉得先换成上游名 `alpine:3.20`，而那需要 Docker Hub 可达",
    ),
}

# 基镜像（Dockerfile `FROM`）：政策上浮动，**只许出现在 FROM 行或文档里**。
# `python` 不放这里：它同时是执行型（变异容器），归 `_PENDING` 管，两边政策不同写在那条理由里。
_BASE_IMAGE_BY_POLICY: dict[str, str] = {
    "node": "Dockerfile 的 web-build 阶段基镜像；拉新政策 + dependabot 管版本",
}

# 本仓自己构建的镜像：按设计每次提交都会变，不需要（也不能）钉 digest。
_INTERNAL_SUFFIX = "warden-agent"

# 镜像 token：仓库名可带斜杠；可选 `:tag`；可选 `@sha256:<64hex>`。至少要有 tag 或 digest。
_IMAGE_RE = re.compile(
    r"(?<![\w./-])(?P<repo>[a-z0-9][a-z0-9._-]*(?:/[a-z0-9._-]+)*)"
    r"(?::(?P<tag>[A-Za-z0-9][A-Za-z0-9._-]*))?"
    r"(?:@(?P<digest>sha256:[0-9a-f]{64}))?"
    r"(?![\w./-])"
)
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_DOCKER_CTX_RE = re.compile(r"\bdocker\s+(?:run|pull|create)\b")

# `docker run` 里**取一个值**的选项。布尔选项绝对不能列进来：列错会把镜像名当成选项的值吃掉
# （第一版用的是“前一个 token 以 - 开头就跳过”的启发式，结果 `docker run --rm busybox:1.36`
# 这种最普通的写法会被漏掉——变异矩阵 M5 抓出来的真洞）。
#
# 这张表**自检**：表写错了，`test_扫描器体检_执行型引用的集合符合预期` 发现的集合就会与
# 台账不符 → 红。将来用了新旗标（带值、又不在表里）也会红，处置办法是把它加进表里。
_VALUE_FLAGS = frozenset(
    {
        "-e", "--env", "--env-file", "-v", "--volume", "--mount", "--volumes-from",
        "-p", "--publish", "-w", "--workdir", "-u", "--user", "--name", "--hostname",
        "-h", "--entrypoint", "--network", "--net", "--security-opt", "--cap-add",
        "--cap-drop", "--tmpfs", "--label", "-l", "--restart", "--platform", "--add-host",
        "--dns", "--ulimit", "--sysctl", "--device", "--log-driver", "--log-opt",
        "--shm-size", "--memory", "-m", "--cpus", "--gpus", "--runtime", "--pid",
        "--ipc", "--userns", "--group-add", "--stop-signal", "--stop-timeout", "--pull",
        "--attach", "-a", "--detach-keys", "--cidfile", "--cgroup-parent", "--health-cmd",
        "--health-interval", "--health-retries", "--health-timeout", "--health-start-period",
        "--blkio-weight", "--pids-limit", "--oom-score-adj",
    }
)

# 布尔选项（不取值）：只为“表里混进布尔选项”的体检断言服务，不参与解析。
_BOOL_FLAGS = frozenset(
    {"-d", "--detach", "-i", "--interactive", "-t", "--tty", "--rm", "--init",
     "--privileged", "--read-only", "--no-healthcheck", "--oom-kill-disable",
     "--sig-proxy", "--tty"}
)
_ASSIGN_RE = re.compile(
    r"^\s*(?:export\s+)?(?P<name>[A-Z][A-Z0-9_]*)\s*=\s*[\"']?(?P<value>[^\"'\s]+)"
)


class _Ref:
    """一处镜像引用：哪个文件哪一行、解析出来的三段、以及它是不是"文档"（注释/文档字符串）。"""

    __slots__ = ("path", "lineno", "repo", "tag", "digest", "is_doc", "text", "executed")

    def __init__(
        self,
        path: str,
        lineno: int,
        repo: str,
        tag: str | None,
        digest: str | None,
        is_doc: bool,
        text: str,
        executed: bool,
    ) -> None:
        self.path = path
        self.lineno = lineno
        self.repo = repo
        self.tag = tag
        self.digest = digest
        self.is_doc = is_doc
        self.text = text
        self.executed = executed

    @property
    def where(self) -> str:
        return f"{self.path}:{self.lineno}"

    @property
    def is_internal(self) -> bool:
        return self.repo.split("/")[-1] == _INTERNAL_SUFFIX

    @property
    def pinned_form(self) -> str:
        return f"{self.repo}:{self.tag}@{self.digest}" if self.digest else f"{self.repo}:{self.tag}"

    def __repr__(self) -> str:
        kind = "执行" if self.executed else ("文档" if self.is_doc else "引用")
        return f"<{self.where} [{kind}] {self.pinned_form}>"


def _doc_lines(source: str, suffix: str) -> set[int]:
    """注释行 + （.py 的）文档字符串行号。

    为什么需要它：**被执行的**必须钉 digest，而"文档里写的"允许只是 tag——改文档里的历史
    记录（比如某条引用了真实报错的命令）会把记录改假，所以两者待遇不同。
    """
    lines = source.splitlines()
    doc = {i for i, line in enumerate(lines, start=1) if line.lstrip().startswith("#")}
    if suffix == ".py":
        # `ast.parse` 不接受 BOM：本仓 `tests/test_cli_commands.py` 带 UTF-8 BOM（用
        # `git ls-files '*.py'` 逐个查前三个字节验过，全仓只有它一个）。读文件用 utf-8-sig。
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(
                node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
            ):
                continue
            if ast.get_docstring(node) is None:
                continue
            first = node.body[0]
            doc.update(range(first.lineno, getattr(first, "end_lineno", first.lineno) + 1))
    return doc


def _logical_lines(source: str) -> list[tuple[int, str]]:
    """把反斜杠续行拼成一行（行号取续行的最后一行）。

    ci.yml 里就是这么写的：`docker run --rm --entrypoint promtool \\` 换行之后才是镜像名。
    不拼续行 = 镜像名一行都扫不到（第一版就是这样，靠下面的"体检"才发现）。
    """
    out: list[tuple[int, str]] = []
    buf: str | None = None
    for lineno, line in enumerate(source.splitlines(), start=1):
        buf = f"{buf} {line}" if buf is not None else line
        if line.rstrip().endswith("\\"):
            continue
        out.append((lineno, buf))
        buf = None
    if buf is not None:
        out.append((len(source.splitlines()), buf))
    return out


def _tokens_with_images(text: str) -> list[tuple[str, str | None, str | None]]:
    """一行里“看起来是镜像”的 token（按选项表跳过带值的选项，其余一个个判）。

    为什么要跳过选项的值：`-v /var/run/docker.sock:/var/run/docker.sock` 的
    `docker.sock:…`、`--security-opt no-new-privileges:true` 的
    `no-new-privileges:true` 都会“长得像镜像”。
    """
    out: list[tuple[str, str | None, str | None]] = []
    tokens = text.replace('"', " ").replace("'", " ").split()
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.startswith("-"):
            flag = token.split("=", 1)[0]
            i += 2 if ("=" not in token and flag in _VALUE_FLAGS) else 1
            continue
        m = _IMAGE_RE.fullmatch(token.rstrip(","))
        if m is not None:
            repo, tag, digest = m.group("repo"), m.group("tag"), m.group("digest")
            if (tag or digest) and any(c.isalpha() for c in repo):
                out.append((repo, tag, digest))
        i += 1
    return out


def _scan_yaml_images(text: str, path: str) -> list[tuple[int, str]]:
    """YAML 结构面的 `image:` 值（用 yaml 解析，不靠正则——正则漏了没人会知道）。"""
    found: list[tuple[int, str]] = []
    doc = yaml.safe_load(text)
    stack: list[object] = [doc]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "image" and isinstance(value, str):
                    lineno = next(
                        (
                            i
                            for i, line in enumerate(text.splitlines(), start=1)
                            if f"image: {value}" in line or f'image: "{value}"' in line
                        ),
                        1,
                    )
                    found.append((lineno, value))
                else:
                    stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    return found


def _execute_context_refs(path: str, suffix: str, text: str, doc: set[int]) -> list[_Ref]:
    """"被执行"的引用：`docker run/pull`、`*_IMAGE=` / `IMG=` 赋值、YAML `image:`。"""
    lines = text.splitlines()
    refs: list[_Ref] = []
    for lineno, logical in _logical_lines(text):
        raw = lines[lineno - 1] if lineno <= len(lines) else logical
        if "FROM " in raw:  # 基镜像：政策上浮动（见模块文档）
            continue
        is_doc = lineno in doc
        if _DOCKER_CTX_RE.search(logical):
            for repo, tag, digest in _tokens_with_images(logical):
                refs.append(_Ref(path, lineno, repo, tag, digest, is_doc, logical.strip(), True))
        match = _ASSIGN_RE.match(raw)
        if match and ("IMAGE" in match.group("name") or match.group("name") == "IMG"):
            token = match.group("value").strip().rstrip(",")
            m = _IMAGE_RE.fullmatch(token)
            if m is not None and (m.group("tag") or m.group("digest")):
                refs.append(
                    _Ref(
                        path,
                        lineno,
                        m.group("repo"),
                        m.group("tag"),
                        m.group("digest"),
                        is_doc,
                        raw.strip(),
                        True,
                    )
                )
    return refs


def _tracked_md_files() -> list[str]:
    """提交里的 `.md` 清单——**从提交里取，不从磁盘上捞**，而且**不带 pathspec**。

    磁盘上还有工作区里的临时文档（`.zcode/plans/*.md`、草稿），它们不是交付面；反过来，
    新加的交付文档必须自动进入范围。所以两侧都得对着 `git ls-files`（第七十二批的教训：
    “本机扫到的文件集”与“提交里的文件集”必须相等，差一条就是一条静默漏检）。

    后缀过滤留在 Python 里（不在 git 的 pathspec 上）：这样用例那侧可以用**另一条 argv**
    （`git ls-files "*.md"`）当独立 oracle——否则“这个辅助函数漏了一个文件”会让两侧一起变小，
    那条相等断言就变成恒真。
    """
    proc = subprocess.run(
        ["git", "-c", "core.quotePath=false", "ls-files"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, f"git ls-files 失败，拿不到交付文档清单：{proc.stderr.strip()}"
    lines = proc.stdout.replace("\r\n", "\n").split("\n")
    return [p for p in lines if p.endswith(".md")]


def _fence_mask(source: str) -> tuple[str, set[int]]:
    """把代码围栏**之外**的行掏空（行号保持不变），返回 (掏空后的文本, 围栏内行号)。

    为什么掏空而不是只截出围栏：解析器靠整段文本的行号与结构（反斜杠续行拼接、行首赋值匹配），
    掏空之后 `path:lineno` 仍然对得上原文，报出来的位置可以直接去看。
    """
    keep: list[str] = []
    inside = False
    fence_lines: set[int] = set()
    for lineno, line in enumerate(source.splitlines(), start=1):
        if line.lstrip().startswith(_FENCE):
            inside = not inside
            keep.append("")
            continue
        keep.append(line if inside else "")
        if inside:
            fence_lines.add(lineno)
    return "\n".join(keep), fence_lines


def _scan() -> tuple[list[_Ref], list[_Ref], int, list[str]]:
    """返回 (执行型引用, 全量引用, 读到的文件数)。

    全量引用（含文档/注释）用"按登记过的仓库名去搜"的方式取——这样文档漂移也守得住，
    而不是靠"扫所有看起来像镜像的 token"（那会撞上 `alice:…`、`caller:…` 一堆噪声）。
    """
    executed: list[_Ref] = []
    refs: list[_Ref] = []
    files = 0
    md_files: list[str] = []
    for top in _SCAN_DIRS:
        for path in sorted((_ROOT / top).rglob("*")):
            if path.suffix not in _FILE_SUFFIXES or any(p in _SKIP_NAMES for p in path.parts):
                continue
            rel = path.relative_to(_ROOT).as_posix()
            if rel in _SKIP_FILES:
                continue
            source = path.read_text(encoding="utf-8-sig", errors="replace")
            files += 1
            doc = _doc_lines(source, path.suffix)
            executed.extend(_execute_context_refs(rel, path.suffix, source, doc))
            if path.suffix in {".yml", ".yaml"}:
                for lineno, value in _scan_yaml_images(source, rel):
                    m = _IMAGE_RE.fullmatch(value)
                    if m is not None:
                        executed.append(
                            _Ref(
                                rel,
                                lineno,
                                m.group("repo"),
                                m.group("tag"),
                                m.group("digest"),
                                lineno in doc,
                                value,
                                True,
                            )
                        )
            # 全量引用：按登记名搜索（两个方向都守，注释与文档字符串也算）
            for repo in (*_PINNED, *_PENDING, *_BASE_IMAGE_BY_POLICY):
                pattern = re.compile(
                    re.escape(repo)
                    + r"(?::(?P<tag>[A-Za-z0-9][A-Za-z0-9._-]*))?"
                    + r"(?:@(?P<digest>sha256:[0-9a-f]{64}))?(?![\w./-])"
                )
                for lineno, line in enumerate(source.splitlines(), start=1):
                    if "FROM " in line:
                        continue
                    for m in pattern.finditer(line):
                        refs.append(
                            _Ref(
                                rel,
                                lineno,
                                repo,
                                m.group("tag"),
                                m.group("digest"),
                                lineno in doc,
                                line.strip(),
                                False,
                            )
                        )
    # 交付文档的代码围栏（见模块文档与 `_MD_HISTORY`）
    for rel in _tracked_md_files():
        if rel in _MD_HISTORY:
            continue
        md_files.append(rel)
        source_md = (_ROOT / rel).read_text(encoding="utf-8-sig", errors="replace")
        masked, fence_lines = _fence_mask(source_md)
        executed.extend(_execute_context_refs(rel, ".md", masked, fence_lines))
        for lineno, line in enumerate(masked.splitlines(), start=1):
            if lineno not in fence_lines:
                continue
            match = _IMAGE_LINE_RE.match(line)
            if match is None:
                continue
            token = match.group("value").strip().rstrip(",")
            image = _IMAGE_RE.fullmatch(token)
            if image is not None and (image.group("tag") or image.group("digest")):
                executed.append(
                    _Ref(
                        rel,
                        lineno,
                        image.group("repo"),
                        image.group("tag"),
                        image.group("digest"),
                        True,
                        line.strip(),
                        True,
                    )
                )
    return executed, refs, files, md_files


_EXECUTED, _REFS, _FILES, _MD_FILES = _scan()
_EXTERNAL_EXECUTED = [r for r in _EXECUTED if not r.is_internal]


def test_台账自身自洽() -> None:
    for repo, (tag, digest, source) in _PINNED.items():
        assert tag and source, f"{repo} 缺 tag 或来源"
        assert _DIGEST_RE.match(digest), f"{repo} 的 digest 形状不对：{digest}"
    for repo, (tag, reason) in _PENDING.items():
        assert tag, f"{repo} 缺 tag"
        assert len(reason) >= 20 and "2026-" in reason, f"{repo} 的待钉理由太含糊：{reason}"
    for repo, reason in _BASE_IMAGE_BY_POLICY.items():
        assert reason, f"{repo} 缺理由"


def test_选项表自身自洽() -> None:
    """布尔选项混进取值表 = 会把镜像名吃掉（一个静默漏检），所以单独断言。"""
    overlap = _VALUE_FLAGS & _BOOL_FLAGS
    assert not overlap, f"这些是布尔选项，不能出现在 _VALUE_FLAGS 里：{sorted(overlap)}"
    assert "--rm" not in _VALUE_FLAGS, "--rm 不取值"


def test_扫描器体检_执行型引用的集合符合预期() -> None:
    """解析器/上下文判据坏掉时**必须红**（否则"没扫到"会被当成"没问题"）。"""
    assert _FILES >= 20, f"只读到 {_FILES} 个文件，扫描范围或读取可能坏了"
    found = {r.repo for r in _EXTERNAL_EXECUTED}
    registered = set(_PINNED) | set(_PENDING)
    assert found == registered, (
        "扫描到但未登记：" + str(sorted(found - registered))
        + "；台账里但没在执行上下文里扫到：" + str(sorted(registered - found))
    )
    assert not (set(_BASE_IMAGE_BY_POLICY) & found), "基镜像出现在执行上下文里了"


def test_内部镜像被认出来而不是被当成外部() -> None:
    internal = [r for r in _EXECUTED if r.is_internal]
    assert internal, "扫描器没认出本仓自建镜像（warden-agent*），说明解析坏了"


def test_被执行的引用必须钉在台账_digest_上() -> None:
    """执行面（CI 步骤、脚本、测试里真跑的 docker run）必须钉 digest。

    文档字符串里的手工命令不在此列（读者自己敲的命令，写成可读的 tag 更合理），
    但它们的 tag 由 `test_文档里引用的版本不漂移` 守。
    """
    for repo, (tag, digest, _) in _PINNED.items():
        hits = [r for r in _EXTERNAL_EXECUTED if r.repo == repo and not r.is_doc]
        assert hits, f"{repo} 没有非文档的执行型引用，这条判据是空的"
        for r in hits:
            assert r.digest == digest, (
                f"{r.where}：digest 与台账不一致\n  台账 {digest}\n  实际 {r.digest}\n  {r.text}"
            )
            assert r.tag == tag, f"{r.where}：tag 与台账不一致（台账 {tag}，实际 {r.tag}）"


def test_待钉清单里的镜像不许已经带上_digest() -> None:
    """反向：如果某一处已经钉了，台账就该从 `_PENDING` 移走——否则台账在骗人。"""
    for r in _EXTERNAL_EXECUTED:
        if r.repo in _PENDING:
            assert r.digest is None, f"{r.where} 已钉 digest，应当从 _PENDING 移到 _PINNED"
            assert r.tag == _PENDING[r.repo][0], (
                f"{r.where}：tag 与台账不一致（台账 {_PENDING[r.repo][0]}，实际 {r.tag}）"
            )


def test_文档里引用的版本不漂移() -> None:
    """注释/文档字符串里的引用：允许只是 tag，但 tag 必须与台账一致；带了 digest 更要对得上。"""
    checked = 0
    for r in _REFS:
        tag_expected = _PINNED.get(r.repo, _PENDING.get(r.repo, (None,)))[0]
        if tag_expected is None or not r.is_doc or r.tag is None:
            # 只提仓库名、不写 tag 的散文（“用户 postgres、空密码”）不算版本引用。
            continue
        checked += 1
        assert r.tag == tag_expected, (
            f"{r.where}：文档里的 tag 漂移（台账 {tag_expected}，实际 {r.tag}）→ {r.text}"
        )
        if r.digest is not None and r.repo in _PINNED:
            assert r.digest == _PINNED[r.repo][1], f"{r.where}：文档里的 digest 与台账不一致"
    assert checked >= 5, f"只检查了 {checked} 处文档引用，判据可能是空的"


def test_门禁与反向对照跑的是同一个程序() -> None:
    """gitleaks 那条反向对照的意义全在"同一个程序"上：门禁容器与对照容器必须同 digest。"""
    hits = [r for r in _EXTERNAL_EXECUTED if r.repo == "ghcr.io/gitleaks/gitleaks"]
    assert len(hits) >= 2, f"gitleaks 只扫到 {len(hits)} 处，门禁/反向对照的一致性没人守"
    places = {r.path for r in hits}
    needed = {".github/workflows/ci.yml", "scripts/gitleaks_reverse_control.sh"}
    assert needed <= places, f"gitleaks 的引用位置变了：{sorted(places)}"


def test_kubeconform_与_trivy_与_promtool_都在执行上下文里被扫到() -> None:
    """逐工具点名：任一条的 `docker run` 被改写成别的形式（解析不到）时会红。"""
    for repo in ("ghcr.io/yannh/kubeconform", "aquasec/trivy", "prom/prometheus", "postgres"):
        hits = [r for r in _EXTERNAL_EXECUTED if r.repo == repo]
        assert hits, f"{repo} 在执行上下文里一处都没扫到"


def test_yaml_services_的镜像也走同一台账() -> None:
    workflow = _ROOT / ".github" / "workflows" / "ci.yml"
    doc = yaml.safe_load(workflow.read_text(encoding="utf-8-sig"))
    images: list[str] = []
    for job in doc.get("jobs", {}).values():
        for svc in (job.get("services") or {}).values():
            if "image" in svc:
                images.append(svc["image"])
    assert images, "YAML 面没解析到任何 services 镜像（结构变了或解析坏了）"
    for image in images:
        repo = image.split("@")[0].rsplit(":", 1)[0]
        assert repo in _PINNED, f"service 镜像 {image} 不在台账里"
        assert "@sha256:" in image, f"service 镜像 {image} 没钉 digest"


def test_交付文档里可复制的镜像命令也必须同源() -> None:
    """`.md` 围栏里的命令可以只写 tag（读者自己敲），但 tag 必须与台账一致；
    带了 digest 更要对得上；**没登记的镜像直接红**（先在台账里登记，再写进文档）。"""
    hits = [r for r in _EXECUTED if r.path.endswith(".md") and not r.is_internal]
    assert len(hits) >= 3, f"交付文档围栏里只扫到 {len(hits)} 处镜像命令，判据可能是空的"
    for r in hits:
        assert r.repo in _PINNED or r.repo in _PENDING, (
            f"{r.where}：文档里的镜像没进台账 → 先登记再写进文档\n  {r.text}"
        )
        want = (_PINNED.get(r.repo) or _PENDING.get(r.repo))[0]
        if r.tag is not None:
            assert r.tag == want, (
                f"{r.where}：文档里的 tag 漂移（台账 {want}，实际 {r.tag}）→ {r.text}"
            )
        if r.digest is not None and r.repo in _PINNED:
            assert r.digest == _PINNED[r.repo][1], f"{r.where}：文档里的 digest 与台账不一致"


def test_围栏判据的两个方向() -> None:
    """正向：围栏里的漂移 tag 会被认出来；反向：正文里的同一句话不会被当成执行面。"""
    drift = "```bash\ndocker run --rm ghcr.io/yannh/kubeconform:v0.0.1 \\\n  -summary /m\n```\n"
    masked, fence_lines = _fence_mask(drift)
    found = _execute_context_refs("docs/x.md", ".md", masked, fence_lines)
    assert [r.repo for r in found] == ["ghcr.io/yannh/kubeconform"], found
    assert found[0].tag == "v0.0.1"
    assert found[0].is_doc, "文档围栏里的引用必须按“文档”待遇（tag 级判据，不是 digest 级）"

    prose = "我们当时跑的是 ghcr.io/yannh/kubeconform:v0.0.1 那个版本，记录在这里。\n"
    masked_prose, fence_lines_prose = _fence_mask(prose)
    assert fence_lines_prose == set(), "正文里没有围栏"
    assert _execute_context_refs("docs/x.md", ".md", masked_prose, fence_lines_prose) == [], (
        "正文散文被当成执行面了——历史记录会被天天误报"
    )


def test_围栏里的_yaml_片段能认出_image_行() -> None:
    """文档里贴的清单片段：`image:` 行按行取（不引解析器，片段可能不合法/多文档）。"""
    snippet = (
        "```yaml\n"
        "spec:\n  containers:\n    - name: app\n"
        "      image: REPLACE_ME/warden-agent:0.1.0\n"
        "```\n"
    )
    masked, fence_lines = _fence_mask(snippet)
    line = next(
        text for lineno, text in enumerate(masked.splitlines(), start=1) if lineno in fence_lines
        and _IMAGE_LINE_RE.match(text)
    )
    match = _IMAGE_LINE_RE.match(line)
    assert match is not None and match.group("value") == "REPLACE_ME/warden-agent:0.1.0"


def test_历史日志显式排除而且要写出理由() -> None:
    assert "CHANGELOG.md" in _MD_HISTORY, "历史日志必须显式排除（否则旧 tag 天天误报）"
    assert len(_MD_HISTORY["CHANGELOG.md"]) >= 20, "排除必须带理由，不能是一个裸名字"
    assert "CHANGELOG.md" not in _MD_FILES, "历史日志被扫了"


def test_交付文档的扫描集等于提交里的_md_集() -> None:
    """两侧必须相等：少一个 = 新文档静默漏出范围；多一个 = 扫到了非交付面的东西。

    oracle 必须**独立于被测代码**：这里另起一条 argv（`git ls-files "*.md"`）直接问 git，
    不复用 `_tracked_md_files()`——复用的话“辅助函数漏掉一个文件”会让两侧同时变小、断言恒真。
    """
    proc = subprocess.run(
        ["git", "-c", "core.quotePath=false", "ls-files", "*.md"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, f"独立口径问不出来：{proc.stderr.strip()}"
    independent = {p for p in proc.stdout.replace("\r\n", "\n").split("\n") if p}
    assert len(independent) >= 8, f"独立口径只看到 {len(independent)} 个 .md，这条判据可能是空的"
    expected = independent - set(_MD_HISTORY)
    assert expected == set(_MD_FILES), (
        "扫描集与提交集不一致：\n  提交里没扫到的："
        + str(sorted(expected - set(_MD_FILES)))
        + "\n  扫了但不在提交里的："
        + str(sorted(set(_MD_FILES) - independent))
    )
