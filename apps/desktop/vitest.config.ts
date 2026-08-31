import type { TestProjectConfiguration } from 'vitest/config'
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
    testTimeout: 15_000
  }
}

const electronNative: TestProjectConfiguration = {
  test: {
    name: 'electron',
    environment: 'node',
    include: ['electron/**/*.test.ts', 'scripts/**.test.{ts,mjs}'],
    exclude: ['scripts/run-short-session-hang-repro.test.mjs'],
    // These shell out to real `git`/`ssh` in temp repos, so they are slower
    // than the jsdom cold start above — and `npm run check` runs them AFTER
    // the 100s+ ui project, when the runner is already loaded. On the 5000ms
    // default that made git-worktree-ops/git-review-ops fail the aggregate
    // gate while passing standalone. Same headroom, same reasoning as ui.
    testTimeout: 15_000
  }
}

export default defineConfig({
  test: {
    projects: [reactUi, electronNative]
  }
})
