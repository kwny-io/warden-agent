// ConversationList：左侧对话栏——历史对话一览，点条目切换会话。
// 5s 轮询 /runs；带最后活跃时间戳和状态彩点。

import { useCallback, useEffect, useRef, useState, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent } from "react";
import type { RunInfo } from "../lib/types";
import { toRunInfos } from "../lib/guards";
import { fmtTime, runDisplayName } from "../lib/labels";
import { usePolling } from "../lib/usePolling";
import { api } from "../lib/api";

/** 错误对象 → 一行能显示给人看的字 */
function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/** 长按多久进入勾选模式。按下后**手指/鼠标不动**才算长按；
 *  一旦移动超过 `MOVE_TOLERANCE` 就判成滚动/误触，立刻取消。 */
export const LONG_PRESS_MS = 450;
const MOVE_TOLERANCE = 6;

/** 指针下面那一行的 run_id。jsdom 没实现 elementFromPoint，测试里返回 null。 */
function rowIdAt(x: number, y: number): string | null {
  try {
    const el = document.elementFromPoint(x, y) as HTMLElement | null;
    return el?.closest<HTMLElement>("[data-run-id]")?.dataset.runId ?? null;
  } catch {
    return null;
  }
}

const STATUS_DOT: Record<string, string> = {
  COMPLETED: "bg-warden-ok",
  WAITING_APPROVAL: "bg-warden-warn",
  FAILED: "bg-warden-danger",
  CANCELLED: "bg-warden-danger",
  TIMED_OUT: "bg-warden-danger",
  RUNNING: "bg-warden-accent animate-pulse",
};

export default function ConversationList({
  userId,
  activeRunId,
  onSwitch,
  onCollapse,
  createOpen,
  onOpenCreate,
  onCreateDone,
  onDeleted,
}: {
  userId: string; // 当前中控台账号，对话列表按它过滤
  activeRunId: string;
  onSwitch: (runId: string) => void;
  onCollapse: () => void;
  createOpen: boolean; // 创建输入行是否展开（左栏按钮 / 顶栏 ＋ 共用）
  onOpenCreate: () => void;
  onCreateDone: () => void;
  /** 参数是**数组**：单删是长度 1 的数组，批量删是 N 个。上层无需分两条路径。 */
  onDeleted: (deletedIds: string[], remaining: RunInfo[]) => void;
}) {
  // error 必须接住：拉取失败时 data 是 null（或上一次的旧值）。
  // 不接住就会把「没拉到」显示成「还没有对话」——那是一句在那一刻为假的话。
  const { data, error, refresh } = usePolling<RunInfo[]>(
    (signal) => api.runs(userId, { signal }),
    5000,
    userId, // 换账号立刻重拉并清空旧列表
  );
  // 畸形元素（缺 run_id / 非对象）由 toRunInfos 滤掉，不让 `r.run_id` 炸渲染
  const runs = toRunInfos(data);
  // 拉取成功但形状不对（后端契约变了 / 网关兜底体）：这不是「没有对话」，得说出来
  const shapeBad = data !== null && !Array.isArray(data);
  const [newId, setNewId] = useState("");
  const [busy, setBusy] = useState(false);
  // 动作类失败（创建 / 删除）原来是静默的：catch 里 refresh 一下就完了，
  // 用户点了没反应，只能怀疑自己没点中。
  const [actionError, setActionError] = useState<string | null>(null);

  // 勾选模式：常态下左栏就是列表（点一下切会话），**长按**某一行才进入勾选模式。
  // 为什么不做成"永远显示复选框"：那会让每一次切会话都多一个可误点的目标，
  // 而批量删除是低频动作——低频动作应当显式进入，高频动作不该被它拖累。
  // 为什么不留一个「批量」按钮：那是面板头部一个常驻的、和主流程无关的控件，
  // 每次看一眼列表都要先略过它。长按是"就地表达意图"，不占任何常驻位置。
  const [selectMode, setSelectMode] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  // 正在长按的那一行（只为视觉反馈：按住的这一行要立刻有点变化，
  // 否则用户在 450ms 里得不到任何反馈，会以为没按中而松手）。
  const [pressing, setPressing] = useState<string | null>(null);

  // 一次指针交互的状态。放在 ref 里而不是 state：pointermove 每秒几十次，
  // 走 state 会把整张列表重渲染几十次，而它唯一的作用是"这一段拖拽要不要刷"。
  const session = useRef<{ x0: number; y0: number; value: boolean; painting: boolean } | null>(null);
  const timer = useRef<number | null>(null);
  // 长按/拖拽结束的那一次 click 必须吞掉：否则它会顺手把当前会话切走，
  // 或在勾选模式下把刚画上的勾又翻回去。
  const swallowClick = useRef(false);

  const clearTimer = useCallback(() => {
    if (timer.current !== null) {
      clearTimeout(timer.current);
      timer.current = null;
    }
  }, []);

  // 只在值真的变了时才产生新 Set：拖过同一行几十次不该几十次重渲染。
  const applyPaint = useCallback((runId: string, on: boolean) => {
    setSelected((prev) => {
      if (prev.has(runId) === on) return prev;
      const next = new Set(prev);
      if (on) next.add(runId);
      else next.delete(runId);
      return next;
    });
  }, []);

  // 拖拽期间监听 window：指针滑出这一行（甚至滑出列表）也要继续生效，
  // 而 rowIdAt 用 elementFromPoint 判断"现在压在哪一行上"。
  useEffect(() => {
    const onMove = (e: PointerEvent) => {
      const s = session.current;
      if (!s) return;
      if (!s.painting) {
        // 还没按够时间就动了 → 这是滚动或误触，不是长按。立刻取消，别在用户
        // 想滚列表的时候突然弹出勾选条。
        if (Math.hypot(e.clientX - s.x0, e.clientY - s.y0) > MOVE_TOLERANCE) {
          clearTimer();
          session.current = null;
          setPressing(null);
        }
        return;
      }
      const id = rowIdAt(e.clientX, e.clientY);
      if (id) applyPaint(id, s.value);
    };
    const onUp = () => {
      clearTimer();
      session.current = null;
      setPressing(null);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
    };
  }, [applyPaint, clearTimer]);

  useEffect(() => () => clearTimer(), [clearTimer]);

  // 列表刷新后剔除已经不存在的 id：否则"全选 → 删掉 → 列表变了"会剩下一堆
  // 指向已删会话的幽灵勾选，界面上那个数字对不上任何一行。
  const liveIds = runs.map((r) => r.run_id);
  const selectedLive = [...selected].filter((id) => liveIds.includes(id));
  const allChecked = liveIds.length > 0 && selectedLive.length === liveIds.length;

  const toggleOne = (runId: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(runId)) next.delete(runId);
      else next.add(runId);
      return next;
    });
  };

  const toggleAll = () => {
    setSelected(allChecked ? new Set() : new Set(liveIds));
  };

  const exitSelect = () => {
    setSelectMode(false);
    setSelected(new Set());
    setPressing(null);
    session.current = null;
    clearTimer();
  };

  // 指针落在某一行上：start = 开始计时长按；进入勾选模式后就是"开始画"。
  // 方向在按下的那一刻定死（这一勾还是这一抹取消），拖到一半不翻向——
  // 否则手一抖就变成"勾了又取消"，用户完全无法预测结果。
  const onRowPointerDown = (e: ReactPointerEvent, runId: string) => {
    if (e.button !== 0) return; // 只认左键/主指针
    swallowClick.current = false;
    if (selectMode) {
      const on = !selected.has(runId);
      session.current = { x0: e.clientX, y0: e.clientY, value: on, painting: true };
      // 勾在按下那一刻就画上（拖拽的起点也算选中），因此松手的那次 click
      // 必须吞掉——它会把刚画上的勾又翻回去。
      swallowClick.current = true;
      applyPaint(runId, on);
      return;
    }
    session.current = { x0: e.clientX, y0: e.clientY, value: true, painting: false };
    setPressing(runId);
    clearTimer();
    timer.current = window.setTimeout(() => {
      timer.current = null;
      const s = session.current;
      if (!s || s.painting) return;
      s.painting = true;
      swallowClick.current = true; // 长按命中的那一下不应再被当成"点击"
      setSelectMode(true);
      setPressing(null);
      setSelected(new Set([runId]));
    }, LONG_PRESS_MS);
  };

  // 创建会话：用用户输入的 ID 建档（幂等接口），成功后切换过去
  const create = async () => {
    const id = newId.trim();
    if (!id || busy) return;
    setBusy(true);
    setActionError(null);
    try {
      await api.createRun(id);
      refresh();
      onSwitch(id);
      setNewId("");
      onCreateDone();
    } catch (e) {
      setActionError(`创建失败：${errText(e)}`);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (e: ReactMouseEvent, r: RunInfo) => {
    e.stopPropagation(); // 别触发切换会话
    if (!window.confirm(`删除对话「${runDisplayName(r.run_id, r.title)}」？记录不可恢复。`))
      return;
    setActionError(null);
    try {
      await api.deleteRun(r.run_id);
      refresh();
      const remaining = runs.filter((x) => x.run_id !== r.run_id);
      onDeleted([r.run_id], remaining);
    } catch (e) {
      // 删不掉也说一声：原来静默 refresh 会让「删了还在」看起来像界面 bug
      setActionError(`删除失败：${errText(e)}`);
      refresh();
    }
  };

  // 批量删除：确认框里必须写出**数量**——「确定删除？」在删 1 条和删 30 条时
  // 是完全不同的决定，用户看不到数量就等于盲签。
  const removeSelected = async () => {
    const ids = selectedLive;
    if (ids.length === 0 || busy) return;
    if (!window.confirm(`删除选中的 ${ids.length} 个对话？记录不可恢复。`)) return;
    setBusy(true);
    setActionError(null);
    try {
      await api.deleteRuns(ids);
      exitSelect();
      refresh();
      const remaining = runs.filter((x) => !ids.includes(x.run_id));
      onDeleted(ids, remaining);
    } catch (e) {
      // 后端是「全删或全不删」，所以这里不能 refresh 后就当成功了：
      // 用户必须知道**一个都没删**，否则会以为部分已删而不再重试。
      setActionError(`批量删除失败（一个都没删）：${errText(e)}`);
      refresh();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="w-full flex flex-col gap-3 min-h-0">
      <div className="glass rounded-2xl p-3 flex flex-col gap-2.5 flex-1 min-h-0 relative">
        {/* 面板头部 */}
        <div className="flex items-center gap-1.5 px-0.5 pb-2 border-b border-white/[0.06]">
          <span className="w-2 h-2 rounded-full bg-warden-cyan shadow-[0_0_10px_rgba(111,211,199,0.9)]" />
          <span className="text-sm font-medium text-zinc-100">对话</span>
          {runs.length > 0 && (
            <span className="text-[10px] font-mono text-zinc-500">{runs.length}</span>
          )}
          <button
            onClick={onCollapse}
            title="收起对话栏"
            aria-label="收起对话栏"
            className="ml-auto w-6 h-6 rounded-md text-warden-fg/40 hover:text-warden-fg hover:bg-white/[0.06] transition"
          >
            ⟨
          </button>
        </div>

        {/* 勾选模式的操作条：全选 + 删除（带数量）+ 已选计数 + 退出。
            只有长按进入勾选模式才出现。
            **必须浮在列表上、不能挤占布局**：长按成功的那一刻行会插入，若它参与
            文档流，整列会往下窜三十几个像素——手指按住的那一行从指下溜走，
            接着往下的"滑选"就会从错误的位置开始。实测位移 33px。
            「退出」从面板头部挪到这里：它只在勾选模式里有意义，跟全选/删除是一件事。 */}
        {selectMode && (
          <div className="absolute left-2 right-2 bottom-2 z-20 flex items-center gap-2 px-2 py-1.5 rounded-lg text-[11px] bg-warden-bg/95 backdrop-blur border border-warden-accent/30 shadow-lg">
            <label className="flex items-center gap-1 text-warden-fg/70 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={allChecked}
                onChange={toggleAll}
                aria-label="全选"
                className="accent-warden-accent"
              />
              全选
            </label>
            <span className="text-warden-fg/40">已选 {selectedLive.length}</span>
            <button
              onClick={exitSelect}
              title="退出勾选"
              className="ml-auto px-1.5 py-0.5 rounded border border-slate-600/60 text-warden-fg/60 hover:border-warden-accent/70 hover:text-warden-fg transition"
            >
              取消
            </button>
            <button
              onClick={() => void removeSelected()}
              disabled={selectedLive.length === 0 || busy}
              className="px-2 py-0.5 rounded border border-warden-danger/60 text-warden-danger hover:bg-warden-danger/10 disabled:opacity-40 disabled:hover:bg-transparent transition"
            >
              删除{selectedLive.length > 0 ? ` (${selectedLive.length})` : ""}
            </button>
          </div>
        )}

      {/* 拉取失败：唯一的错误出口（顶部横幅 + 可点的重试）。
          有旧数据时额外说明“下面看到的不是最新的”，因为屏幕上那几行仍然是真的。 */}
      {error && (
        <p className="shrink-0 text-[11px] text-warden-danger bg-warden-danger/10 rounded px-2 py-1 flex items-start gap-1.5">
          <span className="flex-1">
            ⚠️ 加载对话列表失败：{error}
            {runs.length > 0 && "（下面显示的是上一次的结果）"}
          </span>
          <button onClick={refresh} className="underline shrink-0">重试</button>
        </p>
      )}

      {/* 创建 / 删除失败：不确定的话不能说“成功” */}
      {actionError && (
        <p className="shrink-0 text-[11px] text-warden-danger bg-warden-danger/10 rounded px-2 py-1">
          ⚠️ {actionError}
        </p>
      )}

      <button
        onClick={onOpenCreate}
        className="btn-sheen shrink-0 rounded-lg border border-warden-accent/40 px-3 py-2 text-sm text-warden-fg hover:border-warden-accent/80 transition"
      >
        ＋ 新对话
      </button>

      {/* 创建输入行：自己输入会话 ID，点「创建」建档并切换 */}
      {createOpen && (
        <div className="shrink-0 flex gap-1">
          <input
            autoFocus
            value={newId}
            onChange={(e) => setNewId(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && create()}
            aria-label="新会话 ID"
            placeholder="输入 USER_ID…"
            className="flex-1 min-w-0 bg-black/30 border border-white/10 rounded-lg px-2.5 py-1.5 text-[13px] font-mono outline-none focus:border-warden-accent/70"
          />
          <button
            onClick={create}
            disabled={busy || !newId.trim()}
            className="px-2.5 rounded-lg border border-warden-accent/50 text-[13px] text-warden-accent hover:bg-warden-accent/10 disabled:opacity-40 transition"
          >
            创建
          </button>
        </div>
      )}

      {/* pb 只在勾选模式加：浮层压在列表底部，不给留白就会盖住最后几行。
          加 padding-bottom 不会移动已有行（只有滚到底才生效），所以长按那一瞬
          列表仍然不动。 */}
      <div className={`flex-1 min-h-0 overflow-y-auto flex flex-col pr-0.5 ${selectMode ? "pb-11" : ""}`}>
        {runs.length === 0 &&
          (shapeBad ? (
            <p className="text-xs text-warden-danger text-center mt-4">
              ⚠️ 后端返回的对话列表不是数组（契约变了？）
            </p>
          ) : error ? null : ( // 拉取失败时上面那条横幅已经说过了：这里绝不再说「还没有对话」
            <p className="text-xs text-warden-fg/35 text-center mt-4">还没有对话</p>
          ))}
          {runs.map((r) => {
            const active = r.run_id === activeRunId;
            const checked = selected.has(r.run_id);
            return (
              <div
                key={r.run_id}
                data-run-id={r.run_id}
                className={`group relative w-full text-left border-l-2 transition-colors ${
                  active ? "border-warden-accent" : "border-transparent hover:border-slate-600/60"
                } ${pressing === r.run_id ? "bg-white/[0.06]" : ""}`}
              >
                <button
                  onPointerDown={(e) => onRowPointerDown(e, r.run_id)}
                  // 长按进入勾选模式、或勾选模式下拖拽画勾：这一下 click 不该再
                  // 切会话/翻勾，否则长按松手的瞬间就把状态改回去了。
                  onClick={() => {
                    if (swallowClick.current) {
                      swallowClick.current = false;
                      return;
                    }
                    if (selectMode) toggleOne(r.run_id);
                    else onSwitch(r.run_id);
                  }}
                  // 长按是"另一条路"，不是点击的补充：阻止默认的文本选择/拖拽，
                  // 否则按住 450ms 会顺带选中这一行的文字，看起来像界面坏了。
                  onDragStart={(e) => e.preventDefault()}
                  aria-pressed={selectMode ? checked : undefined}
                  title={
                    selectMode
                      ? `${runDisplayName(r.run_id, r.title)}（点击${checked ? "取消勾选" : "勾选"}）`
                      : `${runDisplayName(r.run_id, r.title)}（长按可多选）`
                  }
                  // 不用 touch-none：那会把触屏上的滚动一并废掉（列表滚不动比
                  // 手势少一种严重得多）。鼠标拖拽不受 touch-action 影响，桌面端照常可用。
                  className="w-full text-left px-2.5 py-1.5 block select-none"
                >
                  <div className="flex items-center gap-1.5">
                    {selectMode && (
                      // 复选框只是观感，点击落在外层 button 上：一个 label 套 input 会在
                      // 冒泡时同时触发 input 与 button 两遍 toggle（勾上又立刻取消）。
                      <span
                        aria-hidden="true"
                        className={`w-3 h-3 shrink-0 rounded-sm border flex items-center justify-center text-[9px] leading-none ${
                          checked
                            ? "border-warden-accent bg-warden-accent/20 text-warden-accent"
                            : "border-slate-600"
                        }`}
                      >
                        {checked ? "✓" : ""}
                      </span>
                    )}
                    <span
                      className={`w-1.5 h-1.5 shrink-0 rounded-full ${
                        STATUS_DOT[r.status] ?? "bg-warden-fg/30"
                      }`}
                    />
                    <span
                      className={`text-[13px] truncate flex-1 ${
                        active ? "text-warden-fg" : "text-warden-fg/70"
                      }`}
                    >
                      {runDisplayName(r.run_id, r.title)}
                    </span>
                  </div>
                  <div className="mt-1 pl-3 text-[11px] font-mono text-warden-fg/40 leading-relaxed truncate">
                    {fmtTime(r.updated_at) || "—"} · {r.msg_count} 条 · {r.run_id}
                  </div>
                  {/* 分割线：两边浅中间深的渐变 */}
                  <div className="mt-1.5 h-px bg-gradient-to-r from-transparent via-zinc-400/60 to-transparent" />
                </button>
                {/* 单删按钮在勾选模式下隐藏：两种删除入口同时在场，用户会分不清
                    「点删除到底删 1 个还是删勾选的那些」。 */}
                {!selectMode && (
                  <button
                    onClick={(e) => remove(e, r)}
                    title="删除对话"
                    aria-label="删除对话"
                    className="absolute top-1.5 right-1 w-5 h-5 rounded text-[11px] leading-none text-zinc-600 opacity-0 group-hover:opacity-100 hover:text-warden-danger hover:bg-white/[0.06] transition"
                  >
                    🗑
                  </button>
                )}
              </div>
            );
          })}
      </div>
      </div>
    </div>
  );
}
