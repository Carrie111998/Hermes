import { describe, expect, it } from 'vitest'

import { sessionIdentityKey } from '@/lib/session-identity'

import { sessionSwitcherStatus } from './session-switcher'

describe('sessionSwitcherStatus', () => {
  it('reads status from the owning profile when stored session ids collide', () => {
    const statusIds = {
      attention: new Set([sessionIdentityKey('shared', 'alpha')]),
      unread: new Set([sessionIdentityKey('shared', 'beta')]),
      working: new Set([sessionIdentityKey('shared', 'beta')])
    }

    expect(sessionSwitcherStatus({ id: 'shared', profile: 'alpha' }, statusIds)).toEqual({
      attention: true,
      unread: false,
      working: false
    })
    expect(sessionSwitcherStatus({ id: 'shared', profile: 'beta' }, statusIds)).toEqual({
      attention: false,
      unread: true,
      working: true
    })
  })
})
