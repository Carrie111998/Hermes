import { describe, expect, it } from 'vitest'

import { sessionIdentityKey } from '@/lib/session-identity'

import { archivedSessionElementId, archivedSessionTarget, sessionPaletteItemId } from './session-items'

describe('command-palette session identity', () => {
  it('distinguishes colliding stored ids by profile and keeps ids opaque', () => {
    expect(sessionPaletteItemId('session', { id: 'shared', profile: 'alpha' })).not.toBe(
      sessionPaletteItemId('session', { id: 'shared', profile: 'beta' })
    )
    expect(sessionPaletteItemId('session', { id: ' shared ', profile: 'alpha' })).toContain(
      encodeURIComponent(sessionIdentityKey(' shared ', 'alpha'))
    )
  })

  it('deep-links archived rows by compound identity', () => {
    const identity = sessionIdentityKey('shared', 'alpha')

    expect(archivedSessionTarget({ id: 'shared', profile: 'alpha' })).toContain(encodeURIComponent(identity))
    expect(archivedSessionElementId(identity)).toBe(`archived-session-${encodeURIComponent(identity)}`)
  })
})
