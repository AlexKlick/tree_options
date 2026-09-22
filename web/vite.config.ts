/// <reference types="vitest" />
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// base './' + the FastAPI root StaticFiles mount: the built index.html
// sits NEXT TO assets/, so './assets/...' resolves from / (loopback) and
// /trex/ (portal-stripped) alike. Never a leading slash.
export default defineConfig({
  base: './',
  plugins: [react()],
  build: {
    outDir: '../src/tree_options/trex_web/static',
    emptyOutDir: true,
    sourcemap: false,
    target: 'es2020',
  },
  server: {
    host: '127.0.0.1',
    port: 5175,
    strictPort: true,
    proxy: {
      '/api': 'http://127.0.0.1:8090',
      '/plan': 'http://127.0.0.1:8090',
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test-setup.ts'],
  },
})
