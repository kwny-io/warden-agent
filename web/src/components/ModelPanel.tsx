// ModelPanel：模型切换组件——列出傻瓜式接入的模型，一键切换；
// 需要密钥的模型支持当场导入 API Key（Key 存内存，重启回到启动配置）。

import { useState } from "react";
import type { ModelInfo, ModelProbe, ModelsInfo } from "../lib/types";
import { toModelsInfo } from "../lib/guards";
import { usePolling } from "../lib/usePolling";
import { api } from "../lib/api";

export default function ModelPanel() {
  // 轮询错误必须接住：不接住时 /models 挂了会一直显示「加载中…」（转圈转到天荒，
  // 而进度早就断了）——那也是一句假话。
  const { data, error: loadError, refresh } = usePolling<ModelsInfo>((signal) => api.models({ signal }), 5000);
  const [editFor, setEditFor] = useState<string | null>(null); // 正在为哪个模型填配置
  const [keyValue, setKeyValue] = useState("");
  const [baseUrlValue, setBaseUrlValue] = useState("");
  const [modelNameValue, setModelNameValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // 刚探到的结论（比轮询里的 last_probe 新；点「测一下」后立刻回显）
  const [probes, setProbes] = useState<Record<string, ModelProbe>>({});

  // models 不是数组（缺字段/后端改形）时降级为空列表，而不是在 .map 上炸
  const info = toModelsInfo(data);
  const models = info?.models ?? [];
  const current = info?.current ?? "";

  /** 展开某一项的配置面板；已登记的值预填进去（地址不是秘密，且能改错别字）。 */
  const openEditor = (m: ModelInfo) => {
    setError(null);
    setEditFor((v) => (v === m.id ? null : m.id));
    setKeyValue("");
    setBaseUrlValue(m.base_url ?? "");
    setModelNameValue(m.model ?? "");
  };

  const endpoint = (m: ModelInfo) =>
    m.needs_endpoint
      ? { base_url: baseUrlValue.trim() || null, model: modelNameValue.trim() || null }
      : undefined;

  const select = async (m: ModelInfo, apiKey?: string) => {
    setBusy(true);
    setError(null);
    try {
      await api.selectModel(m.id, apiKey, undefined, endpoint(m));
      setEditFor(null);
      setKeyValue("");
      refresh();
      // 切完就地探一下：不让用户等到发出第一条消息才发现 provider 是挂的。
      void probe(m.id, true);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  /** 主动探测：失败也要把**原因**显示出来（只说「失败」等于让人去猜是网络、key 还是 base_url）。
   *
   * 带着面板上**还没保存**的地址与密钥去探：用户敲完就想知道通不通，不该被迫先把一份
   * 可能写错的配置存进库里（存错了还得再改一次）。没保存过的模型一把已登记的 key 都没有，
   * 不把输入框里那把带上，探针只会回一句「需要先导入」——"先探通再保存"就地失效。 */
  const probe = async (
    id: string,
    force: boolean,
    ep?: { base_url?: string | null; model?: string | null },
    apiKey?: string,
  ) => {
    try {
      const r = await api.probeModel(id, force, undefined, ep, apiKey);
      setProbes((prev) => ({ ...prev, [id]: r }));
    } catch (e) {
      setProbes((prev) => ({
        ...prev,
        [id]: { id, ok: false, reason: e instanceof Error ? e.message : String(e) },
      }));
    }
  };

  /** 能不能提交：自建接入必须给出地址；需要 key 的，要么已有、要么这次填了。 */
  const canSubmit = (m: ModelInfo): boolean => {
    if (m.needs_endpoint && !(baseUrlValue.trim() || m.base_url)) return false;
    if (m.needs_key && !m.configured && !keyValue.trim()) return false;
    return true;
  };

  const clickModel = (m: ModelInfo) => {
    setError(null);
    if (m.id === current) return;
    // 自建接入：地址没填就切换是死路（后端会 400），而且界面上一个能填的地方都没有。
    // 需要 key 或需要端点，都先展开配置。
    if ((m.needs_key && !m.configured) || m.needs_endpoint) {
      openEditor(m);
      return;
    }
    void select(m);
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
          const showEditor = editFor === m.id;
          // 探测结论：界面自己探的优先，否则用后端轮询带回来的上次结论
          const p = probes[m.id] ?? m.last_probe ?? null;
          // 探不通就要说。**不要**加 `m.configured &&` 这道门：尚未保存的模型
          // 恰恰是最需要探测反馈的时候（"我这地址填对了吗"），
          // 而按 configured 过滤会让「先探通再保存」这个用法一句原因都看不到。
          const unreachable = p !== null && !p.ok;
          // 「未导入」的前置条件要按真实缺口说：自建接入缺的可能是地址，不是 key。
          const missing = !m.configured;
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
                {missing && (
                  <span className="text-[11px] text-warden-warn/80">未配置</span>
                )}
                {/* 有结论就显示，不论是否已保存：未保存的自建模型探通了，
                    这正是"我这地址填对了吗"的答案；按 configured 挡住等于答了又不给看。 */}
                {p !== null && (
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

              {/* 探不通就把**该做什么**写在卡片里：换 Key / 去充值 / 改地址 / 等一等
                  是四件不同的事，只给一句原始异常等于让用户猜（笔记 95）。
                  `hint` 由服务端给，前端不自己编文案：后端契约与界面必须同源。 */}
              {unreachable && (p?.hint || p?.reason) && (
                <p className="mt-1 text-[10px] text-warden-danger/90 break-all">
                  ⚠️ {m.configured ? "有凭据，但探不通" : "探不通"}：{p?.hint || p.reason}
                  {p?.hint && p?.reason && (
                    <span className="block text-warden-fg/50">{p.reason}</span>
                  )}
                </p>
              )}

              {/* 配置面板：需要密钥、或需要自建端点时展开。
                  自建接入的两个输入框是这一项存在的全部理由——「不愿把对话发到公网大模型」
                  的答案就是在这里填一个自己公司的地址。 */}
              {showEditor && (
                <div className="mt-1.5 flex flex-col gap-1">
                  {m.needs_endpoint && (
                    <>
                      <input
                        type="text"
                        value={baseUrlValue}
                        onChange={(e) => setBaseUrlValue(e.target.value)}
                        aria-label={`${m.name} 的端点地址`}
                        placeholder="http://10.0.0.7:8000/v1"
                        className="w-full min-w-0 bg-black/30 border border-white/10 rounded px-2 py-1 text-[11px] font-mono outline-none focus:border-warden-accent/70"
                      />
                      <input
                        type="text"
                        value={modelNameValue}
                        onChange={(e) => setModelNameValue(e.target.value)}
                        aria-label={`${m.name} 的模型名`}
                        placeholder="模型名，如 qwen2.5-7b-instruct"
                        className="w-full min-w-0 bg-black/30 border border-white/10 rounded px-2 py-1 text-[11px] font-mono outline-none focus:border-warden-accent/70"
                      />
                    </>
                  )}
                  {/* key 已有就不必重填：留空 = 沿用库里那把（后端会原样抄回）。
                      但要给一句提示，否则用户不知道留空会发生什么。 */}
                  <div className="flex items-center gap-1">
                    <input
                      type="password"
                      value={keyValue}
                      onChange={(e) => setKeyValue(e.target.value)}
                      aria-label={`${m.name} 的 API Key`}
                      placeholder={m.configured ? "沿用已保存的访问密钥（留空即可）" : "粘贴访问密钥…"}
                      className="flex-1 min-w-0 bg-black/30 border border-white/10 rounded px-2 py-1 text-[11px] font-mono outline-none focus:border-warden-accent/70"
                    />
                    <button
                      onClick={() =>
                        void probe(
                          m.id,
                          true,
                          { base_url: baseUrlValue.trim() || null, model: modelNameValue.trim() || null },
                          keyValue.trim() || undefined,
                        )
                      }
                      aria-label={`用当前填写的地址测一下 ${m.name}`}
                      className="px-2 py-1 rounded border border-slate-600/60 text-zinc-300 text-[10px] hover:border-warden-accent/70 hover:text-white transition"
                    >
                      测一下
                    </button>
                    <button
                      onClick={() => void select(m, keyValue.trim() || undefined)}
                      disabled={busy || !canSubmit(m)}
                      className="px-2 py-1 rounded bg-warden-accent/80 text-white text-[10px] disabled:opacity-40 hover:brightness-110 transition"
                    >
                      {m.configured ? "保存并切换" : "导入并切换"}
                    </button>
                  </div>
                  {m.needs_endpoint && (
                    <p className="text-[10px] text-warden-fg/45 leading-relaxed">
                      填公司内网 / 本机的 OpenAI 兼容地址即可，对话不出你的网络。
                      不校验密钥的本地端点可随便填一个占位串（如 not-needed）。
                    </p>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
