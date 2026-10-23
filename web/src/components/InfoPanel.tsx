// InfoPanel：侧边栏的信息面板——run 状态、能力(capabilities)、记忆(memory)、健康检查。

import { useCallback, useEffect, useState, type ReactNode } from "react";
import { api } from "../lib/api";
import { toCapabilities, toMemoryCandidates, toMemoryItems, toRunInfos } from "../lib/guards";
import { runDisplayName, statusLabel } from "../lib/labels";
import type { Capabilities, HealthResult, MemoryCandidate, MemoryItem } from "../lib/types";
import { usePolling } from "../lib/usePolling";

export default function InfoPanel({ runId }: { runId: string }) {
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [capsError, setCapsError] = useState<string | null>(null);
  const [capsTick, setCapsTick] = useState(0); // 点「重试」+1，触发重新加载
  const [toolsOpen, setToolsOpen] = useState(false);
  // 对话标题：界面上要显示"用户认得出的那句话"，而不是 `run-mvjzgmos`。
  // 只在本 runId 变化时拉一次（不轮询）：标题来自首条用户消息，落库后不会变。
  const [runTitle, setRunTitle] = useState("");

  useEffect(() => {
    const ctrl = new AbortController();
    let cancelled = false;
    setRunTitle("");
    api
      .runs(undefined, { signal: ctrl.signal })
      .then((raw) => {
        if (cancelled) return;
        const hit = toRunInfos(raw).find((r) => r.run_id === runId);
        setRunTitle(hit?.title ?? "");
      })
      .catch(() => {
        /* 拿不到标题就退回显示 run_id：标题只是锦上添花，不该让整个面板报错 */
      });
    return () => {
      cancelled = true;
      ctrl.abort();
    };
  }, [runId]);

  // 健康检查轮询
  const { data: health, error: healthError } = usePolling<HealthResult>(
    (signal) => api.health({ signal }),
    5000,
  );

  // run 状态轮询（3s）：聊完一轮状态卡能自动跟上，不用手点刷新
  const {
    data: runStatusData,
    error: runStatusError,
    refresh: refreshRunStatus,
  } = usePolling<{ run_id: string; status: string }>(
    (signal) => api.status(runId, { signal }),
    3000,
    runId,
  );
  const runStatus = runStatusData?.status ?? "…";

  // 记忆轮询（5s）：memory.remember 之后卡片能自动跟上
  const {
    data: memData,
    error: memError,
    refresh: refreshMemories,
  } = usePolling<MemoryItem[]>(
    (signal) => api.memory("session", { signal }),
    5000,
    runId,
  );
  const memories = toMemoryItems(memData);

  // 待确认候选轮询（5s）：`memory.remember` 只写候选（不生效），
  // 不把人该做的“确认”摆到界面上，那句话就只是文案——东西确实没生效，而用户不知道。
  const {
    data: candData,
    error: candError,
    refresh: refreshCandidates,
  } = usePolling<MemoryCandidate[]>(
    (signal) => api.memoryCandidates("session", { signal }),
    5000,
    runId,
  );
  const candidates = toMemoryCandidates(candData);

  // 确认/拒绝：单击期间禁用同一候选的两个按钮（双击会发两次请求：确认完再确认=404，冲突项还会弹两次错）
  const [busyUid, setBusyUid] = useState<string | null>(null);
  const [decideError, setDecideError] = useState<string | null>(null);
  const decide = useCallback(
    async (kind: "confirm" | "reject", uid: string) => {
      if (!uid) return;
      setBusyUid(uid);
      setDecideError(null);
      try {
        if (kind === "confirm") await api.confirmMemoryCandidate("session", uid);
        else await api.rejectMemoryCandidate("session", uid);
        // 一次动作会同时改两处：候选区少一条、生效列表多（或少）一条
        await Promise.all([refreshCandidates(), refreshMemories()]);
      } catch (e: unknown) {
        // 失败要说出来：“点了没反应”比报错更难查
        setDecideError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusyUid(null);
      }
    },
    [refreshCandidates, refreshMemories],
  );

  // 工具列表默认只展示 6 个，可展开
  const shownTools = caps ? (toolsOpen ? caps.tools : caps.tools.slice(0, 6)) : [];

  // 能力（runId 变化时加载一次）：带取消信号 + 卸载守卫，
  // 快速切 run 时旧响应不会回来覆盖新 run 的能力列表
  useEffect(() => {
    const ctrl = new AbortController();
    let cancelled = false;
    setCaps(null);
    setCapsError(null);
    api
      .capabilities({ signal: ctrl.signal })
      .then((raw) => {
        if (cancelled) return;
        const parsed = toCapabilities(raw);
        // 整体不是对象（如后端返回数组/字符串）：当加载失败展示，而不是假装空能力
        if (!parsed) setCapsError("响应格式异常");
        else setCaps(parsed);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setCapsError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
      ctrl.abort(); // 取消在途请求，避免旧 run 的响应后到
    };
  }, [runId, capsTick]);

  const retryCaps = useCallback(() => setCapsTick((t) => t + 1), []);

  const statusColor =
    runStatus === "COMPLETED"
      ? "text-warden-ok"
      : runStatus === "FAILED" || runStatus === "CANCELLED"
        ? "text-warden-danger"
        : runStatus === "WAITING_APPROVAL"
          ? "text-warden-warn"
          : "text-warden-fg";

  const healthLabel = healthError
    ? `异常：${healthError}`
    : health
      ? health.status
      : "未知";

  return (
    <div className="flex flex-col gap-3">
      <div className="wpanel p-3.5">
        <div className="flex items-center justify-between">
          <h3 className="flex items-center gap-1.5 text-sm font-medium text-warden-fg">
            <span className="w-2 h-2 rounded-full bg-warden-accent shadow-[0_0_8px_rgba(86,184,201,0.8)]" />
            Run 状态
          </h3>
          <button
            onClick={() => refreshRunStatus()}
            className="text-xs text-warden-accent hover:underline"
          >
            刷新
          </button>
        </div>
        <div className="mt-1 flex items-center gap-2">
          <span className={`text-sm font-medium ${statusColor}`} title={`原始状态：${runStatus || "未知"}`}>
            {statusLabel(runStatus) || "…"}
          </span>
        </div>
        {/* 拉状态失败与"真未知"要能区分开，否则用户只看到永远的「…」 */}
        {runStatusError && (
          <p className="mt-1 text-[11px] text-warden-danger">⚠️ 状态获取失败：{runStatusError}</p>
        )}
        {/* 显示对话标题而不是 `run-xxx`：标题是用户自己写的第一句话，他找得到；
            原始 ID 移进 title 属性，悬停仍可看到（技术用户不会失去它）。 */}
        <div className="mt-1 text-xs text-warden-fg/80 break-all" title={`会话 ID：${runId}`}>
          {runDisplayName(runId, runTitle)}
        </div>
      </div>

      {/* 能力 */}
      <div className="wpanel p-3.5">
        <h3 className="flex items-center gap-1.5 text-sm font-medium text-warden-fg mb-2">
          <span className="w-2 h-2 rounded-full bg-warden-cyan shadow-[0_0_8px_rgba(111,211,199,0.8)]" />
          能力
        </h3>
        {capsError ? (
          <div className="flex items-center gap-2">
            <p className="text-xs text-warden-danger">⚠️ 加载失败：{capsError}</p>
            <button
              onClick={retryCaps}
              className="shrink-0 px-2 py-0.5 rounded border border-warden-accent/50 text-[11px] text-warden-accent hover:bg-warden-accent/10 transition"
            >
              重试
            </button>
          </div>
        ) : !caps ? (
          <p className="text-xs text-warden-fg/70">加载中…</p>
        ) : (
          <>
            <div className="mb-1.5 flex flex-wrap gap-1">
              {caps.features.memory && <Chip>memory</Chip>}
              {/* 只在后端**明确说了**“进程内”时才提醒（null = 未上报，不吓人） */}
              {caps.features.memory && caps.features.memory_persistent === false && (
                <Chip>memory:进程内重启即丢</Chip>
              )}
              {caps.features.web && <Chip>web</Chip>}
              {/* 搜索/抓取是离线演示时必须显式标出：mock 的结果与真结果长得一样，
                  不标出来用户会以为真的联网搜了 */}
              {caps.features.web && caps.features.web_search_mock === true && (
                <Chip>web:离线演示</Chip>
              )}
              {caps.features.model_kind_now === "fake" && <Chip>模型:演示脚本</Chip>}
              {caps.features.skills.map((s) => (
                <Chip key={s}>skill:{s}</Chip>
              ))}
              {caps.features.mcp_server && <Chip>mcp:{caps.features.mcp_server}</Chip>}
            </div>
            {/* 演示档要写清楚“会差在哪、怎么换成真的”，否则用户只会觉得“这东西很差” */}
            {caps.features.web && caps.features.web_search_mock === true && (
              <p className="mb-1.5 text-[11px] leading-relaxed text-amber-300/90">
                ⚠️ 联网搜索/抓取是「离线演示数据」：web.search 只回内置样例，web.fetch 只认预置
                URL（不会真的建网）。
                {caps.features.web_degraded_reason
                  ? ` 原因：${caps.features.web_degraded_reason}。`
                  : ""}
                {" "}要接真实搜索：设 WARDEN_SEARCH_PROVIDER=tavily|brave|custom 与
                WARDEN_SEARCH_API_KEY。
              </p>
            )}
            {/* 演示模型同理：不解释清楚，用户会把脚本回复读成"它真的会思考" */}
            {caps.features.model_kind_now === "fake" && (
              <p className="mb-1.5 text-[11px] leading-relaxed text-amber-300/90">
                ⚠️ 当前模型是「演示脚本」（没配到可用模型 key）：回复内容与工具参数都是写死的，
                不能当作系统能力或回答质量的证据。要接真模型：设 DEEPSEEK_API_KEY，或设
                WARDEN_BASE_URL + WARDEN_MODEL_API_KEY（custom 兼容端点）。
              </p>
            )}
            <div className="mb-1.5 flex flex-wrap gap-1">
              {shownTools.map((t) => (
                <span
                  key={t}
                  className="px-1.5 py-0.5 rounded border border-zinc-700 bg-zinc-800/70 text-[11px] font-mono text-zinc-200"
                >
                  {t}
                </span>
              ))}
            </div>
            {caps.tools.length > 6 && (
              <button
                onClick={() => setToolsOpen((v) => !v)}
                className="text-[11px] text-warden-accent hover:underline"
              >
                {toolsOpen ? "收起" : `展开全部 (${caps.tools.length})`}
              </button>
            )}
          </>
        )}
      </div>

      {/* 记忆 */}
      <div className="wpanel p-3.5">
        <h3 className="flex items-center gap-1.5 text-sm font-medium text-warden-fg mb-2">
          <span className="w-2 h-2 rounded-full bg-sky-300 shadow-[0_0_8px_rgba(125,211,252,0.7)]" />
          记忆(session)
          {candidates.length > 0 && (
            <span className="px-1.5 py-0.5 rounded-full bg-amber-400/15 text-[10px] text-amber-300">
              待确认 {candidates.length}
            </span>
          )}
        </h3>
        {memError ? (
          <p className="text-xs text-warden-danger">⚠️ 记忆加载失败：{memError}</p>
        ) : memories.length === 0 ? (
          <p className="text-xs text-warden-fg/70">（暂无已生效记忆）</p>
        ) : (
          <ul className="flex flex-col gap-1.5">
            {memories.map((m, i) => (
              <li key={m.key || i} className="text-xs leading-relaxed text-warden-fg/85">
                <span className="text-warden-accent">{m.key}</span>：{m.text}
              </li>
            ))}
          </ul>
        )}

        {/* 待确认候选：确认后才生效。这是 memory.remember 的真实语义，
            所以必须看得见、点得动——否则就是“写了但永远不生效”的静默丢弃。 */}
        {candError ? (
          <p className="mt-2 text-[11px] text-warden-danger">⚠️ 待确认列表加载失败：{candError}</p>
        ) : (
          candidates.length > 0 && (
            <div className="mt-2 border-t border-zinc-700/60 pt-2">
              <p className="text-[11px] text-warden-fg/70">待确认（点确认后才生效）：</p>
              <ul className="mt-1.5 flex flex-col gap-2">
                {candidates.map((c) => (
                  <li key={c.uid || `${c.key}|${c.text}`} className="text-xs">
                    <div className="leading-relaxed text-warden-fg/85">
                      <span className="text-warden-accent">{c.key}</span>：{c.text}
                    </div>
                    {c.replaces_text && (
                      <div className="text-[11px] text-amber-300/90">
                        ⚠️ 确认后会替掉现有：{c.replaces_text}
                      </div>
                    )}
                    <div className="mt-1 flex items-center gap-2">
                      <button
                        onClick={() => void decide("confirm", c.uid)}
                        disabled={!c.uid || busyUid === c.uid}
                        className="px-2 py-0.5 rounded border border-warden-ok/50 text-[11px] text-warden-ok hover:bg-warden-ok/10 disabled:opacity-40 transition"
                      >
                        确认
                      </button>
                      <button
                        onClick={() => void decide("reject", c.uid)}
                        disabled={!c.uid || busyUid === c.uid}
                        className="px-2 py-0.5 rounded border border-zinc-600 text-[11px] text-warden-fg/80 hover:bg-zinc-700/40 disabled:opacity-40 transition"
                      >
                        拒绝
                      </button>
                      {!c.uid && (
                        <span className="text-[11px] text-warden-danger">缺候选 id，无法处理</span>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            </div>
          )
        )}
        {decideError && (
          <p className="mt-1.5 text-[11px] text-warden-danger">⚠️ 操作失败：{decideError}</p>
        )}
      </div>

      {/* 健康 */}
      <div className="wpanel p-3.5">
        {/* 状态灯就在标题左边：绿=ok，红=异常；悬停可看原始状态 */}
        <h3 className="flex items-center gap-1.5 text-sm font-medium text-warden-fg">
          <span
            role="img"
            aria-label={`健康状态：${healthLabel}`}
            title={healthLabel}
            className={`inline-block w-2.5 h-2.5 rounded-full ${
              !health
                ? "bg-warden-fg/30"
                : healthError || health.status !== "ok"
                  ? "bg-warden-danger shadow-[0_0_10px_rgba(224,122,106,0.9)]"
                  : "bg-warden-ok shadow-[0_0_10px_rgba(91,191,179,0.9)]"
            }`}
          />
          健康检查
        </h3>
        {healthError && (
          <p className="mt-1 text-[11px] text-warden-danger">⚠️ 健康检查失败：{healthError}</p>
        )}
      </div>
    </div>
  );
}

function Chip({ children }: { children: ReactNode }) {
  return (
    <span className="px-2 py-0.5 rounded-full bg-warden-accent/15 text-warden-accent text-[11px]">
      {children}
    </span>
  );
}
