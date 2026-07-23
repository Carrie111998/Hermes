import type { TestProjectConfiguration } from 'vitest/config';
import { defineConfig } from 'vitest/config'

// vitest.setup.ts widens Testing Library's asyncUtilTimeout so findBy*/waitFor
// survive a starved runner. That only helps if the test itself is allowed to
// outlive the wait — at vitest's 5s default the test dies first and reports an
// opaque "Test timed out" instead of the query error. Keep testTimeout well
// above asyncUtilTimeout so the failing query is the thing that gets reported.
const TEST_TIMEOUT_MS = 30_000

const reactUi: TestProjectConfiguration = {
  extends: './vite.config.ts',
  test: {
    name: 'ui',
    environment: 'jsdom',
    setupFiles: ['./vitest.setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    globals: true,
    testTimeout: TEST_TIMEOUT_MS,
    hookTimeout: TEST_TIMEOUT_MS
  }
}

const electronNative: TestProjectConfiguration = {
  test: {
    name: 'electron',
    environment: 'node',
    include: ['electron/**/*.test.ts', 'scripts/**.test.{ts,mjs}'],
    // These shell out to real git/bash in temp dirs; on a contended machine a
    // single clone or worktree add can outrun the 5s default on its own.
    testTimeout: TEST_TIMEOUT_MS,
    hookTimeout: TEST_TIMEOUT_MS
  }
}

export default defineConfig({
  test: {
    projects: [reactUi, electronNative]
  }
})
