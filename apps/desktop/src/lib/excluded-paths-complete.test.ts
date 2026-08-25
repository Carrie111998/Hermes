import { describe, expect, it } from 'vitest'
import { ALWAYS_EXCLUDED } from '@/lib/excluded-paths'

describe('ALWAYS_EXCLUDED completeness', () => {
  it('contains Node.js ecosystem dirs', () => {
    for (const d of ['node_modules', '.nuxt', '.svelte-kit', '.output', 'bower_components']) {
      expect(ALWAYS_EXCLUDED.has(d)).toBe(true)
    }
  })

  it('contains Python ecosystem dirs', () => {
    for (const d of ['__pycache__', '.mypy_cache', '.pytest_cache', '.ruff_cache', '.tox']) {
      expect(ALWAYS_EXCLUDED.has(d)).toBe(true)
    }
  })

  it('contains IDE dirs', () => {
    expect(ALWAYS_EXCLUDED.has('.idea')).toBe(true)
  })

  it('contains build system dirs', () => {
    for (const d of ['.gradle', 'target', 'dist', 'build', 'out']) {
      expect(ALWAYS_EXCLUDED.has(d)).toBe(true)
    }
  })

  it('contains virtual environment dirs', () => {
    for (const d of ['.venv', 'venv', 'env']) {
      expect(ALWAYS_EXCLUDED.has(d)).toBe(true)
    }
  })
})
