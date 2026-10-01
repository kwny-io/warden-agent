// 全局类型声明：让 tsc 认识 Vitest 的全局 API（describe/it/expect/vi）。
// vite.config.ts 里开了 test.globals = true，运行时它们挂在 globalThis 上；
// 这里只是把对应的类型带进编译，测试文件因此不用逐个 import。
/// <reference types="vitest/globals" />
