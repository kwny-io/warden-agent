// ModelPanel：模型切换组件——列出傻瓜式接入的模型，一键切换；
// 需要密钥的模型支持当场导入 API Key（Key 存内存，重启回到启动配置）。

import { useState } from "react";
import type { ModelProbe, ModelsInfo } from "../lib/types";
import { toModelsInfo } from "../lib/guards";
import { usePolling } from "../lib/usePolling";
import { api } from "../lib/api";

export default function ModelPanel() {
  // 轮询错误必须接住：不接住时 /models 挂了会一直显示「加载中…」（转圈转到天荒，
  // 而进度早就断了）——那也是一句假话。
  const { data, error: loadError, refresh } = usePolling<ModelsInfo>((signal) => api.models({ signal }), 5000);
  const [keyFor, setKeyFor] = useState<string | null>(null); // 正在为哪个模型输入 Key
  const [keyValue, setKeyValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // 刚探到的结论（比轮询里的 last_probe 新；点「测一下」后立刻回显）
  const [probes, setProbes] = useState<Record<string, ModelProbe>>({});

  // models 不是数组（缺字段/后端改形）时降级为空列表，而不是在 .map 上炸
  const info = toModelsInfo(data);
  const models = info?.models ?? [];
  const current = info?.current ?? "";

  const select = async (id: string, apiKey?: string) => {
    setBusy(true);
    setError(null);
    try {
      await api.selectModel(id, apiKey);
      setKeyFor(null);
      setKeyValue("");
      refresh();
      // 切完就地探一下：不让用户等到发出第一条消息才发现 provider 是挂的。
      void probe(id, true);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  /** 主动探测：失败也要把**原因**显示出来（只说「失败」等于让人去猜是网络、key 还是 base_url）。 */
  const probe = async (id: string, force: boolean) => {
    try {
      const r = await api.probeModel(id, force);
      setProbes((prev) => ({ ...prev, [id]: r }));
    } catch (e) {
      setProbes((prev) => ({
        ...prev,
        [id]: { id, ok: false, reason: e instanceof Error ? e.message : String(e) },
      }));
    }
  };

  const clickModel = (m: ModelsInfo["models"][number]) => {
    setError(null);
    if (m.id === current) return;
    if (m.needs_key && !m.configured) {
      setKeyFor((v) => (v === m.id ? null : m.id)); // 展开内联 Key 输入
      return;
    }
    select(m.id);
  };

  return (
    <div className="wpanel p-3.5">
      <h3 className="flex items-center gap-1.5 text-sm font-medium text-warden-fg mb-2">
        <span className="w-2 h-2 rounded-full bg-warden-accent shadow-[0_0_8px_rgba(94,129,172,0.8)]" />
        模型
      </h3>

      {error && (
        <p className="text-xs text-warden-danger bg-warden-danger/10 rounded px-2 py-1 mb-2">
          {error}
        </p>
      )}

      {loadError && (
        <p className="text-xs text-warden-danger bg-warden-danger/10 rounded px-2 py-1 mb-2">
          ⚠️ 加载模型列表失败：{loadError}
          <button onClick={refresh} className="ml-2 underline">重试</button>
        </p>
      )}

      <div className="flex flex-col gap-1.5">
        {models.length === 0 && !loadError && <p className="text-xs text-warden-fg/40">加载中…</p>}
        {models.map((m) => {
          const isCurrent = m.id === current;
          const showKeyInput = keyFor === m.id;
          // 探测结论：界面自己探的优先，否则用后端轮询带回来的上次结论
          const p = probes[m.id] ?? m.last_probe ?? null;
          const unreachable = m.configured && p !== null && !p.ok;
          return (
            <div key={m.id} className="rounded-lg bg-black/20 border border-white/[0.05] px-2.5 py-1.5">
              <div className="flex items-center gap-1.5">
                <span
                  className={`w-1.5 h-1.5 shrink-0 rounded-full ${
                    isCurrent ? "bg-warden-ok shadow-[0_0_8px_rgba(86,154,140,0.9)]" : "bg-zinc-600"
                  }`}
                />
                <span
                  className={`text-[13px] flex-1 truncate ${
                    isCurrent ? "text-zinc-100 font-medium" : "text-zinc-400"
                  }`}
                >
                  {m.name}
                </span>
                {!m.configured && (
                  <span className="text-[11px] text-warden-warn/80">未导入</span>
                )}
                {m.configured && p !== null && (
                  <button
                    onClick={() => probe(m.id, true)}
                    title={p.ok ? `实测连通${p.latency_ms != null ? ` (${p.latency_ms}ms)` : ""}` : p.reason ?? "探不通"}
                    className={`px-1.5 py-0.5 rounded text-[10px] border transition ${
                      p.ok
                        ? "border-warden-ok/40 text-warden-ok hover:border-warden-ok/70"
                        : "border-warden-danger/50 text-warden-danger hover:border-warden-danger"
                    }`}
                  >
                    {p.ok ? "实测连通" : "探不通"}
                  </button>
                )}
                {m.configured && p === null && (
                  <button
                    onClick={() => probe(m.id, true)}
                    className="px-1.5 py-0.5 rounded text-[10px] border border-slate-600/60 text-zinc-400 hover:border-warden-accent/70 hover:text-white transition"
                  >
                    测一下
                  </button>
                )}
                {!isCurrent && (
                  <button
                    onClick={() => clickModel(m)}
                    disabled={busy}
                    className="btn-sheen px-2 py-0.5 rounded border border-slate-600/60 text-[10px] text-zinc-300 hover:border-warden-accent/70 hover:text-white disabled:opacity-40 transition"
                  >
                    切换
                  </button>
                )}
                {isCurrent && (
                  <span className="text-[11px] text-warden-ok">使用中</span>
                )}
              </div>

              {/* 探不通就把原因写在卡片里：有 key ≠ 能用 */}
              {unreachable && p?.reason && (
                <p className="mt-1 text-[10px] text-warden-danger/90 break-all">
                  ⚠️ 有 Key，但探测失败：{p.reason}
                </p>
              )}

              {/* 内联 Key 导入：需要密钥且未配置时展开 */}
              {showKeyInput && (
                <div className="mt-1.5 flex items-center gap-1">
                  <input
                    type="password"
                    value={keyValue}
                    onChange={(e) => setKeyValue(e.target.value)}
                    aria-label={`${m.name} 的 API Key`}
                    placeholder="粘贴 API Key…"
                    className="flex-1 min-w-0 bg-black/30 border border-white/10 rounded px-2 py-1 text-[11px] font-mono outline-none focus:border-warden-accent/70"
                  />
                  <button
                    onClick={() => select(m.id, keyValue.trim() || undefined)}
                    disabled={busy || !keyValue.trim()}
                    className="px-2 py-1 rounded bg-warden-accent/80 text-white text-[10px] disabled:opacity-40 hover:brightness-110 transition"
                  >
                    导入并切换
                  </button>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
