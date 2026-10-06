"""交付/演练清单里的外部镜像：要么钉在 digest 上，要么进台账（本批：第六十四批）。

为什么要有这条（和第六十三批那条是**互补**的，不是重复）：

- 第六十三批守的是**门禁自己执行**的程序（ci.yml 里的 `docker run`）：那些必须钉住，
  因为它们挂着工作区、`trivy` 还挂 `/var/run/docker.sock`，浮动 tag = CI 里执行的是
  别人随时可以替换的代码。
- 本守卫守的是**交付/演练清单**（`deploy/**`、根 `docker-compose.yml`）：这里镜像被打包
  进交付物、被人拿去 `kubectl apply` / `docker compose up`。浮动 tag 的失败模式不同：
  不是"门禁撒谎"，而是**同一份清单，今天装出来的和明天装出来的不是同一个东西**，
  出了事故没法回到当时那个镜像；演练栈（prometheus/grafana）尤其容易烂在这里——
  它常年没人看，tag 烂了也没人知道。

为什么是"钉住**或**进台账"，不是"一律钉住"：

- 本机的网络到上游 registry（Docker Hub）会被限流，**取不到权威 digest 就不能瞎写一个**。
  第六十三批已经实证：镜像站的 digest 不能当权威——2026-10-04 实测同一个 postgres 镜像，
  在上游 registry 名下是 `sha256:f1c337…`、在 `docker.m.daocloud.io` 名下是 `sha256:a3b7f4…`
  （镜像站自己重签名了 manifest；当时用的具体 tag 记在桌面笔记第 61/62 篇，这里不写 tag
  是为了不让本文件与镜像台账的 tag 互相漂移——tests/test_ci_tool_pinning.py 守着这一点）。
  写一个错的 digest 比留着 tag **更坏**：tag 至少还能拉下来，错 digest 直接拉不动，
  而且看起来"已经钉住了"。
- 所以没能力钉的，进 `deploy/IMAGE_LEDGER.yaml` 记账：**日期 + 理由 + 复核期限**。
  台账不是免死金牌——过了 `review_by` 守卫就红。

有意**不**要求钉住的（写清楚，免得下一个人以为漏了）：

- `REPLACE_ME/warden-agent:<tag>` 应用镜像占位符：发布时由
  `scripts/pin_image_digests.sh --app-image <ref>` 回写成 `<仓库>@sha256:…`。
  留占位符是对的，钉一个不存在的仓库才是错的。
- 注释掉的引用（如 `docker-compose.yml` 里注释的 `db:` 服务）：本守卫**解析 YAML**，
  不 grep，所以注释天然不参与——但如果有人把它取消注释，它立刻变成"未登记的浮动引用"
  被拦住。这正是我们想要的：**这条路的失败模式是"悄悄多了一个浮动镜像"**。

用法：
    python scripts/check_pinned_images.py                 # 守本仓库
    python scripts/check_pinned_images.py --root <目录>    # 守任意目录树（负向对照用）
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
LEDGER_NAME = "deploy/IMAGE_LEDGER.yaml"
LEDGER_FIELDS = ("image", "added", "reason", "review_by")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
# 占位 digest：64 位**全都相同**（全 0、全 f、全 1…）。真 digest 不可能 64 位同一个字符，
# 所以这个宽度比“只怼全 0”更强；但报错文案得跟得上，不能只说全 0/全 f（否则误报时人看不懂）。
FAKE_DIGEST_RE = re.compile(r"^sha256:(.)\1{63}$")
# 发布时由 pin_image_digests.sh --app-image 回写的占位符（本仓自己的应用镜像）
PLACEHOLDER_PREFIX = "REPLACE_ME/"
# 这两个键下面的每一项都是 Pod 级容器规格，`image:` 在里面才有意义
CONTAINER_KEYS = ("containers", "initContainers")


class Finding:
    """一条巡检结论（违规或统计）。"""

    def __init__(self, level: str, path: str, line_hint: str, message: str) -> None:
        self.level = level  # "error" | "info"
        self.path = path
        self.line_hint = line_hint
        self.message = message

    def render(self) -> str:
        icon = "❌" if self.level == "error" else "·"
        where = f"{self.path}:{self.line_hint}" if self.line_hint else self.path
        return f"{icon} {where} {self.message}"


def _iter_yaml_files(root: Path) -> Iterator[Path]:
    """交付/演练清单：仓库根的 compose 文件 + `deploy/**` 下的 YAML。"""
    seen: set[Path] = set()
    for name in ("docker-compose.yml", "docker-compose.yaml"):
        path = root / name
        if path.is_file() and path not in seen:
            seen.add(path)
            yield path
    deploy = root / "deploy"
    if deploy.is_dir():
        for path in sorted(deploy.rglob("*")):
            is_manifest = path.is_file() and path.suffix in {".yml", ".yaml"}
            if is_manifest and path.name != Path(LEDGER_NAME).name and path not in seen:
                seen.add(path)
                yield path


def _walk_container_images(node: Any, prefix: str = "") -> Iterator[tuple[str, str]]:
    """递归找 `containers:` / `initContainers:` 下面每一项的 `image:`。

    返回 (定位串, 镜像引用)，定位串用容器名（没名字就退成 `containers[0]`）——
    早期版本这里塞的是文件名，输出变成
    `deploy/k8s/backup-cronjob.yaml:deploy/k8s/backup-cronjob.yaml`，等于没告诉你哪一处。
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key in CONTAINER_KEYS and isinstance(value, list):
                for idx, item in enumerate(value):
                    if isinstance(item, dict) and isinstance(item.get("image"), str):
                        name = item.get("name") or f"{key}[{idx}]"
                        yield f"{prefix}{key}.{name}", item["image"]
            yield from _walk_container_images(value, prefix)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_container_images(item, prefix)


def _collect_from_doc(doc: Any) -> Iterator[tuple[str, str]]:
    """一份 YAML 文档里的外部镜像引用（compose 走 `services.*.image`，其余走容器规格）。"""
    if isinstance(doc, dict) and isinstance(doc.get("services"), dict):
        for name, service in doc["services"].items():
            if isinstance(service, dict) and isinstance(service.get("image"), str):
                yield f"services.{name}", service["image"]
        return
    yield from _walk_container_images(doc)


def collect_refs(root: Path) -> list[tuple[Path, str, str]]:
    """返回 (文件, 位置提示, 镜像引用)。compose 用服务名，k8s 用容器名。"""
    refs: list[tuple[Path, str, str]] = []
    for path in _iter_yaml_files(root):
        rel = path.relative_to(root).as_posix()
        try:
            # `safe_load_all`：k8s 清单里 `---` 分隔多文档是常态
            # （`deploy/k8s/namespace.yaml` 就是两份），用 `safe_load` 会直接报
            # "expected a single document in the stream" —— 那是**守卫的假红**，
            # 比漏检更坏：报红的东西根本不是镜像问题。
            docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        except yaml.YAMLError as exc:  # 解析失败也是红的：清单读不出来比读错更糟
            refs.append((path, rel, f"__PARSE_ERROR__ {exc}"))
            continue
        for doc in docs:
            for hint, image in _collect_from_doc(doc):
                refs.append((path, hint, image))
    return refs


def load_ledger(root: Path) -> tuple[dict[str, dict[str, Any]], list[Finding], bool]:
    path = root / LEDGER_NAME
    findings: list[Finding] = []
    if not path.is_file():
        # **文件不存在本身不是红**：一个全钉住的仓库根本不需要台账，没台账也给红
        # 就是假红（本仓自己的口径：假红比漏检更坏，它教人忽略门禁）。
        # 只有在“真有未钉的引用、要拿台账抵”的时候，缺台账才算违规——
        # 那个判断在 `audit()` 里做（它知道哪些引用没钉住）。
        return {}, [], False
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return {}, [Finding("error", LEDGER_NAME, "", f"台账不是合法 YAML：{exc}")], True
    entries = (data or {}).get("entries")
    if not isinstance(entries, list):
        return {}, [Finding("error", LEDGER_NAME, "", "台账缺少 `entries:` 列表")], True
    ledger: dict[str, dict[str, Any]] = {}
    today = _dt.date.today()
    for idx, entry in enumerate(entries):
        at = f"entries[{idx}]"
        if not isinstance(entry, dict):
            findings.append(Finding("error", LEDGER_NAME, at, "台账条目不是映射"))
            continue
        missing = [f for f in LEDGER_FIELDS if not entry.get(f)]
        if missing:
            findings.append(
                Finding("error", LEDGER_NAME, at, f"台账条目缺字段：{', '.join(missing)}")
            )
            continue
        image = str(entry["image"])
        if image in ledger:
            findings.append(Finding("error", LEDGER_NAME, at, f"台账里 `{image}` 重复登记"))
            continue
        ledger[image] = entry
        for field in ("added", "review_by"):
            raw = entry[field]
            try:
                when = raw if isinstance(raw, _dt.date) else _dt.date.fromisoformat(str(raw))
            except ValueError:
                findings.append(
                    Finding("error", LEDGER_NAME, at, f"`{field}` 不是 ISO 日期：{raw!r}")
                )
                continue
            if field == "review_by" and when < today:
                findings.append(
                    Finding(
                        "error",
                        LEDGER_NAME,
                        at,
                        f"`{image}` 的复核期限 {when.isoformat()} 已过（今天 {today.isoformat()}）"
                        "：要么钉住，要么重新评估并更新日期",
                    )
                )
            if field == "added" and when > today:
                findings.append(Finding("error", LEDGER_NAME, at, f"`added` 是未来日期：{raw!r}"))
    return ledger, findings, True


def _bad_digest_msg(image: str) -> str:
    """单拿出来写：拼在 `Finding(...)` 里会超 100 列（ruff E501，本仓真开门禁）。"""
    return f"digest 格式不对（要 sha256: + 64 位小写十六进制）：{image}"


def audit(root: Path) -> tuple[list[Finding], dict[str, int]]:
    refs = collect_refs(root)
    ledger, findings, ledger_exists = load_ledger(root)
    stats = {"refs": 0, "pinned": 0, "placeholder": 0, "ledgered": 0}
    used: set[str] = set()
    for path, hint, image in refs:
        rel = path.relative_to(root).as_posix()
        if image.startswith("__PARSE_ERROR__"):
            findings.append(Finding("error", rel, hint, image))
            continue
        # 同一个引用出现在多处是正常的（app 镜像在 deployment 与 job 里各一次），
        # 但**每一处**都要合规，所以逐处检查、只在统计上重复计数。
        stats["refs"] += 1
        if "$" in image or "{" in image:
            # 用变量拼镜像引用 = 绕开这个守卫（它只能检查字面量），而且没人能一眼看出
            # 实际跑的是哪个镜像。行为上等价于“没钉”，所以直接红。
            findings.append(
                Finding(
                    "error",
                    rel,
                    hint,
                    f"镜像引用不能是变量/插值（守卫就看不见它了）：{image}\n"
                    "    修法：写完整的字面量引用；非要可变就把它放进一张“每个环境一份”的清单里。",
                )
            )
            continue
        if "@" in image:  # 钉住的形式：`<ref>@sha256:<64hex>`
            digest = image.rpartition("@")[2]
            if DIGEST_RE.match(digest) and not FAKE_DIGEST_RE.match(digest):
                stats["pinned"] += 1
                continue
            if FAKE_DIGEST_RE.match(digest):
                findings.append(
                    Finding(
                        "error",
                        rel,
                        hint,
                        "digest 是占位值（64 位全都相同），能过语法校验但拉取时 "
                        f"`manifest unknown`：{image}\n"
                        "    这就是“看起来钉了、其实从没跑起来”。"
                        "（口径与 tests/test_k8s_manifests.py 一致）",
                    )
                )
                continue
            findings.append(Finding("error", rel, hint, _bad_digest_msg(image)))
            continue
        if image.startswith(PLACEHOLDER_PREFIX):
            if not rel.startswith("deploy/k8s/"):
                findings.append(
                    Finding(
                        "error",
                        rel,
                        hint,
                        f"占位符只允许出现在 deploy/k8s/（发布时由 --app-image 回写）：{image}",
                    )
                )
                continue
            stats["placeholder"] += 1
            continue
        if image in ledger:
            stats["ledgered"] += 1
            used.add(image)
            continue
        where = (
            f" {LEDGER_NAME} 台账里"
            if ledger_exists
            else f" {LEDGER_NAME} 台账里（台账文件还不存在）"
        )
        findings.append(
            Finding(
                "error",
                rel,
                hint,
                f"外部镜像既没钉 digest、也不在{where}：{image}\n"
                f"    修法：能取到上游 digest 就写成 {image.split(':')[0]}@sha256:<digest>；\n"
                f"    取不到就进 {LEDGER_NAME} 记一笔（字段要写全：\n"
                "    image / added / reason / review_by）。",
            )
        )
    for image, entry in ledger.items():
        if image not in used:
            findings.append(
                Finding(
                    "error",
                    LEDGER_NAME,
                    "",
                    f"台账条目 `{image}` 已经没有任何清单引用了（added={entry.get('added')}）"
                    "：过期条目要删掉，否则台账会越来越大、越来越没人信",
                )
            )
    return findings, stats


def main(argv: list[str] | None = None) -> int:
    # 本机控制台是 cp936，`❌`/`·` 直接 print 会 UnicodeEncodeError（实测：脚本以 traceback
    # 退出，CI 步骤上看到的是一堆栈、看不见"哪几个镜像不合规"）。对**门禁**来说这特别坏：
    # 它把"结论"换成了"崩溃"。修法与 `src/warden_agent/rag/eval.py:main` 一致（同一仓
    # 已是第三次踩：`scripts/load_test.py` 的 ⚠️、`rag/eval.py` 的 ⚠ 都修过）。
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    parser = argparse.ArgumentParser(description="交付/演练清单里的外部镜像必须钉 digest 或进台账")
    parser.add_argument("--root", default=str(REPO_ROOT), help="仓库根目录（默认本脚本所在仓库）")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"❌ 不是目录：{root}")
        return 2
    findings, stats = audit(root)
    errors = [f for f in findings if f.level == "error"]
    for finding in findings:
        print(finding.render())
    print(
        f"\n扫描 {stats['refs']} 处外部镜像引用：钉住 {stats['pinned']}、"
        f"发布占位符 {stats['placeholder']}、台账 {stats['ledgered']}"
    )
    if errors:
        print(f"\n❌ {len(errors)} 处不合规")
        return 1
    print("✅ 全部钉住或有台账记录")
    return 0


if __name__ == "__main__":
    sys.exit(main())
