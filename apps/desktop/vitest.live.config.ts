import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    name: 'windows-remote-live',
    environment: 'node',
    include: ['electron/**/*.live.test.ts']
  }
})
