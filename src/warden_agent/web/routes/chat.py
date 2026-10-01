"""对话与流式路由（从 `web/server.py` 机械拆分而来，handler 逐字照搬）。

拆分说明：`/chat/{run_id}` 与 `/chat/stream/{run_id}`、`/events/{run_id}` 在原文件里
**不相邻**（中间夹着 /runs、/approvals 等），而路由注册顺序是在对外形状的一部分。
所以本模块导出两个注册函数：`register()` 注册 `/chat/{run_id}`，`register_streaming()`
注册 `/chat/stream/{run_id}` 与 `/events/{run_id}`；由 build_app 在正确的位置调用。

唯一的非机械改动：`chat_stream` 的心跳间隔由 `SSE_HEARTBEAT_SECONDS` 这个名字改成
参数 `sse_heartbeat_seconds()`（一个**每次调用时**读取 server 模块全局的取值器）。
原因：原实现读的是 `warden_agent.web.server.SSE_HEARTBEAT_SECONDS` 这个模块全局，
测试用 `monkeypatch.setattr(server_mod, "SSE_HEARTBEAT_SECONDS", 0.05)` 把它改小；
若这里读自己模块的常量副本，那种 monkeypatch 就会失效（心跳退回 10s）——那是可观测的行为变化。
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from warden_agent.runtime.locking import RunLease
from warden_agent.runtime.session import FinalReply, NeedsApproval
from warden_agent.web.coordination import EventBus
from warden_agent.web.http_kit import (
    SseConcurrencyGate,
    _model_kind_headers,
    _serialize_messages,
)
from warden_agent.web.schemas import ChatRequestIn, ChatResponseOut
from warden_agent.web.session_registry import SessionRegistry


def register(
    app: FastAPI,
    *,
    _acquire_run: Callable[[str], RunLease],
    _identity: Callable[[Request, str | None], str],
    bus: EventBus,
    logger: logging.Logger,
    registry: SessionRegistry,
) -> None:
    """注册 `POST /chat/{run_id}`（非流式）。"""

    @app.post("/chat/{run_id}")
    def chat(
        run_id: str,
        body: ChatRequestIn,
        request: Request,
        response: Response,
        user_id: str = "demo-user",
    ) -> ChatResponseOut:
        lease = _acquire_run(run_id)
        try:
            sess = registry.get(run_id)
            if not sess.run.user_id:
                # 首轮对话建立归属：认证模式下身份来自凭证（user_id 参数被忽略）
                sess.run.user_id = _identity(request, user_id)
                # 归属刚定下来 → 把模型同步成"这个用户选的那个"（没选过就是部署默认）
                registry.apply_owner_model(sess)
            # 换成演示模型时，响应要自带"这是演示脚本"标记（见 _model_kind_headers）；
            # 必须放在归属/模型同步**之后**，否则会把刚切过来的假模型漏报。
            response.headers.update(_model_kind_headers(sess.model))
            try:
                outcome = sess.start(body.text)
            except Exception as e:  # 工具未注册 / 被 DENY 等
                logger.exception("chat 失败 run=%s", run_id)
                bus.publish(run_id, {"event": "error", "message": str(e)})
                raise HTTPException(status_code=400, detail=str(e)) from e

            if isinstance(outcome, FinalReply):
                bus.publish(run_id, {"event": "final", "text": outcome.text})
                return ChatResponseOut(
                    run_id=run_id,
                    status=sess.status().name,
                    kind="final",
                    text=outcome.text,
                    messages=_serialize_messages(outcome.messages),
                )
            if isinstance(outcome, NeedsApproval):
                bus.publish(
                    run_id,
                    {"event": "needs_approval", "approval": outcome.approval.tool_name},
                )
                return ChatResponseOut(
                    run_id=run_id,
                    status=sess.status().name,
                    kind="needs_approval",
                    approval={
                        "approval_id": outcome.approval.approval_id,
                        "tool_name": outcome.approval.tool_name,
                        "arguments": outcome.approval.arguments,
                        "reason": outcome.approval.reason,
                    },
                )
            raise HTTPException(status_code=500, detail="未知结果类型")
        finally:
            lease.stop()


def register_streaming(
    app: FastAPI,
    *,
    _acquire_run: Callable[[str], RunLease],
    _identity: Callable[[Request, str | None], str],
    bus: EventBus,
    logger: logging.Logger,
    registry: SessionRegistry,
    sse_gate: SseConcurrencyGate,
    sse_heartbeat_seconds: Callable[[], float],
    sse_max_concurrency: int,
) -> None:
    """注册 `POST /chat/stream/{run_id}` 与 `GET /events/{run_id}`（注册顺序不变）。"""

    @app.post("/chat/stream/{run_id}")
    async def chat_stream(
        request: Request, run_id: str, body: ChatRequestIn, user_id: str = "demo-user"
    ) -> StreamingResponse:
        """流式对话（SSE 打字机）：模型边生成边把增量推给前端。
        前端拿到增量直接渲染，就能看到"逐字打出"的效果。

        ⚠️ 会话驱动**不在**这个 HTTP 生成器里跑（见下面的 drive 线程）：客户端断线
        （切网 / 刷新页面 / 中间层 idle 超时）只让"搬运"停下来，本轮模型调用照常跑完、
        最终回答照常落库，并同样发一份到事件总线——客户端连回 `/events/{run_id}`
        就能续读到**同一份**结果。不这么做的话，"刷新一下页面"就等于整轮回复丢掉，
        而模型调用已经花掉的钱和 token 一分没省。
        """
        # 并发闸门：到上限立刻 503，不排队（排队只会让一堆请求一起超时）。
        if not sse_gate.acquire():
            raise HTTPException(
                status_code=503,
                detail=(
                    f"SSE 长连接已达并发上限 {sse_max_concurrency}，请稍后重试"
                    "（WARDEN_SSE_MAX_CONNECTIONS 可调）"
                ),
            )
        # 流式用**手动 start/stop** 的租约：锁在端点里取（抢不到就地 423），
        # 但必须活到 SSE 流结束——所以释放放在生成器的 finally 里，而不是端点作用域。
        # 租约自带心跳续租，所以"流很久"也不会中途过期被接管。
        try:
            lease = _acquire_run(run_id)
            try:
                sess = registry.get(run_id)
                if not sess.run.user_id:
                    # 归属只能用 `_identity` 派生（认证模式下来自凭证，忽略查询参数），
                    # 与非流式 `/chat` 一致——否则客户端能用 `?user_id=alice` 把消息写进别人名下。
                    sess.run.user_id = _identity(request, user_id)
                    registry.apply_owner_model(sess)  # 用该用户自己选的模型
            except Exception:
                # 建流**之前**出错也必须释放租约：租约带后台心跳续租，漏掉的话这个 run
                # 会被永久占住（一直回 423），直到进程重启。流内的释放见 generate() 的 finally。
                lease.stop()
                raise
        except Exception:
            # 建流前任何失败（含上面抢不到 Run 锁的 423）都要把并发名额还回去。
            sse_gate.release()
            raise

        # 会话驱动与 HTTP 传输解耦：worker 线程只负责"产出帧"，生成器只负责"搬帧"。
        # 队列有界：客户端已经走了就丢弃后续帧（会话照常跑完，不因断线堆积内存）。
        frames: queue.Queue[str | None] = queue.Queue(maxsize=256)
        client_gone = threading.Event()
        worker_done = threading.Event()

        def offer(frame: str | None) -> None:
            """把一帧放进搬运队列；客户端已经走了就丢弃（会话不受影响）。"""
            while True:
                try:
                    frames.put(frame, timeout=0.5)
                    return
                except queue.Full:
                    if client_gone.is_set():
                        return

        def drive() -> None:
            """在后台线程里跑完整轮会话（与客户端是否还在读无关）。"""
            try:
                for event in sess.stream(body.text):
                    # 终态事件同时进事件总线：断线的客户端连回 /events/{run_id}
                    # 就能续读到**同一份**结果（格式与非流式 /chat 保持一致）。
                    etype = event.get("type")
                    if etype == "final":
                        bus.publish(run_id, {"event": "final", "text": event.get("text", "")})
                    elif etype == "needs_approval":
                        bus.publish(
                            run_id,
                            {"event": "needs_approval", "approval": event.get("approval")},
                        )
                    # SSE 格式：每条事件以 "data: <json>\n\n" 结尾
                    offer(f"data: {json.dumps(event, ensure_ascii=False)}\n\n")
            except Exception as e:  # DENY / 工具错误等，以 error 事件结束
                logger.exception("流式 chat 失败 run=%s", run_id)
                bus.publish(run_id, {"event": "error", "message": str(e)})
                err = {"type": "error", "message": str(e)}
                offer(f"data: {json.dumps(err, ensure_ascii=False)}\n\n")
            finally:
                # 整段流式期间都在驱动这个 run，直到这里才释放：提前释放等于开门让人并发写。
                # 并发名额同生共死：与租约一起还回去。
                # 注意这是 **worker** 的 finally，客户端断线不会让它提前触发。
                worker_done.set()
                lease.stop()
                sse_gate.release()
                offer(None)

        threading.Thread(target=drive, name=f"sse-drive-{run_id}", daemon=True).start()

        def generate() -> Any:
            # 断线只停搬运：会话驱动线程继续跑完。
            # 租约与并发名额**不在**这里释放，而在 drive() 的 finally——
            # 否则客户端一断线就等于提前开门让人并发写同一个 run。
            try:
                while True:
                    try:
                        frame = frames.get(timeout=sse_heartbeat_seconds())
                    except queue.Empty:
                        if worker_done.is_set():
                            break
                        # 心跳：一个字节都不吐的空闲期，发一条 SSE 注释行保活。
                        yield ": keep-alive\n\n"
                        continue
                    if frame is None:
                        break
                    yield frame
            finally:
                # 无论是正常收尾还是 GeneratorExit（客户端断开），都标记"没人读了"：
                # drive() 之后产生的帧会被丢掉，不会在队列里无限堆积。
                client_gone.set()

        # 关键响应头：no-cache 防止代理缓冲；X-Accel-Buffering 关掉 nginx 缓冲，
        # 否则增量会被攒住不实时发出（部署必配）。
        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
                # 演示模型的回复也会"逐字打出来"，比非流式更像真的 —— 所以流式
                # 响应同样要带标记（见 _model_kind_headers）。
                **_model_kind_headers(sess.model),
            },
        )

    @app.get("/events/{run_id}")
    async def events(run_id: str) -> StreamingResponse:
        """SSE：监听某会话的事件（最终结果 / 审批请求 / 错误）。

        走可插拔事件总线：单副本是进程内实现（条件变量唤醒、无延迟），
        多副本换成存储实现（多个副本订阅同一张事件表），客户端连任一副本都能收到。
        """
        # 与 /chat/stream 共用同一并发闸门：否则只限 /chat/stream 是形同虚设——
        # 用 /events 一样能无限量开长连接把连接池占满。名额在生成器 finally 里释放
        # （含客户端断开触发的 GeneratorExit）。
        if not sse_gate.acquire():
            raise HTTPException(
                status_code=503,
                detail=(
                    f"SSE 长连接已达并发上限 {sse_max_concurrency}，请稍后重试"
                    "（WARDEN_SSE_MAX_CONNECTIONS 可调）"
                ),
            )

        def generate() -> Any:
            try:
                seq = 0
                while True:
                    items = bus.poll(run_id, seq, timeout=15.0)
                    if not items:
                        yield ": keep-alive\n\n"  # 心跳，防中间层掐连接
                        continue
                    stop = False
                    for s, ev in items:
                        seq = s
                        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                        if ev.get("event") in ("final", "error"):
                            stop = True
                    if stop:
                        break
            finally:
                # 流结束（含客户端断开）就归还并发名额，防泄漏
                sse_gate.release()

        return StreamingResponse(generate(), media_type="text/event-stream")
