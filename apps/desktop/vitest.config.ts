import type { TestProjectConfiguration } from 'vitest/config'
import { defineConfig } from 'vitest/config'

const uiColdStartLimits =
  process.platform === 'win32' ? { maxWorkers: 4, testTimeout: 30_000 } : { testTimeout: 15_000 }

const reactUi: TestProjectConfiguration = {
  extends: './vite.config.ts',
  test: {
    name: 'ui',
    environment: 'jsdom',
    setupFiles: ['./vitest.setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    globals: true,
    // The first test in each file pays jsdom env init + full module transform.
    // On Windows the larger renderer graphs can exceed 15s cold, and Vitest's
    // default all-core fan-out (32 workers on common desktops) turns that into
    // transform starvation. Bound parallelism and leave enough cold-start
    // headroom without slowing other platforms or disabling the timeout for
    // genuinely hung tests.
    ...uiColdStartLimits
  }
}

const electronNative: TestProjectConfiguration = {
  test: {
    name: 'electron',
    environment: 'node',
    include: ['electron/**/*.test.ts', 'scripts/**.test.{ts,mjs}'],
    exclude: ['scripts/run-short-session-hang-repro.test.mjs']
  }
}

export default defineConfig({
  test: {
    projects: [reactUi, electronNative]
  }
})
