import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 本地开发：前端 5174（5173 被其他项目占用），API 代理到后端 8001，WebSocket 也代理
export default defineConfig({
  plugins: [react()],
  build: {
    // 每次构建清空输出目录，避免历史 chunk 越堆越多（曾累积到 352 个 js）
    emptyOutDir: true,
    chunkSizeWarningLimit: 1200,
    rollupOptions: {
      output: {
        // 把不会常变的第三方库拆出来：业务代码更新时用户不必重新下载 React
        manualChunks(id) {
          if (!id.includes('node_modules')) return undefined
          if (id.includes('react-dom') || id.includes('/react/')) return 'vendor-react'
          if (id.includes('react-router')) return 'vendor-router'
          if (id.includes('axios')) return 'vendor-http'
          return 'vendor'
        },
      },
    },
  },
  server: {
    port: 5174,
    strictPort: true,
    // 监听所有网卡。默认只绑 ::1（IPv6 回环），局域网其他电脑连不进来。
    host: '0.0.0.0',
    // Vite 5.4.12+ 会校验 Host 头，用局域网 IP 直连会被拦（Blocked request），这里放开
    allowedHosts: true,
    // 关键：编辑器/工具是"原子写"——先在目标目录下建 <名字>.<pid>.<uuid>.tmpdir/ 再改名。
    // chokidar 去 watch 这个临时文件时 Windows 抛 EBUSY，Vite 收到 FSWatcher 的 error
    // 事件会直接退出（这个控制台被崩过）。忽略规则按 dot:true 解析，能匹配 . 开头的名字。
    // 客户端那份 vite.config.js 也有一份同样的配置。
    watch: {
      ignored: [
        '**/*.tmpdir',
        '**/*.tmpdir/**',
        '../backend/**',
        '**/.venv/**',
      ],
    },
    proxy: {
      '/api': {
        target: 'http://localhost:8001',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://localhost:8001',
        ws: true,
        changeOrigin: true,
      },
    },
  },
})
