/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // 深色控制台主题，跟原演示控制台配色呼应
        warden: {
          bg: "#0f1220",     // 页面底色
          panel: "#1a1e30",  // 面板
          line: "#2a2f45",   // 分隔线
          fg: "#e6e8f0",     // 主文字
          accent: "#4f8cff", // 强调蓝
          ok: "#3ddc97",     // 成功绿
          warn: "#ffc857",   // 警示黄
          danger: "#ff5c5c", // 危险红
        },
      },
    },
  },
  plugins: [],
};
