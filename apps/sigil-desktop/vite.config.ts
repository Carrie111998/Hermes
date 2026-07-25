import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'node:path'
import { defineConfig } from 'vite'

export default defineConfig({
  base: './',
  plugins: [react(), tailwindcss()],
  css: { postcss: { plugins: [] } },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, '../desktop/src'),
      '@hermes-desktop': path.resolve(__dirname, '../desktop/src'),
      '@sigil/mission-control': path.resolve(__dirname, './src/mission-control')
    },
    dedupe: ['react', 'react-dom']
  },
  server: { host: '127.0.0.1', port: 5175, strictPort: true },
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['src/**/*.test.{ts,tsx}', 'electron/**/*.test.ts']
  }
})
