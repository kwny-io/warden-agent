// 自定义 Hook：轮询式拉取后端状态（审批队列 / 健康检查等）。
// 用 setInterval 轮询，简单可靠；不依赖 SSE 也能看到队列变化。
// resetKey 变化时（如切换 run_id）立即重拉并清空旧数据，避免显示上一个 key 的结果。

import { useEffect, useRef, useState } from "react";

export function usePolling<T>(
  fetcher: () => Promise<T>,
  intervalMs: number,
  resetKey?: string,
): { data: T | null; error: string | null; refresh: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const load = async () => {
    try {
      const d = await fetcherRef.current();
      setData(d);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  useEffect(() => {
    if (resetKey !== undefined) setData(null); // 换了 key：旧数据立即作废
    load();
    const id = setInterval(load, intervalMs);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, resetKey]);

  return { data, error, refresh: load };
}
