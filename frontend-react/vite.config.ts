import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Dev proxies /api to the FastAPI backend so cookies are same-origin in dev.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://localhost:7788', changeOrigin: true },
    },
  },
  build: { outDir: 'dist' },
});
