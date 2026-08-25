import { describe, expect, it } from 'vitest'
import { connectionScopeSuffix } from '@/lib/connection-scoped'

describe('connectionScopeSuffix', () => {
  it('returns empty for local connection', () => {
    expect(connectionScopeSuffix({ mode: 'local' })).toBe('')
  })

  it('returns empty for undefined connection', () => {
    expect(connectionScopeSuffix(undefined)).toBe('')
  })

  it('returns empty for null connection', () => {
    expect(connectionScopeSuffix(null)).toBe('')
  })

  it('returns empty for unknown mode', () => {
    expect(connectionScopeSuffix({ mode: 'unknown' as never })).toBe('')
  })

  it('returns remote suffix with base and profile', () => {
    const result = connectionScopeSuffix({
      mode: 'remote',
      baseUrl: 'https://example.com',
      profile: 'myprofile',
    })
    expect(result).toContain('.remote.')
    expect(result).toContain('example.com')
    expect(result).toContain('myprofile')
  })

  it('URL-encodes special characters', () => {
    const result = connectionScopeSuffix({
      mode: 'remote',
      baseUrl: 'https://example.com/path?q=1',
      profile: 'user/name',
    })
    expect(result).not.toContain('/')
    expect(result).not.toContain('?')
  })

  it('defaults profile to "default"', () => {
    const result = connectionScopeSuffix({
      mode: 'remote',
      baseUrl: 'https://example.com',
    })
    expect(result).toContain('default')
  })
})
