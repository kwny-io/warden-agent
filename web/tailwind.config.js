/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // 中性锌灰（ZCode 式）底 + 冷白 + 低饱和蓝强调
        warden: {
          bg: "#101013",     // 全局底色（zinc-950 调）
          midnight: "#0c0c0e", // 更深一层
          panel: "#1c1c1f",  // 面板基色
          line: "#2e2e33",   // 分隔线
          fg: "#e4e4e7",     // 主文字（zinc-200）
          accent: "#5e81ac", // 主强调：低饱和蓝
          cyan: "#7fa3c4",   // 浅钢蓝（渐变搭档）
          ok: "#569a8c",     // 成功 = 低饱和青绿
          warn: "#c9a04a",   // 等待 / 警示 = 低饱和琥珀
          danger: "#c96a5a", // 危险 = 低饱和红
        },
      },
    },
  },
  plugins: [],
};
