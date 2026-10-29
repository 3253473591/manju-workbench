import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 前端只认 `/api/v1`：开发期由 Vite 代理到后端，生产由反代转发。
// target 可用 VITE_API_TARGET 覆盖 —— 本机 8000 常被别的程序占用，硬编码会让人排查半天。
const target = process.env.VITE_API_TARGET ?? "http://127.0.0.1:8000";
const port = Number(process.env.VITE_PORT ?? 5173);

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1", // 显式绑 IPv4：只绑 localhost 时 Windows 上只监听 ::1，用 127.0.0.1 反而连不上
    port,
    strictPort: true,
    proxy: { "/api": { target, changeOrigin: false } },
  },
  build: { outDir: "dist", sourcemap: true },
});
