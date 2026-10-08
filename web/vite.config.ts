import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Built files are served by the backend at /app/ (see server/app.py).
// `npm run dev` proxies API calls to a running instance (AAF_DEV_BACKEND, default 127.0.0.1:8111).
const backend = process.env.AAF_DEV_BACKEND || 'http://127.0.0.1:8111'

export default defineConfig({
  base: '/app/',
  plugins: [react(), tailwindcss()],
  server: { proxy: { '/api': backend, '/login': backend, '/setup': backend, '/files': backend } },
  test: { environment: 'jsdom', setupFiles: ['./src/test-setup.ts'], css: false },
})
