import { defineConfig } from 'vite';
import { resolve } from 'path';

// Multi-page build: bundles/minifies the existing pages without rewriting their
// logic. Inline scripts are preserved. The backend can still serve the source
// pages directly (same-origin); `npm run build` produces dist/ for CDN hosting.
export default defineConfig({
  root: '.',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: {
      input: {
        main: resolve(__dirname, 'index.html'),
        login: resolve(__dirname, 'login.html'),
      },
    },
  },
});
