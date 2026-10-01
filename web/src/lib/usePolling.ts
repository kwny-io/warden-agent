// 自定义 Hook：轮询式拉取后端状态（审批队列 / 健康检查等）。
// resetKey 变化时（如切换 run_id）立即重拉并清空旧数据，避免显示上一个 key 的结果。
// 标签页切到后台时暂停轮询，回到前台立刻补一次——后台轮询只是白烧请求。
//
// 这个 Hook 被 7 个面板复用（审批 2s / 历史 5s / 状态 3s / 记忆 5s / 健康 5s /
// 会话列表 5s / 模型 5s），所以正确性比省事重要，见下面三处守卫。

import { useCallback, useEffect, useRef, useState } from "react";

// 连续失败后的退避上限：后端 5xx / 宕机时前端不能变成压垮源
// （7 个轮询点按 2–5s 打同一台机器，审批队列 2s 最密）。
const MAX_BACKOFF_MS = 30_000;

export function usePolling<T>(
  fetcher: (signal?: AbortSignal) => Promise<T>,
  intervalMs: number,
  resetKey?: string,
): { data: T | null; error: string | null; refresh: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  // 卸载后不再 setState：慢请求回来时组件可能已经没了
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // 请求代际：换 key / 卸载 / 发起新请求时自增，只有「最新一代」的响应有资格写 state。
  // 没有这道门时：A 的慢响应晚于 B 的快响应返回就会覆盖 B——切会话、快速切账号时
  // 必现，界面上表现为「显示的是上一个会话的数据」，而且是静默的。
  const seqRef = useRef(0);
  // 在途请求的取消句柄：换 key / 卸载时真正断开网络，而不只是「回来时丢弃结果」。
  // 轮询的每次新拉取也会取消上一次未完成的请求，避免请求在故障时堆积。
  const abortRef = useRef<AbortController | null>(null);
  // 连续失败次数，用来算退避间隔；任何一次成功都清零
  const failRef = useRef(0);

  // 作废在途响应：换 key / 卸载时叫一代。包成函数而不是在 cleanup 里直接写
  // seqRef.current++ —— 后者会让 react-hooks/exhaustive-deps 报警（它无法知道
  // 这个 ref 是稳定的「计数器」而不是渲染节点）。
  const bumpSeq = useCallback(() => {
    seqRef.current += 1;
  }, []);

  const load = useCallback(async () => {
    const seq = ++seqRef.current;
    abortRef.current?.abort(); // 上一次还没回来就先放弃它，只认最新的
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      const d = await fetcherRef.current(ctrl.signal);
      if (!mountedRef.current || seq !== seqRef.current) return; // 迟到的旧代结果：丢弃
      failRef.current = 0;
      setData(d);
      setError(null);
    } catch (e) {
      if (!mountedRef.current || seq !== seqRef.current) return;
      // 主动 abort 不算失败：切会话 / 卸载不是错误，别把它显示成红色报警，
      // 也别让它触发退避（否则一次切会话会把后续轮询都降频）。
      if (ctrl.signal.aborted) return;
      failRef.current += 1;
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    if (resetKey !== undefined) setData(null); // 换了 key：旧数据立即作废
    bumpSeq(); // 作废在途响应（紧接着的 load() 会再自增一代）
    failRef.current = 0; // 换 key / 重挂载后从正常频率重新开始
    let timer: ReturnType<typeof setTimeout> | null = null;
    let stopped = false;

    const schedule = () => {
      // 指数退避：失败越多间隔越大，上限 30s；成功已在 load 里把计数清零
      const delay = Math.min(intervalMs * 2 ** Math.min(failRef.current, 4), MAX_BACKOFF_MS);
      timer = setTimeout(() => void tick(), delay);
    };
    const tick = async () => {
      if (stopped) return;
      await load();
      if (stopped || document.hidden) return; // 后台标签页不排下一次
      schedule();
    };

    // 起手先拉一次，拿到结果（含失败次数）再排下一次：退避节奏才一致，
    // 且不会在上一次请求还没回来时又发一份。
    void tick();

    const onVisibilityChange = () => {
      if (document.hidden) {
        if (timer !== null) {
          clearTimeout(timer);
          timer = null;
        }
      } else {
        void tick(); // 回到前台：先把 stale 数据补上，再恢复定时
      }
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      stopped = true;
      if (timer !== null) clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibilityChange);
      bumpSeq(); // 卸载后在途响应一律作废
      abortRef.current?.abort(); // 并真正断开，不留悬挂请求
      abortRef.current = null;
    };
  }, [intervalMs, resetKey, load, bumpSeq]);

  return { data, error, refresh: load };
}
