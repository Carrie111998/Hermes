import { describe, expect, it } from 'vitest'
import { ALWAYS_EXCLUDED } from '@/lib/excluded-paths'

describe('ALWAYS_EXCLUDED', () => {
  it('contains VCS internals', () => {
    expect(ALWAYS_EXCLUDED.has('.git')).toBe(true)
    expect(ALWAYS_EXCLUDED.has('.hg')).toBe(true)
    expect(ALWAYS_EXCLUDED.has('.svn')).toBe(true)
  })

  it('contains dependency dirs', () => {
    expect(ALWAYS_EXCLUDED.has('node_modules')).toBe(true)
    expect(ALWAYS_EXCLUDED.has('vendor')).toBe(true)
    expect(ALWAYS_EXCLUDED.has('Pods')).toBe(true)
  })

  it('contains Python caches', () => {
    expect(ALWAYS_EXCLUDED.has('__pycache__')).toBe(true)
    expect(ALWAYS_EXCLUDED.has('.venv')).toBe(true)
    expect(ALWAYS_EXCLUDED.has('.pytest_cache')).toBe(true)
  })

  it('contains build output dirs', () => {
    expect(ALWAYS_EXCLUDED.has('dist')).toBe(true)
    expect(ALWAYS_EXCLUDED.has('build')).toBe(true)
    expect(ALWAYS_EXCLUDED.has('.next')).toBe(true)
  })
})
