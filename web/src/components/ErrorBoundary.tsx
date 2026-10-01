// ErrorBoundary：全局兜底。任何子组件在渲染期抛错都会在这里被接住，
// 换成可读的中文提示 + 「重新加载」按钮，而不是整页白屏。
//
// 只有**渲染/lifecycle** 的错误会被 class ErrorBoundary 捕获；事件处理器和
// 异步代码里的错误不会走到这里（那些地方组件自己 try/catch，如 ChatView.send）。
// 所以它不是万能兜底，但足以挡住"某个面板拿到畸形数据直接 throw → 整站白屏"。

import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
  /** 可选的定制兜底 UI；不传用默认样式。 */
  fallback?: (error: Error, reset: () => void) => ReactNode;
  /** 可选的重新加载实现；默认 window.location.reload。测试里可注入 spy。 */
  onReload?: () => void;
}

interface State {
  error: Error | null;
}

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // 留一份现场：生产环境可换成上报（Sentry 等）。这里只写 console，
    // 注意这条 log 本身绝不能再抛错，否则会把兜底 UI 一起带走。
    try {
      console.error("[ErrorBoundary] 渲染出错:", error, info.componentStack);
    } catch {
      /* 忽略：连 console 都不可用时也要保证兜底 UI 渲染出来 */
    }
  }

  reset = (): void => this.setState({ error: null });

  reload = (): void => {
    if (this.props.onReload) this.props.onReload();
    else window.location.reload();
  };

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;
    if (this.props.fallback) return this.props.fallback(error, this.reset);

    return (
      <div className="h-full w-full flex-1 flex items-center justify-center p-6">
        <div className="wpanel max-w-lg w-full p-6 text-center flex flex-col items-center gap-3">
          <div className="w-12 h-12 rounded-xl border border-warden-danger/50 bg-warden-danger/10 flex items-center justify-center text-2xl">
            ⚠️
          </div>
          <h2 className="text-base font-medium text-warden-fg">页面出了点问题</h2>
          {/* 这句话必须与实际行为一致：全局边界一触发是**整页**被替换，
              只有一个面板崩而不影响其余面板的是 PanelBoundary。 */}
          <p className="text-sm text-warden-fg/60">
            界面渲染失败，已拦截（这是最后一道兜底，请重新加载）。
          </p>
          <pre className="w-full text-left text-[11px] font-mono text-warden-danger/90 bg-black/30 border border-white/[0.06] rounded-lg p-3 overflow-auto max-h-40 whitespace-pre-wrap break-all">
            {error.message || String(error)}
          </pre>
          <div className="flex gap-2 mt-1">
            <button
              onClick={this.reset}
              className="px-4 py-1.5 rounded-lg border border-slate-600/60 text-sm text-warden-fg hover:border-warden-accent/70 transition"
            >
              重试
            </button>
            <button
              onClick={this.reload}
              className="px-4 py-1.5 rounded-lg bg-warden-accent/80 text-white text-sm hover:brightness-110 transition"
            >
              重新加载
            </button>
          </div>
        </div>
      </div>
    );
  }
}

/**
 * 面板级边界：把一个面板的渲染错误**限制在它自己的卡片内**，其余面板继续可用。
 *
 * 为什么必须有：全局那一层（main.tsx）只包住整个 App，任一子组件抛错会把**整页**
 * 换成兜底 UI——与"一个面板坏了不影响别的"的产品承诺相反。治理控制台尤其不能这样：
 * 审批面板崩了不该连带把正在跑的对话区一起抹掉。
 */
export function PanelBoundary({ name, children }: { name: string; children: ReactNode }) {
  return (
    <ErrorBoundary
      fallback={(error, reset) => (
        <div className="h-full w-full flex-1 flex items-center justify-center p-4">
          <div className="wpanel w-full max-w-sm p-4 text-center flex flex-col items-center gap-2">
            <div className="text-xl">⚠️</div>
            <p className="text-sm text-warden-fg">「{name}」渲染失败，已隔离，其他面板不受影响。</p>
            <pre className="w-full text-left text-[11px] font-mono text-warden-danger/90 bg-black/30 border border-white/[0.06] rounded-lg p-2 overflow-auto max-h-24 whitespace-pre-wrap break-all">
              {error.message || String(error)}
            </pre>
            <button
              onClick={reset}
              className="px-3 py-1.5 rounded-lg border border-zinc-600 text-zinc-300 text-sm hover:border-zinc-400 transition"
            >
              重试
            </button>
          </div>
        </div>
      )}
    >
      {children}
    </ErrorBoundary>
  );
}
