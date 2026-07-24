import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    // Runs once, before any test file is loaded, so lockfile drift is reported
    // as drift instead of as a wall of missing-export failures. See
    // vitest.globalSetup.mjs.
    globalSetup: ['./vitest.globalSetup.mjs'],
    environment: 'node',
    include: ['**/*.test.{ts,mjs}'],
  },
})
