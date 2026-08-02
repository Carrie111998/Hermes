import type { TestProjectConfiguration } from 'vitest/config';
import { defineConfig } from 'vitest/config'

const reactUi: TestProjectConfiguration = {
  extends: './vite.config.ts',
  test: {
    name: 'ui',
    environment: 'jsdom',
    setupFiles: ['./vitest.setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    globals: true,
    // The first test in each file pays jsdom env init + full module transform,
    // which can exceed vitest's 5000ms default under CI/load. 15s gives the
    // cold start headroom without masking genuinely hung tests.
    testTimeout: 15_000,
    // The desktop workspace can be linked to a different physical install than
    // the repository root. Externalized React consumers then bypass Vite's
    // root React aliases (and a CJS preload cannot intercept their ESM imports),
    // producing two dispatchers. Inline UI dependencies so every React import
    // is resolved through the aliases/dedupe contract in vite.config.ts.
    server: {
      deps: {
        inline: true
      }
    }
  }
}

const electronNative: TestProjectConfiguration = {
  test: {
    name: 'electron',
    environment: 'node',
    include: ['electron/**/*.test.ts', 'scripts/**.test.{ts,mjs}']
  }
}

export default defineConfig({
  test: {
    projects: [reactUi, electronNative]
  }
})
