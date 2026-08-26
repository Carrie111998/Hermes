import { fileURLToPath, URL } from 'node:url'
import path from 'node:path'
import { createRequire } from 'node:module'

import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Resolve a path relative to this config file.
const r = (p: string) => fileURLToPath(new URL(p, import.meta.url))
const requireFromMobile = createRequire(path.join(r('./'), 'vite.config.ts'))
const driverIife = path.join(path.dirname(requireFromMobile.resolve('driver.js')), 'driver.js.iife.js')

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      // The desktop renderer references itself via `@/...`; point that at the
      // desktop workspace source so its components run here UNMODIFIED — no
      // vendoring. The mobile bridge also uses `@/global` etc. via this alias.
      '@': r('../desktop/src'),
      // Shared gateway client (workspace package).
      '@hermes/shared': r('../shared/src'),
      '@hermes/plugin-sdk': r('../desktop/src/sdk/index.ts'),
      'driver.js/dist/driver.js.iife.js?raw': `${driverIife}?raw`,
      'driver.js/dist/driver.js.iife.js': driverIife,
      // Mobile-only (net-new) code.
      '~mobile': r('./src/mobile'),
      '~bridge': r('./src/bridge'),
    },
  },
  server: {
    host: '127.0.0.1',
    port: 5180,
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})
