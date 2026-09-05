"""技能（Skill）—— 符合 SKILL.md 协议的、可渐进披露的能力包。

  - SkillMetadata             ：SKILL.md frontmatter（name/description/version/author/trust）。
  - SkillContent              ：解析后的技能内容（元数据 + 正文指令 + 引用的资源）。
  - SkillPackageParser        ：把一段 SKILL.md 文本解析成 SkillContent。
  - SkillCatalog              ：按别名登记/查找技能（FrozenSkillBinding 概念）。
  - 渐进披露（progressive disclosure）：
      技能不把全部能力一股脑塞给 Agent；只有被"激活"时才把正文注入上下文，
      或把技能定义的操作转成 Tool。信任快照记录"这个技能哪来的、可信度如何"。

白话解释：
  Tool 是"一张技能卡"（能调用）；Skill 是"一份带说明书的技能包"（SKILL.md 协议）。
  Skill = 元数据（这技能干嘛的）+ 正文（怎么用/规则）+ 可选资源。
  它比 Tool 更"文档化"，也更符合"能力可按需披露、可审计来源"的需求。
  典型来源：某个目录下的 SKILL.md、内嵌在 classpath 里的技能、或从外部加载。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from typing import Protocol

_SKILL_FRONT = re.compile(r"^---\s*$(.*?)^---\s*$", re.MULTILINE | re.DOTALL)


@dataclass(frozen=True)
class SkillMetadata:
    """SKILL.md 的 frontmatter 元数据。"""

    name: str = ""
    description: str = ""
    version: str = ""
    author: str = ""
    trust: str = "untrusted"  # trusted / untrusted / unknown
    requires: tuple[str, ...] = ()  # 需要的工具/依赖

    @classmethod
    def parse(cls, frontmatter: str) -> SkillMetadata:
        """从 YAML 风格的 frontmatter 解析元数据（极简解析，够用不引依赖）。"""
        fields: dict[str, object] = {}
        for line in frontmatter.splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip().lower().replace("-", "_")
            value = value.strip().strip('"').strip("'")
            if not key:
                continue
            fields[key] = value
        return cls(
            name=str(fields.get("name", "")),
            description=str(fields.get("description", "")),
            version=str(fields.get("version", "")),
            author=str(fields.get("author", "")),
            trust=str(fields.get("trust", "untrusted")),
        )


@dataclass(frozen=True)
class SkillContent:
    """解析后的技能包：元数据 + 正文指令 + 资源引用。"""

    metadata: SkillMetadata
    body: str = ""
    resources: tuple[str, ...] = ()

    @property
    def digest(self) -> str:
        """内容摘要（信任快照用）：对版本+正文+资源做哈希。

        版本也参与哈希：同一份技能的不同版本会得到不同的信任快照，
        审计时能区分"用的是哪个版本"（不会因为都叫 alias 就同一个摘要）。
        """
        return hashlib.sha256(
            f"{self.metadata.version}|{self.metadata.name}|{self.body}|{self.resources}"
            .encode()
        ).hexdigest()[:16]


class SkillPackageParser:
    """把 SKILL.md 文本解析成 SkillContent。"""

    def parse(self, markdown: str) -> SkillContent:
        body = markdown
        metadata = SkillMetadata()
        m = _SKILL_FRONT.search(markdown)
        resources: tuple[str, ...] = ()
        if m:
            metadata = SkillMetadata.parse(m.group(1))
            body = markdown[m.end():].strip()
            resources = tuple(_find_resources(body))
        return SkillContent(metadata=metadata, body=body, resources=resources)


def _find_resources(body: str) -> list[str]:
    """粗略地扫描正文里的资源引用（如 脚本路径/文件）。"""
    out: list[str] = []
    for line in body.splitlines():
        for pat in (r"\{\{\s*\$([A-Za-z0-9_./-]+)\s*\}\}",):
            for mm in re.finditer(pat, line):
                out.append(mm.group(1))
    return out


@dataclass(frozen=True)
class SkillTrustSnapshot:
    """信任快照：记录这个技能的来源与内容摘要，供审计。"""

    alias: str
    source: str
    digest: str
    level: str


class FrozenSkillBinding(Protocol):
    """一个已登记、可激活的技能绑定。"""

    def metadata(self) -> SkillMetadata: ...
    def content(self) -> SkillContent: ...
    def activate(self) -> str: ...  # 注入正文返回给 Agent 作为指令
    def trust_snapshot(self) -> SkillTrustSnapshot: ...


class _BoundSkill:
    """默认技能绑定实现。"""

    def __init__(self, alias: str, content: SkillContent, source: str) -> None:
        self._alias = alias
        self._content = content
        self._source = source

    def metadata(self) -> SkillMetadata:
        return self._content.metadata

    def content(self) -> SkillContent:
        return self._content

    def activate(self) -> str:
        """激活：把技能正文作为可注入的系统指令返回（渐进披露的核心）。"""
        md = self._content.metadata
        head = f"[技能 {md.name}] {md.description}"
        return f"{head}\n\n{self._content.body}".strip()

    def trust_snapshot(self) -> SkillTrustSnapshot:
        return SkillTrustSnapshot(
            alias=self._alias,
            source=self._source,
            digest=self._content.digest,
            level=self._content.metadata.trust,
        )


class SkillCatalog:
    """技能目录：按别名登记/查找技能，一个别名可存在多个版本，默认取最新。

    冻结绑定：每个 (别名, 版本) 一经登记不变。查询不传版本时默认返回**最新版**。
    """

    def __init__(self) -> None:
        # 别名 → {版本字符串 → 该版本的绑定}。版本空串("")视为特殊"无版本"分支。
        self._versions: dict[str, dict[str, _BoundSkill]] = {}

    def load_skill(self, alias: str, content: SkillContent, source: str = "inline") -> None:
        """登记一份技能（按它的 version 分桶）。同一别名可登记多个版本，互不覆盖。

        - 不传/空 version 的技能登记到该别名的"无版本"分支。
        - 同一 (别名, 版本) 再次登记会覆盖（幂等更新该版本）。
        """
        version = content.metadata.version or ""
        self._versions.setdefault(alias, {})[version] = _BoundSkill(alias, content, source)

    def find(self, alias: str, version: str | None = None) -> FrozenSkillBinding | None:
        """按别名找技能；不传 `version` 默认取**最新版**，传版本取指定版。

        - `version=None`：在该别名的所有版本里挑版本号最高者（数字比较优先，回退字典序）。
        - `version="..."`：精确取该版本；没有则返回 None。
        - `version=""`：取"无版本"分支。
        """
        bucket = self._versions.get(alias)
        if not bucket:
            return None
        if version is not None:
            return bucket.get(version)
        return bucket.get(_latest_version(bucket))

    def versions(self, alias: str) -> list[str]:
        """该别名下所有已登记版本（空串表示"无版本"），按版本序。"""
        return list(self._versions.get(alias, {}).keys())

    def snapshot(self) -> list[SkillTrustSnapshot]:
        """所有已登记技能（每个版本各一条）的信任快照。"""
        return [b.trust_snapshot() for bucket in self._versions.values() for b in bucket.values()]

    def has(self, alias: str) -> bool:
        return alias in self._versions

    def aliases(self) -> list[str]:
        """去重后的别名列表（一个别名不管几个版本都只出现一次）。"""
        return list(self._versions.keys())


def _latest_version(bucket: dict[str, _BoundSkill]) -> str:
    """在一组版本的绑定里挑"最新"的那个版本键。

    排序优先级（从高到低）：
      1. 可解析成数字的版本（如 "1.10.0" > "1.9.0"，按数值比较）；
      2. 不可解析的普通字符串（字典序）；
      3. 无版本分支 ("")——永远垫底：有版本时不用它当"最新"。
    """
    def _sort_key(v: str) -> tuple[int, tuple[int, ...]]:
        if v == "":
            return (2, ())
        nums = tuple(int(p) for p in v.split(".") if p.isdigit())
        if nums:
            return (0, nums)
        return (1, ())
    return max(bucket.keys(), key=_sort_key)


def load_skills_from_dir(catalog: SkillCatalog, directory: str) -> int:
    """从目录里加载所有 SKILL.md 文件登记进目录，返回加载数。

    支持两种目录约定（同一技能可多版本并存）：
      - `<directory>/<alias>/SKILL.md`            —— 别名 = 父目录名（无版本）。
      - `<directory>/<alias>/<version>/SKILL.md`  —— 别名 = 上两级目录名，版本 = 父目录名。
    来源记为文件路径（供信任快照审计）。
    """
    import os

    count = 0
    if not os.path.isdir(directory):
        return 0
    parser = SkillPackageParser()
    base = os.path.normpath(directory)
    for root, _dirs, files in os.walk(directory):
        for fname in files:
            if fname != "SKILL.md":
                continue
            path = os.path.join(root, fname)
            rel = os.path.relpath(path, base)
            segments = [s for s in rel.replace("\\", "/").split("/") if s]
            # segments 形如 ["alias","SKILL.md"] 或 ["alias","version","SKILL.md"]
            if len(segments) == 3:
                alias, version_dir, _ = segments
            elif len(segments) == 2:
                alias, _ = segments
                version_dir = ""
            else:
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    content = parser.parse(f.read())
            except OSError:
                continue
            # 版本目录约定且 frontmatter 没写版本时,用目录名补上
            if version_dir and not content.metadata.version:
                content = SkillContent(
                    metadata=replace(content.metadata, version=version_dir),
                    body=content.body, resources=content.resources,
                )
            catalog.load_skill(alias, content, source=path)
            count += 1
    return count
