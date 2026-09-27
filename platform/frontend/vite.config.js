import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发期把 /api 与 /mcp 直接转给后端（默认 127.0.0.1:8900）。
// 生产构建由后端自己托管静态产物，不存在跨域 —— 这里只服务开发体验。
const BACKEND = process.env.REWRITE_BACKEND || 'http://127.0.0.1:8900'

export default defineConfig({
  plugins: [vue()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': { target: BACKEND, changeOrigin: true },
      '/mcp': { target: BACKEND, changeOrigin: true },
      '/healthz': { target: BACKEND, changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 900,
  },
})
