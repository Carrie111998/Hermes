import { describe, expect, it } from 'vitest'

import {
  parseSessionIdentityKey,
  sessionIdentityKey,
  sessionIdentityKeysFromLegacyIds,
  sessionMatchesIdentity
} from './session-identity'

describe('session identity', () => {
  it('normalizes the profile and keeps identical stored ids distinct across profiles', () => {
    expect(sessionIdentityKey('shared-id', ' alpha ')).toBe(sessionIdentityKey('shared-id', 'alpha'))
    expect(sessionIdentityKey('shared-id', 'alpha')).not.toBe(sessionIdentityKey('shared-id', 'beta'))
    expect(sessionIdentityKey('shared-id', null)).toBe(sessionIdentityKey('shared-id', 'default'))
  })

  it('treats the raw stored id as opaque', () => {
    expect(sessionIdentityKey(' shared-id', 'alpha')).not.toBe(sessionIdentityKey('shared-id', 'alpha'))
    expect(sessionIdentityKey('shared-id ', 'alpha')).not.toBe(sessionIdentityKey('shared-id', 'alpha'))
    expect(parseSessionIdentityKey(sessionIdentityKey(' shared-id ', 'alpha')).storedSessionId).toBe(' shared-id ')
    expect(parseSessionIdentityKey(sessionIdentityKey('alpha\u0000shared', 'default')).storedSessionId).toBe(
      'alpha\u0000shared'
    )

    expect(sessionMatchesIdentity({ id: ' shared-id ', profile: 'alpha' }, ' shared-id ', 'alpha')).toBe(true)
    expect(sessionMatchesIdentity({ id: 'shared-id', profile: 'alpha' }, ' shared-id ', 'alpha')).toBe(false)
  })

  it('round-trips compound keys without assuming ids are globally unique', () => {
    expect(parseSessionIdentityKey(sessionIdentityKey('shared-id', 'alpha'))).toEqual({
      profile: 'alpha',
      storedSessionId: 'shared-id'
    })
  })

  it('matches stored and lineage ids only within the owning profile', () => {
    const session = {
      id: 'runtime-id',
      profile: 'alpha',
      _lineage_root_id: 'stored-id'
    }

    expect(sessionMatchesIdentity(session, 'stored-id', 'alpha')).toBe(true)
    expect(sessionMatchesIdentity(session, 'stored-id', 'beta')).toBe(false)
    expect(sessionMatchesIdentity(session, 'runtime-id', 'alpha')).toBe(true)
  })

  it('migrates legacy ownerless ids as opaque default-profile values', () => {
    const sentinelLookingId = 'alpha\u0000shared'

    expect(sessionIdentityKeysFromLegacyIds([sentinelLookingId, '\u0000leading', sentinelLookingId])).toEqual([
      sessionIdentityKey(sentinelLookingId, 'default'),
      sessionIdentityKey('\u0000leading', 'default')
    ])
    expect(sessionIdentityKey(sentinelLookingId, 'default')).not.toBe(sessionIdentityKey('shared', 'alpha'))
  })
})
