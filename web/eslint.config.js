// ESLint flat config（eslint 9+ / web 目录）。
// 目标：抓常见 bug 与 hooks 误用，不引入需要"大改业务代码"才能过的严格规则。
// 只 lint web/src；构建产物与依赖目录忽略。
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";

export default tseslint.config(
  {
    ignores: ["dist/**", "node_modules/**", "coverage/**"],
  },
  tseslint.configs.recommended,
  reactRefresh.configs.vite,
  {
    files: ["src/**/*.{ts,tsx}"],
    plugins: { "react-hooks": reactHooks },
    languageOptions: {
      ecmaVersion: 2020,
      sourceType: "module",
    },
    rules: {
      // 只启用 react-hooks 的两个经典规则。插件 v7 自带的 recommended-latest
      // 额外包含一批面向 React Compiler 的强规则（immutability / purity /
      // set-state-in-effect 等），要全过需要大改现有业务代码，故不启用。
      "react-hooks/rules-of-hooks": "error",
      // 从 warn 提到 error：依赖数组漏项是"静默失效"类 bug（闭包拿到旧值、
      // 该重跑的 effect 不重跑），而 README 里拿它当门禁——warn 在 CI 里
      // 不会让流水线变红，等于没门禁。CI 现在也带 --max-warnings 0。
      "react-hooks/exhaustive-deps": "error",
      // 测试与被 mock 的代码里允许 `_` 前缀占位的未使用参数。
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
    },
  },
);
