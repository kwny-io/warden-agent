// lib/deepLink.ts —— 会话 ID 的 URL 深链（`?run=xxx`）。
//
// 为什么值得单独一个模块并单测：这是**外部输入**（用户粘贴的链接、书签、IM 里被截断的链接）。
// 它直接决定"打开页面时去看哪个会话"，所以必须：
//   1. 只接受形如 run-id 的字面量——URL 里能塞任意字符串，不校验就等于把外部字符串
//      当成会话 ID 直接拿去拼请求路径（同目录下 `guards.ts` 的同一条思路）；
//   2. 写回 URL 用 `replaceState`——用 `pushState` 的话，切换 10 次会话按"后退"要退 10 次
//      才能离开页面（把浏览器的历史记录搞成噪声，是那种"功能对但用起来很烦"的实现）；
//   3. 读写都不抛异常：无 hash 支持 / 沙箱环境（如测试里的 jsdom、某些隐私模式）不能让整页崩掉。

/** 合法会话 ID：字母数字与 `-` `_` `.` `:`，长度 1–80。刻意不放行空格与 `%`。 */
const RUN_ID_RE = /^[A-Za-z0-9._:-]{1,80}$/;

/** 会话 ID 是否是合法字面量（深链读取与创建输入行都走这一份口径）。 */
export function isValidRunId(id: string): boolean {
  return RUN_ID_RE.test(id);
}

/**
 * 从查询串里读会话 ID。
 *
 * @param search 形如 `?run=abc` 的查询串（通常传 `window.location.search`）
 * @returns 合法则为该 ID；缺失、为空、或形态不对（可能被截断/被注入）则 `null`
 *
 * 注意**不抛异常也不兜底返回脏值**：形态不对就当"没有深链"，回退到新会话——
 * 宁可多开一个会话，也不要拿着一个可疑字符串去请求别人的会话。
 */
export function readRunFromUrl(search: string): string | null {
  if (!search) return null;
  let raw: string | null = null;
  try {
    raw = new URLSearchParams(search).get("run");
  } catch {
    return null;
  }
  if (!raw) return null;
  const id = raw.trim();
  return isValidRunId(id) ? id : null;
}

/**
 * 把当前会话写回 URL（`replaceState`，不产生新的历史条目）。
 *
 * 失败（沙箱 / 无 history）时静默返回：URL 只是"可分享/可刷新"的便利，
 * 写不进去不该让控制台崩掉。
 */
export function writeRunToUrl(
  runId: string,
  history: Pick<History, "replaceState"> | undefined = typeof window === "undefined"
    ? undefined
    : window.history,
  location:
    | { pathname: string; search: string; hash: string }
    | undefined = typeof window === "undefined" ? undefined : window.location,
): void {
  if (!isValidRunId(runId) || !history || !location) return;
  try {
    const params = new URLSearchParams(location.search);
    if (params.get("run") === runId) return; // 已是当前值：不做无意义的历史操作
    params.set("run", runId);
    const query = params.toString();
    history.replaceState(
      null,
      "",
      `${location.pathname}${query ? `?${query}` : ""}${location.hash}`,
    );
  } catch {
    /* 只是便利功能：写不进去就算了 */
  }
}
