import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Vite 配置跑在 Node 里，但前端不装 @types/node；这里只用到 process.env，按需声明即可。
declare const process: { env: Record<string, string | undefined> }

// Vite 必须显式绑定 127.0.0.1：默认只监听 localhost，可能解析到 IPv6 导致连不上。
// 后端默认 8010、前端默认 5173（见交接文档 §3.4）。本机这两个端口被遗留的旧项目 dev server
// 占用，因此两者都做成环境变量可覆盖，本地开发用 MANJU_BACKEND / MANJU_WEB_PORT 切换。
const BACKEND = process.env.MANJU_BACKEND ?? 'http://127.0.0.1:8010'
const PORT = Number(process.env.MANJU_WEB_PORT ?? 5173)

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: PORT,
    strictPort: true,
    proxy: {
      '/api': {
        target: BACKEND,
        changeOrigin: true,
      },
    },
  },
  optimizeDeps: {
    include: ['react', 'react-dom', 'antd', '@xyflow/react'],
  },
})
