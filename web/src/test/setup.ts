// Vitest 全局 setup：只在测试进程里执行，不参与生产构建。
// 目前只做一件事——把 @testing-library/jest-dom 的断言（toBeInTheDocument 等）
// 注入到 Vitest 的 expect 上。
//
// 注意：这里**没有**加 matchMedia / ResizeObserver 的 polyfill。
// 现有 11 个业务文件里没有任何一处调用 window.matchMedia 或 ResizeObserver，
// 加无用 polyfill 只会掩盖真实的环境差异；将来某个组件真的用了、测试在 jsdom
// 下报 "not a function" 时，再回来按需补上并注释原因。
import "@testing-library/jest-dom/vitest";
