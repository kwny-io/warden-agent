// ModelPanel：模型切换组件——列出傻瓜式接入的模型，一键切换；
// 需要密钥的模型支持当场导入 API Key（Key 存内存，重启回到启动配置）。

import { useState } from "react";
import type { ModelsInfo } from "../lib/types";
import { usePolling } from "../lib/usePolling";
import { api } from "../lib/api";

export default function ModelPanel() {
  const { data, refresh } = usePolling<ModelsInfo>(() => api.models(), 5000);
  const [keyFor, setKeyFor] = useState<string | null>(null); // 正在为哪个模型输入 Key
  const [keyValue, setKeyValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const models = data?.models ?? [];
  const current = data?.current ?? "";

  const select = async (id: string, apiKey?: string) => {
    setBusy(true);
    setError(null);
    try {
      await api.selectModel(id, apiKey);
      setKeyFor(null);
      setKeyValue("");
      refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
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

      <div className="flex flex-col gap-1.5">
        {models.length === 0 && <p className="text-xs text-warden-fg/40">加载中…</p>}
        {models.map((m) => {
          const isCurrent = m.id === current;
          const showKeyInput = keyFor === m.id;
          return (
            <div key={m.id} className="rounded-lg bg-black/20 border border-white/[0.05] px-2.5 py-1.5">
              <div className="flex items-center gap-1.5">
                <span
                  className={`w-1.5 h-1.5 shrink-0 rounded-full ${
                    isCurrent ? "bg-warden-ok shadow-[0_0_8px_rgba(86,154,140,0.9)]" : "bg-zinc-600"
                  }`}
                />
                <span
                  className={`text-xs flex-1 truncate ${
                    isCurrent ? "text-zinc-100 font-medium" : "text-zinc-400"
                  }`}
                >
                  {m.name}
                </span>
                {!m.configured && (
                  <span className="text-[10px] text-warden-warn/80">未导入</span>
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
                  <span className="text-[10px] text-warden-ok">使用中</span>
                )}
              </div>

              {/* 内联 Key 导入：需要密钥且未配置时展开 */}
              {showKeyInput && (
                <div className="mt-1.5 flex items-center gap-1">
                  <input
                    type="password"
                    value={keyValue}
                    onChange={(e) => setKeyValue(e.target.value)}
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
