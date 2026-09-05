"""记忆服务 —— Agent 记忆的入口：提议、确认、检索、冲突消解、过期清理、审计。

设计要点：
  1. 候选流：Agent 不会直接把话写进记忆，而是先 propose 成 PENDING 候选，
     由"人工/策略" approve 后才 ACTIVE。避免记忆被随口污染。
  2. 冲突消解：同一 scope:key 下若已有内容，新提议标记 CONFLICTED，等 resolve 决定
     是 supersede（替代）还是保留旧的。
  3. 过期清理：带 expires_at 的记忆到期后视为过期，purge 时物理清除。
  4. 审计：每次 create/propose/approve/reject/expire/purge 都记进 audit。
  5. 作用域：Run / Session / User 三级，检索按 scope + 权限过滤（只给当前上下文该看的）。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

from warden_agent.memory.models import (
    MemoryActor,
    MemoryContent,
    MemoryItem,
    MemoryScope,
    MemoryStatus,
    new_uid,
)
from warden_agent.memory.store import MemoryRepository


@dataclass
class MemoryProposal:
    """一条待确认的记忆提议。approve/reject 它。"""

    item: MemoryItem
    reason: str = ""


class MemoryService:
    """记忆中枢。对外：propose / approve / reject / recall / resolve / purge。"""

    def __init__(self, repository: MemoryRepository) -> None:
        self._repo = repository
        self._actor: MemoryActor = MemoryActor(kind="service", id="default")

    # ---- 提议（候选流）----
    def propose(
        self,
        scope: MemoryScope,
        key: str,
        content: MemoryContent,
        *,
        actor: MemoryActor | None = None,
        expires_at: _dt.datetime | None = None,
        reason: str = "",
    ) -> MemoryProposal:
        """把一条记忆放进"候选区"（PENDING），等 approve 才生效。

        若已有同 key 有效记忆，则新候选标 CONFLICTED（冲突），
        由 resolve_conflicts / approve 决定是否替代。
        """
        existing = self._repo.latest(scope, key)
        status = MemoryStatus.PENDING
        conflicts_with = None
        if existing is not None and existing.is_active:
            status = MemoryStatus.CONFLICTED
            conflicts_with = existing.uid

        item = MemoryItem(
            uid=new_uid(),
            scope=scope,
            key=key,
            content=content,
            status=status,
            actor=actor or self._actor,
            expires_at=expires_at,
            conflicts_with=conflicts_with,
        )
        item.record("propose", item.actor)
        self._repo.save(item)
        return MemoryProposal(item=item, reason=reason)

    def approve(self, proposal: MemoryProposal, replacing: bool = True) -> MemoryItem:
        """确认一条候选记忆为有效。若有冲突且 replacing=True，替代旧记忆（旧置 TOMBSTONED）。"""
        item = proposal.item
        if replacing and item.conflicts_with:
            old = self._repo.find(item.conflicts_with)
            if old is not None and old.status != MemoryStatus.TOMBSTONED:
                old.status = MemoryStatus.TOMBSTONED
                old.supersedes = None
                old.record("superseded", self._actor)
                self._repo.save(old)
                item.supersedes = old.uid
        item.status = MemoryStatus.ACTIVE
        item.conflicts_with = None  # 已解决
        item.record("approve", self._actor)
        self._repo.save(item)
        return item

    def reject(self, proposal: MemoryProposal) -> None:
        """拒绝候选：直接软删除（TOMBSTONED），不入库记忆。"""
        item = proposal.item
        item.status = MemoryStatus.TOMBSTONED
        item.record("reject", self._actor)
        self._repo.save(item)

    # ---- 检索（按权限过滤）----
    def recall(self, scope: MemoryScope, key: str | None = None,
               text_like: str | None = None, limit: int = 20) -> list[MemoryItem]:
        """取回当前作用域下有效的记忆。key 给定时取该键最新；否则按文本模糊搜。

        这是 Agent 查记忆的主入口：它只会看到 ACTIVE 且未过期的（权限过滤）。
        """
        if key is not None:
            item = self._repo.latest(scope, key)
            return [item] if item is not None and self._is_usable(item) else []
        return [i for i in self._repo.search(scope, text_like, limit)
                if self._is_usable(i)]

    def recall_text(self, scope: MemoryScope, key: str | None = None,
                    text_like: str | None = None) -> str:
        """把命中的记忆拼成一段可读文本，方便塞给模型。"""
        items = self.recall(scope, key=key, text_like=text_like)
        if not items:
            return ""
        return "\n".join(
            f"- [{i.scope.name}:{i.key}] {i.content.text}" for i in items
        )

    def pending(self, scope: MemoryScope | None = None) -> list[MemoryProposal]:
        """列出待确认的候选提案（供人工/策略 review）。scope 给定时只看该作用域。"""
        scopes = ([scope] if scope is not None
                  else [MemoryScope.USER, MemoryScope.SESSION, MemoryScope.RUN,
                        MemoryScope.WORKSPACE])
        out = []
        for scope_enum in scopes:
            for item in self._repo.search(scope_enum, None, 100):
                if item.status == MemoryStatus.PENDING:
                    out.append(MemoryProposal(item))
        return out

    # ---- 冲突消解 ----
    def resolve_conflict(self, proposal: MemoryProposal, keep_new: bool) -> None:
        """处理冲突候选：keep_new=True 替代旧的；False 放弃新的保留旧的。"""
        if keep_new:
            self.approve(proposal, replacing=True)
        else:
            self.reject(proposal)

    # ---- 过期与清理 ----
    def request_purge(self, scope: MemoryScope | None = None) -> int:
        """标记过期的记忆中已过期项的数量（统计，不物理删）。"""
        now = _dt.datetime.now(_dt.UTC)
        count = 0
        scopes = ([scope] if scope is not None
                  else [MemoryScope.USER, MemoryScope.SESSION, MemoryScope.RUN,
                        MemoryScope.WORKSPACE])
        for s in scopes:
            for item in self._repo.search(s, None, 1000):
                if item.expires_at is not None and now >= item.expires_at:
                    if item.status != MemoryStatus.EXPIRED:
                        item.status = MemoryStatus.EXPIRED
                        item.record("expire", self._actor)
                        self._repo.save(item)
                    count += 1
        return count

    def execute_purge(self) -> int:
        """物理清除所有过期 / tombstone 的记忆，返回清除条数。"""
        removed = 0
        for s in [MemoryScope.USER, MemoryScope.SESSION, MemoryScope.RUN,
                  MemoryScope.WORKSPACE]:
            for item in self._repo.search(s, None, 10000):
                if item.status in (MemoryStatus.EXPIRED, MemoryStatus.TOMBSTONED):
                    item.record("purge", self._actor)
                    item.status = MemoryStatus.TOMBSTONED
                    self._repo.save(item)
                    removed += 1
        return removed

    # ---- 内部 ----
    def _is_usable(self, item: MemoryItem) -> bool:
        """一条记忆此刻能否被检索到：必须 ACTIVE 且未过期。"""
        if item.status != MemoryStatus.ACTIVE:
            return False
        return item.expires_at is None or _dt.datetime.now(_dt.UTC) < item.expires_at
