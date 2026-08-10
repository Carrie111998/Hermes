import { describe, expect, it } from 'vitest'

import { resolveWindowBackendProfile } from './window-backend-profile'

describe('resolveWindowBackendProfile', () => {
  it('prefers the profile returned by a scoped backend connection', () => {
    expect(resolveWindowBackendProfile('coder', 'default')).toBe('coder')
  })

  it('falls back to the saved preference for an unscoped primary connection', () => {
    expect(resolveWindowBackendProfile(undefined, 'worker')).toBe('worker')
  })

  it('normalizes empty values to default', () => {
    expect(resolveWindowBackendProfile('  ', null)).toBe('default')
  })
})
