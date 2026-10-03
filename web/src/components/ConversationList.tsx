// ConversationList：左侧对话栏——历史对话一览，点条目切换会话。
// 5s 轮询 /runs；带最后活跃时间戳和状态彩点。

import { useState, type MouseEvent as ReactMouseEvent } from "react";
import type { RunInfo } from "../lib/types";
import { toRunInfos } from "../lib/guards";
import { usePolling } from "../lib/usePolling";
import { api } from "../lib/api";

/** 错误对象 → 一行能显示给人看的字 */
function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

const STATUS_DOT: Record<string, string> = {
  COMPLETED: "bg-warden-ok",
  WAITING_APPROVAL: "bg-warden-warn",
  FAILED: "bg-warden-danger",
  CANCELLED: "bg-warden-danger",
  TIMED_OUT: "bg-warden-danger",
  RUNNING: "bg-warden-accent animate-pulse",
};

/** ISO 时间 → 本地 "MM-DD HH:mm"；无时间（老数据）返回空串 */
function fmtTime(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

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
  onDeleted: (deletedId: string, remaining: RunInfo[]) => void;
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
    if (!window.confirm(`删除对话「${r.title || r.run_id}」？记录不可恢复。`)) return;
    setActionError(null);
    try {
      await api.deleteRun(r.run_id);
      refresh();
      const remaining = runs.filter((x) => x.run_id !== r.run_id);
      onDeleted(r.run_id, remaining);
    } catch (e) {
      // 删不掉也说一声：原来静默 refresh 会让「删了还在」看起来像界面 bug
      setActionError(`删除失败：${errText(e)}`);
      refresh();
    }
  };

  return (
    <div className="w-full flex flex-col gap-3 min-h-0">
      <div className="glass rounded-2xl p-3 flex flex-col gap-2.5 flex-1 min-h-0">
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

      <div className="flex-1 min-h-0 overflow-y-auto flex flex-col pr-0.5">
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
            return (
              <div
                key={r.run_id}
                className={`group relative w-full text-left border-l-2 transition-colors ${
                  active ? "border-warden-accent" : "border-transparent hover:border-slate-600/60"
                }`}
              >
                <button
                  onClick={() => onSwitch(r.run_id)}
                  title={`${r.title}（${r.run_id}）`}
                  className="w-full text-left px-2.5 py-1.5 block"
                >
                  <div className="flex items-center gap-1.5">
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
                      {r.title || r.run_id}
                    </span>
                  </div>
                  <div className="mt-1 pl-3 text-[11px] font-mono text-warden-fg/40 leading-relaxed truncate">
                    {fmtTime(r.updated_at) || "—"} · {r.msg_count} 条 · {r.run_id}
                  </div>
                  {/* 分割线：两边浅中间深的渐变 */}
                  <div className="mt-1.5 h-px bg-gradient-to-r from-transparent via-zinc-400/60 to-transparent" />
                </button>
                <button
                  onClick={(e) => remove(e, r)}
                  title="删除对话"
                  aria-label="删除对话"
                  className="absolute top-1.5 right-1 w-5 h-5 rounded text-[11px] leading-none text-zinc-600 opacity-0 group-hover:opacity-100 hover:text-warden-danger hover:bg-white/[0.06] transition"
                >
                  🗑
                </button>
              </div>
            );
          })}
      </div>
      </div>
    </div>
  );
}
