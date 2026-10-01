"""会话注册表（从 `web/server.py` 机械拆分而来，逐字照搬）。

管理所有在跑的会话：按 run_id 存 AgentSession，并锁住线程安全。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import Any

from warden_agent.model.model import AgentChatModel
from warden_agent.policy.policy import PolicyEngine
from warden_agent.runtime.session import AgentSession
from warden_agent.store.sqlite import SqliteStore
from warden_agent.tool.catalog import ToolCatalog

logger = logging.getLogger(__name__)

# 会话内存缓存上限：默认 1000。任何 GET /status/<随机 id> 都会建一个 AgentSession，
# 不透顶时内存可被客户端任意 run_id 撑爆；到上限按 LRU 淘汰最久未访问的会话。
DEFAULT_SESSION_CACHE_MAX = 1000


class SessionRegistry:
    """管理所有在跑的会话：按 run_id 存 AgentSession，并锁住线程安全。"""

    def __init__(
        self,
        model: AgentChatModel,
        catalog: ToolCatalog,
        policy: PolicyEngine,
        store: SqliteStore,
        system_prompt: str = "你是一个能使用工具的助手。",
        extra: dict[str, Any] | None = None,
        default_model_id: str = "",
        stability: Any = None,
        planner: Any = None,
        intent: Any = None,
        max_context_chars: int = 0,
        checkpoint_store: Any = None,
        max_sessions: int = DEFAULT_SESSION_CACHE_MAX,
        session_ttl_s: float = 0.0,
    ) -> None:
        self._model = model
        self._default_model_id: str = default_model_id
        # 按归属者（用户）记的模型选择：多用户下不能让任一个人切走所有人的模型
        # （那是跨租户影响：全体对话改走他的 key，或被他切成离线假模型）。
        self._owner_models: dict[str, tuple[str, AgentChatModel]] = {}
        self._catalog = catalog
        self._policy = policy
        self._store = store
        self._system_prompt = system_prompt
        self._stability = stability
        self._planner = planner
        self._intent = intent
        self._max_context_chars = max_context_chars
        self._checkpoint_store = checkpoint_store
        self.extra = extra or {}  # 额外能力（如 memory_service / skill_catalog）
        # 有界 LRU 缓存：`_sessions` 是 OrderedDict（队尾=最近访问），`_max_sessions`
        # 到上限时淘汰队首（最久未访问）。`_session_seen` 记访问时刻供 TTL 淘汰。
        # 为什么必须有界：任何 `GET /status/<随机 id>` 都会新建一个 AgentSession，
        # 不限的话客户端用一个循环就能把内存撑爆。
        self._sessions: OrderedDict[str, AgentSession] = OrderedDict()
        self._session_seen: dict[str, float] = {}
        self._max_sessions = max_sessions
        self._session_ttl_s = session_ttl_s
        # 防 run_id 抢注：首个“看到”某个 Run 的 principal（进程内）。
        # 客户端自带的 run_id 是先到先得，不记下来就能被另一 principal 后手接管。
        self._created_by: dict[str, str] = {}
        # RLock（可重入）：`get()` 持锁时还要调 `model_for()` 解析归属者模型，
        # 用普通 Lock 会自锁死。
        self._lock = threading.RLock()

    def note_creator(self, run_id: str, principal: str) -> None:
        """记下某个 Run 的首个创建者（首次生效，后续调用不覆盖）。"""
        with self._lock:
            self._created_by.setdefault(run_id, principal)

    def creator_of(self, run_id: str) -> str | None:
        """某个 Run 在**本进程内**被谁首先创建；没记过则 None。"""
        with self._lock:
            return self._created_by.get(run_id)

    def _evict_stale(self) -> None:
        """清掉超过 TTL 未访问的会话（TTL<=0 则不做）。调用方须持锁。"""
        if self._session_ttl_s <= 0:
            return
        now = time.monotonic()
        for run_id in [
            rid for rid, ts in self._session_seen.items()
            if now - ts > self._session_ttl_s
        ]:
            self._evict(run_id)

    def _evict_overflow(self) -> None:
        """超过容量上限就淘汰队首（最久未访问）；`max_sessions<=0` = 不限。须持锁。"""
        if self._max_sessions <= 0:
            return
        while len(self._sessions) > self._max_sessions:
            run_id, sess = self._sessions.popitem(last=False)
            self._session_seen.pop(run_id, None)
            self._close_session(sess, run_id)

    def _evict(self, run_id: str) -> None:
        """按 run_id 淘汰一个会话（含关闭）。须持锁。"""
        sess = self._sessions.pop(run_id, None)
        if sess is not None:
            self._session_seen.pop(run_id, None)
            self._close_session(sess, run_id)

    def _close_session(self, sess: AgentSession, run_id: str) -> None:
        """关闭被淘汰的会话（释放它持有的资源）；关闭失败不得影响缓存淘汰。

        注意：会话持有的模型/存储/记忆是**共享**的，`AgentSession.close()` 只做标记；
        这里把“关闭”做成一个可覆盖的钩子（鸭子类型），方便扩展与测试。
        """
        close = getattr(sess, "close", None)
        if not callable(close):
            return
        try:
            close()
        except Exception:  # noqa: BLE001 - 关闭失败不能拖垮请求
            logger.warning("关闭被淘汰的会话失败 run=%s", run_id, exc_info=True)

    def get(self, run_id: str) -> AgentSession:
        """取会话；没有就基于数据库恢复/新建一个。"""
        with self._lock:
            self._evict_stale()
            sess = self._sessions.get(run_id)
            if sess is None:
                sess = AgentSession(
                    run_id=run_id,
                    model=self._model,
                    catalog=self._catalog,
                    policy_engine=self._policy,
                    store=self._store,
                    system_prompt=self._system_prompt,
                    stability=self._stability,
                    # 认知能力：与 AgentLoop 共用 loop/cognition.py 同一实现，
                    # 所以"记忆按需取用 / 阶段规划 / 意图路由 / 上下文裁剪"在产品路径也生效
                    planner=self._planner,
                    intent=self._intent,
                    memory=self.extra.get("memory_service"),
                    memory_scope=self.extra.get("memory_scope"),
                    max_context_chars=self._max_context_chars,
                    checkpoint_store=self._checkpoint_store,
                )
                self._sessions[run_id] = sess
            else:
                # 已有会话：按它的归属者解析模型（这也是 approve/reject 等驱动路径
                # 不需要各自再解析一次的原因）
                sess.model = self.model_for(sess.run.user_id or None)[1]
            # LRU：本次访问移到队尾；记下访问时刻供 TTL 淘汰；再按上限淘汰最久未访问的。
            self._sessions.move_to_end(run_id)
            self._session_seen[run_id] = time.monotonic()
            self._evict_overflow()
            return sess

    def remove(self, run_id: str) -> None:
        """把会话从内存下线（删除会话时用，数据库由调用方清理）。"""
        with self._lock:
            self._sessions.pop(run_id, None)
            self._session_seen.pop(run_id, None)
            self._created_by.pop(run_id, None)

    def run_ids(self) -> list[str]:
        """当前在内存里的会话 id 快照（遍历用；不要在调用期间持有锁）。"""
        with self._lock:
            return list(self._sessions.keys())

    def set_model(self, model: AgentChatModel, *, model_id: str = "",
                  owner: str | None = None) -> None:
        """切换模型。

        `owner=None` → 部署默认（所有会话）；给定时 → **只切该归属者的会话**，
        并把选择记在该用户名下。多用户部署里普通用户只能影响自己的会话。
        """
        with self._lock:
            if owner is None:
                self._model = model
                self._default_model_id = model_id
            else:
                self._owner_models[owner] = (model_id, model)
            for sess in self._sessions.values():
                if owner is None or sess.run.user_id == owner:
                    sess.model = model

    def model_for(self, owner: str | None = None) -> tuple[str, AgentChatModel]:
        """解析某归属者当前该用的 (model_id, model)：没选过就回落到部署默认。"""
        with self._lock:
            if owner:
                got = self._owner_models.get(owner)
                if got is not None:
                    return got
            return (self._default_model_id, self._model)

    def apply_owner_model(self, sess: AgentSession) -> None:
        """把会话的模型同步成"它的归属者选的那个"（首次绑定归属后调一次）。"""
        sess.model = self.model_for(sess.run.user_id or None)[1]
